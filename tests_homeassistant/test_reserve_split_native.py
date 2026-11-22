"""Test the actual HA dispatch fence with a guarded, synthetic auxiliary circuit."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import threading
import time
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.house_climate.controller.bindings import PowerBindings
from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.power_guard import HeaterPowerGuard
from custom_components.house_climate.reserve_split_native import ReserveSplitAuthorization
from custom_components.house_climate.runtime import NativeAPI, NativeActivationDispatch
from tests.test_controller import config, fixture


class NativeReserveSplitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        self.config = replace(config(), legacy=False)
        self.api = NativeAPI(self.hass, True, threading.Event(), self.config)
        self.controls = self.api.controls = NativeControls(self.hass, self.config, True, "reserve", lambda: None)
        self.controls.active = self.controls.enabled = True
        self.controls.owned = tuple(self.config.climate_entities)
        now = time.monotonic()
        self.controls.heartbeat_monotonic = self.controls.auxiliary_authorized_at = now
        self.controls._auxiliary_heat_possible = True
        self.controls._auxiliary_context = []
        self.controls.auxiliary_context_provider = lambda: ()
        self.controls._auxiliary_stop_limit_f = 72
        self.controls._publish()

        self.heater = self.config.heater_entity
        self.head = self.config.heads["guest"]
        self.other = self.config.heads["bonus"]
        self.reserve_head = self.config.heads["living"]
        self.probes = self.config.zones[self.config.auxiliary_room].probes
        states = fixture(self.config, datetime.now(timezone.utc), guest=76, living=67)
        for entity, state in states.items():
            self.hass.states.async_set(entity, state["state"], state.get("attributes", {}))
        for entity in self.config.heads.values():
            self.hass.states.async_set(entity, "off", {"hvac_modes": ["off", "cool", "heat"],
                                                       "hvac_action": "off"})
        self.hass.states.async_set(self.heater, "heat", {"hvac_modes": ["off", "heat"],
                                                       "hvac_action": "heating"})
        self.bindings = PowerBindings(
            heater=self.heater, heater_switch="switch.fixture_heater", battery="sensor.fixture_battery",
            circuit_power="sensor.fixture_circuit", input_power="sensor.fixture_input",
            output_power="sensor.fixture_output", heater_current=None,
            wall_phase_a_voltage="sensor.fixture_va", wall_phase_b_voltage="sensor.fixture_vb",
            heater_power="sensor.fixture_load", ac_output_switch="switch.fixture_ac")
        self.guard = self.api.power_guard = HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="reserve", notify=lambda: None)
        self.guard._blocked = False
        self.guard._active = True
        self.hass.states.async_set(self.bindings.heater_switch, "on")
        self.hass.states.async_set(self.bindings.ac_output_switch, "on")
        for entity, reading, unit in (
            (self.bindings.battery, 90, "%"), (self.bindings.circuit_power, 1350, "W"),
            (self.bindings.input_power, 550, "W"), (self.bindings.output_power, 1350, "W"),
            (self.bindings.heater_power, 1250, "W"), (self.bindings.wall_phase_a_voltage, 120, "V"),
            (self.bindings.wall_phase_b_voltage, 120, "V"),
        ):
            self.hass.states.async_set(entity, str(reading), {"unit_of_measurement": unit})
        self.calls = []
        async def handle(call):
            self.calls.append(dict(call.data))
        self.hass.services.async_register("climate", "set_temperature", handle)

    async def asyncTearDown(self):
        self.controls.active = False
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def new_ticket(self, **changes):
        proof = await asyncio.to_thread(self.api.reserve_split_proof)
        self.assertIsNotNone(proof)
        self.assertTrue(proof["ready"])
        self.assertLessEqual(proof["heat_reported_at"], proof["meter_reported_at"])
        ticket = {**proof, "heads": [self.head], "reserve_room": self.config.auxiliary_room,
                  "floor_f": 63, "changeover_seconds": 90,
                  "probe_sources": sorted({probe.source.strip() for probe in self.probes}),
                  "expires_at_monotonic": time.monotonic() + 10, "attempt_id": "test",
                  **changes}
        self.assertTrue(self.api.set_reserve_split_authorization(ticket))
        return ticket

    async def send(self, ticket, *, entity=None, window_ticket=None):
        return await asyncio.to_thread(
            self.api.service_reserve_guarded, "climate", "set_temperature",
            ticket=ticket, window_ticket=window_ticket, entity_id=entity or self.head,
            hvac_mode="cool", temperature=69)

    async def test_native_api_records_normal_dispatch_not_deferred_requests(self):
        from custom_components.house_climate.mode_diagnostics import MODE, PATH, OUTCOME

        ticket = await self.new_ticket()
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket, entity=self.other)
        self.assertFalse(self.controls.mode_diagnostics.values(self.other))
        await self.send(ticket)
        values = self.controls.mode_diagnostics.values(self.head)
        self.assertEqual(values["command_path_code"], PATH["normal"])
        self.assertEqual(values["command_mode_code"], MODE["cool"])
        self.assertEqual(values["command_outcome_code"], OUTCOME["returned"])

    async def test_qualified_send_is_only_for_exact_bound_cooling_heads(self):
        ticket = await self.new_ticket()
        # Rebinding the Living head as a cooling victim invalidates and revokes.
        self.assertFalse(self.api.set_reserve_split_authorization({**ticket, "heads": [self.reserve_head]}))
        self.assertTrue(self.api.set_reserve_split_authorization(ticket))
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket, entity=self.other)
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket, window_ticket=object())
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(self.api.service, "climate", "set_temperature",
                                    entity_id=self.head, hvac_mode="cool", temperature=68)
        await self.send(ticket)
        self.assertEqual(self.calls, [{"entity_id": self.head, "hvac_mode": "cool", "temperature": 69}])

    async def test_revoke_fences_already_queued_positive_but_not_new_ordinary_cooling(self):
        ticket = await self.new_ticket()
        parsed = ReserveSplitAuthorization.parse(ticket, self.config)
        dispatch = NativeActivationDispatch(
            time.monotonic() + 5, reserve_split=parsed, reserve_epoch=self.api._reserve_split_epoch)
        queued = {"entity_id": self.head, "hvac_mode": "cool", "temperature": 69}
        self.assertTrue(self.api.set_reserve_split_authorization(None))
        with self.assertRaises(HAActivationDeferred):
            await self.api._async_request("services/climate/set_temperature", queued, dispatch.deadline,
                                         dispatch=dispatch, generation=self.controls.ownership_generation)
        self.assertFalse(self.calls)
        await asyncio.to_thread(self.api.service, "climate", "set_temperature", **queued)
        self.assertEqual(self.calls, [queued])

    async def test_competing_cooling_queued_before_split_cannot_dispatch(self):
        before = self.api._reserve_split_epoch
        await self.new_ticket()
        dispatch = NativeActivationDispatch(time.monotonic() + 5, reserve_epoch=before)
        with self.assertRaises(HAActivationDeferred):
            await self.api._async_request("services/climate/set_temperature",
                                         {"entity_id": self.other, "hvac_mode": "cool", "temperature": 68},
                                         dispatch.deadline, dispatch=dispatch,
                                         generation=self.controls.ownership_generation)
        self.assertFalse(self.calls)

    async def test_expiry_repair_cold_and_stop_revision_veto(self):
        expired = await self.new_ticket(expires_at_monotonic=time.monotonic() - 1)
        with self.assertRaises(HAActivationDeferred):
            await self.send(expired)
        overlong = await self.new_ticket(expires_at_monotonic=time.monotonic() + self.config.poll_seconds + 2)
        with self.assertRaises(HAActivationDeferred):
            await self.send(overlong)
        ticket = await self.new_ticket()
        repair = self.api.repair_gate.begin("repair-fixture", [self.reserve_head])
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.api.repair_gate.release(repair)
        # Completed repair advances the non-target Living epoch too.
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        ticket = await self.new_ticket()
        probe = self.probes[0].entity_id
        self.hass.states.async_set(probe, "61", {"unit_of_measurement": "°F"})
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.hass.states.async_set(probe, "67", {"unit_of_measurement": "°F"})
        self.controls.auxiliary_stop_revision += 1
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.assertFalse(self.calls)

    async def test_missing_planned_probe_cannot_be_replaced_by_warmer_remaining_probes(self):
        ticket = await self.new_ticket()
        source = self.probes[0].source
        for probe in self.probes:
            if probe.source == source:
                self.hass.states.async_set(probe.entity_id, "unavailable")
        self.assertIsNotNone(await asyncio.to_thread(self.api.reserve_split_proof))
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.assertFalse(self.calls)
        # A configured source which was not admitted by planning is not
        # itself required, provided the actual admitted set is still fresh.
        remaining = [item for item in ticket["probe_sources"] if item != source.strip()]
        less = {**ticket, "probe_sources": remaining}
        self.assertTrue(self.api.set_reserve_split_authorization(less))
        await self.send(less)
        self.assertEqual(len(self.calls), 1)

    async def test_empty_isolation_ticket_fences_every_cooling_dispatch(self):
        isolation = await self.new_ticket(heads=[], attempt_id=time.monotonic())
        with self.assertRaises(HAActivationDeferred):
            await self.send(isolation)
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(self.api.service, "climate", "set_temperature",
                                    entity_id=self.head, hvac_mode="cool", temperature=68)
        self.assertFalse(self.calls)

    async def test_new_auxiliary_stop_epoch_invalidates_previous_heat_evidence(self):
        ticket = await self.new_ticket()
        self.controls._auxiliary_stop_epoch += 1
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.assertFalse(self.calls)

    async def test_live_power_is_checked_even_if_cached_guard_has_not_run(self):
        ticket = await self.new_ticket()
        self.hass.states.async_set(self.bindings.battery, "39", {"unit_of_measurement": "%"})
        self.assertFalse(self.guard.blocked)  # Existing async guard has not observed this.
        self.assertIsNone(await asyncio.to_thread(self.api.reserve_split_proof))
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        self.assertFalse(self.calls)

    async def test_stale_restored_idle_or_non_dedicated_draw_is_not_proof(self):
        self.assertIsNotNone(await asyncio.to_thread(self.api.reserve_split_proof))
        for power, attrs, at in (
            (1250, {"restored": True}, time.time()),
            (1250, {}, time.time() - 300),
            (30, {}, time.time()),
        ):
            with self.subTest(power=power, attrs=attrs):
                self.hass.states.async_set(self.bindings.heater_power, str(power),
                                           {"unit_of_measurement": "W", **attrs}, timestamp=at)
                self.assertIsNone(await asyncio.to_thread(self.api.reserve_split_proof))
        self.guard.bindings = replace(self.bindings, heater_power=None)
        self.assertIsNone(await asyncio.to_thread(self.api.reserve_split_proof))
