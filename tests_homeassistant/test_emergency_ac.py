"""Verify optional heater shutdown stages against real HA events and services."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.aggregate_stop import AggregateStopConfig
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as native_controls


BASE = example_bindings().power
SHARED_AC = "switch.reserve_ac_output"


class EmergencyACGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.calls = []
        self.guards = []
        self.ignore_climate = False
        self.ignore_dedicated = False
        self.ignore_ac = False
        self.hold_ac = False
        self.ac_feedback_attributes = {}
        self.ac_entered = asyncio.Event()
        self.ac_release = asyncio.Event()
        self.current_after_native_off = None
        self.patchers = [
            patch.object(power, "OFF_ACK_SECONDS", .025),
            patch.object(power, "RESTORE_ACK_SECONDS", .04),
            patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025),
        ]
        for patcher in self.patchers:
            patcher.start()
        for domain, services in (("climate", ("set_hvac_mode",)),
                                 ("switch", ("turn_off", "turn_on"))):
            for service in services:
                self.hass.services.async_register(domain, service, self.handle)
        self.refresh_safe_measurements()
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(BASE.heater_switch, "on")
        self.hass.states.async_set(SHARED_AC, "on")

    async def asyncTearDown(self):
        self.ac_release.set()
        self.ignore_climate = self.ignore_dedicated = self.ignore_ac = False
        for guard in reversed(self.guards):
            await guard.async_close()
        await self.hass.async_block_till_done()
        for guard in reversed(self.guards):
            await guard.async_close()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def refresh_safe_measurements(self):
        for entity, value, unit in (
            (BASE.circuit_power, 140, "W"), (BASE.input_power, 130, "W"),
            (BASE.output_power, 150, "W"), (BASE.battery, 80, "%"),
            (BASE.heater_current, .05, "A"),
            (BASE.wall_phase_a_voltage, 124, "V"),
            (BASE.wall_phase_b_voltage, 123, "V"),
        ):
            self.hass.states.async_set(entity, str(value),
                                       {"unit_of_measurement": unit}, force_update=True)

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "climate":
            if not self.ignore_climate:
                self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                           force_update=True)
                if self.current_after_native_off is not None:
                    self.hass.states.async_set(BASE.heater_current,
                                               str(self.current_after_native_off),
                                               {"unit_of_measurement": "A"},
                                               force_update=True)
            return
        entity = call.data["entity_id"]
        state = "off" if call.service == "turn_off" else "on"
        if entity == SHARED_AC:
            self.ac_entered.set()
            if self.hold_ac and call.service == "turn_off":
                # Model an integration that continues its actual command after
                # the caller times out or attempts to cancel it.
                while not self.ac_release.is_set():
                    try:
                        await self.ac_release.wait()
                    except asyncio.CancelledError:
                        continue
            if self.ignore_ac:
                return
        elif entity == BASE.heater_switch:
            if self.ignore_dedicated:
                return
        else:
            raise AssertionError(f"Unexpected actuator: {entity}")
        attributes = self.ac_feedback_attributes if entity == SHARED_AC else {}
        self.hass.states.async_set(entity, state, attributes, force_update=True)
        if state == "off":
            self.hass.states.async_set(BASE.heater, "unavailable")

    def direct_bindings(self, *, enabled=True, current=None):
        return replace(BASE, heater_switch=None, heater_current=current,
                       emergency_ac_switch=SHARED_AC,
                       emergency_ac_fallback_enabled=enabled)

    async def start(self, bindings=None, *, live=True, saved=None):
        guard = power.HeaterPowerGuard(
            self.hass, bindings or self.direct_bindings(), live=live,
            entry_id="emergency-stage-test", notify=lambda: None)
        self.guards.append(guard)
        if saved is None:
            await guard.async_load()
        else:
            store = Mock(async_load=AsyncMock(return_value=saved), async_save=AsyncMock())
            with patch("homeassistant.helpers.storage.Store", return_value=store):
                await guard.async_load()
        guard.async_start()
        await self.hass.async_block_till_done()
        return guard

    async def trip(self, *, wait=True):
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(BASE.input_power, "1500", {"unit_of_measurement": "W"})
        if wait:
            await self.hass.async_block_till_done()

    def switch_calls(self, entity, service="turn_off"):
        return [call for call in self.calls
                if call[0:2] == ("switch", service) and call[2]["entity_id"] == entity]

    async def wait_for(self, predicate, timeout=1):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.005)

    async def establish_dedicated_claim(self):
        self.ignore_climate = True
        source = await self.start(BASE)
        await self.trip()
        self.assertTrue(source.switch_cut_owned)
        saved = source._saved_state()
        self.assertTrue(await source.async_close())
        self.ignore_climate = False
        self.refresh_safe_measurements()
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        return saved

    async def prepare_manual_recovery(self):
        self.ignore_climate = False
        self.refresh_safe_measurements()
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                   force_update=True)
        await self.hass.async_block_till_done()

    async def test_explicit_off_without_electrical_trip_uses_native_ack(self):
        guard = await self.start()
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertIsNone(guard.diagnostics["reason"])
        self.assertTrue(await guard.async_request_heater_off())
        self.assertEqual(self.hass.states.get(BASE.heater).state, "off")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertFalse(guard.diagnostics["latched"])
        self.assertEqual(self.switch_calls(SHARED_AC), [])

    async def test_second_stop_does_not_reuse_precommand_high_current(self):
        guard = await self.start(self.direct_bindings(current=BASE.heater_current))
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        self.assertTrue(await guard.async_request_heater_off())
        # A later heating run naturally has current above the first stop's
        # boundary. It is not post-command evidence against the second Off.
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(BASE.heater_current, "3", {"unit_of_measurement": "A"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        self.assertFalse(guard.diagnostics["latched"])
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(await guard.async_request_heater_off())
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertEqual(self.switch_calls(SHARED_AC), [])

    async def test_stale_or_future_ac_on_cannot_authorize_heating(self):
        guard = await self.start()
        for offset in (-power.TELEMETRY_MAX_AGE_SECONDS - 1, 6):
            with self.subTest(ac_report_offset_seconds=offset):
                self.hass.states.async_set(SHARED_AC, "on", force_update=True)
                self.hass.states.get(SHARED_AC).last_reported = (
                    datetime.now(timezone.utc) + timedelta(seconds=offset))
                with self.assertRaises(HAActivationDeferred):
                    guard.check_activation(BASE.heater, {"hvac_mode": "heat"})
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        guard.check_activation(BASE.heater, {"hvac_mode": "heat"})
        self.assertEqual(self.calls, [])

    async def test_manual_reset_requires_thermostat_off_after_ac_restoration(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        self.refresh_safe_measurements()
        # This cached Off is fresh in age, but predates manual AC restoration.
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                   force_update=True)
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        await self.hass.async_block_till_done()
        with self.assertRaisesRegex(RuntimeError, "fresh thermostat Off"):
            await guard.async_acknowledge_emergency()
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        self.assertTrue(await guard.async_acknowledge_emergency())
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_close_during_emergency_intent_save_retains_actual_shutdown(self):
        self.ignore_climate = True
        guard = await self.start()
        save_entered, release_save = asyncio.Event(), asyncio.Event()
        original_save = guard._store.async_save

        async def held_save(data):
            if data.get("emergency_ac_latched"):
                save_entered.set()
                await release_save.wait()
            await original_save(data)

        guard._store.async_save = held_save
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        caller = asyncio.create_task(guard.async_request_heater_off())
        await self.wait_for(save_entered.is_set)
        self.assertEqual(self.switch_calls(SHARED_AC), [])
        try:
            self.assertFalse(await guard.async_close())
            self.assertFalse(caller.cancelled())
            await self.wait_for(lambda: len(self.switch_calls(SHARED_AC)) == 1)
            self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
            self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        finally:
            release_save.set()
            await caller
            await self.hass.async_block_till_done()
        self.assertTrue(await guard.async_close())
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_explicit_off_without_electrical_trip_escalates_when_unresponsive(self):
        self.ignore_climate = True
        guard = await self.start()
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertTrue(await guard.async_request_heater_off())
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_cancelled_off_caller_does_not_cancel_actual_ac_shutdown(self):
        self.ignore_climate = True
        self.hold_ac = True
        guard = await self.start()
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        await self.hass.async_block_till_done()
        caller = asyncio.create_task(guard.async_request_heater_off())
        await self.wait_for(self.ac_entered.is_set)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(.06)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(guard.diagnostics["emergency_ac_inflight"])
        with self.assertRaises(RuntimeError):
            await guard.async_acknowledge_emergency()
        self.ac_release.set()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_legacy_claim_without_identity_is_quarantined(self):
        saved = await self.establish_dedicated_claim()
        saved.pop("binding_identity")
        previous_calls = len(self.calls)
        guard = await self.start(BASE, saved=saved)
        clock = guard._clock
        guard._clock = lambda: clock() + 601
        guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(guard.diagnostics["binding_identity_mismatch"])
        self.assertTrue(guard.diagnostics["manual_recovery_required"])
        self.assertTrue(guard.blocked)
        self.assertFalse(guard.switch_cut_owned)
        self.assertFalse(guard.power_cut_confirmed)
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(BASE.heater_switch, "turn_on"), [])

    async def test_changed_cutoff_identity_cannot_inherit_old_restore_claim(self):
        saved = await self.establish_dedicated_claim()
        replacement = "switch.replacement_heater_outlet"
        self.hass.states.async_set(replacement, "off")
        bindings = replace(BASE, heater_switch=replacement)
        previous_calls = len(self.calls)
        guard = await self.start(bindings, saved=saved)
        clock = guard._clock
        guard._clock = lambda: clock() + 601
        guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(guard.diagnostics["binding_identity_mismatch"])
        self.assertTrue(guard.diagnostics["manual_recovery_required"])
        self.assertTrue(guard.blocked)
        self.assertFalse(guard.switch_cut_owned)
        self.assertFalse(guard.power_cut_confirmed)
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(replacement, "turn_on"), [])

    async def test_external_pending_negative_job_prevents_manual_reset(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        await self.prepare_manual_recovery()
        self.hold_ac = True
        self.ac_entered.clear()
        external = self.hass.async_create_task(self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": SHARED_AC}, blocking=True))
        guard.pending_external_jobs = lambda: (external,) if not external.done() else ()
        await self.wait_for(self.ac_entered.is_set)
        with self.assertRaisesRegex(RuntimeError, "actual heater and AC service jobs"):
            await guard.async_acknowledge_emergency()
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.ac_release.set()
        await external
        await self.hass.async_block_till_done()
        with self.assertRaises(RuntimeError):
            await guard.async_acknowledge_emergency()
        await self.prepare_manual_recovery()
        previous_calls = len(self.calls)
        self.assertTrue(await guard.async_acknowledge_emergency())
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_restored_ac_off_does_not_count_as_fresh_acknowledgement(self):
        self.ignore_climate = True
        self.ac_feedback_attributes = {"restored": True}
        guard = await self.start()
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": "heating"})
        await self.hass.async_block_till_done()
        self.assertFalse(await guard.async_request_heater_off())
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
        self.assertFalse(guard.power_cut_confirmed)
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "unconfirmed")
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.hass.states.async_set(SHARED_AC, "off", {}, force_update=True)
        await self.hass.async_block_till_done()
        self.assertTrue(guard.power_cut_confirmed)
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)

    async def test_manual_reset_persistence_failure_retains_emergency_latch(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        await self.prepare_manual_recovery()
        before = guard._saved_state()
        guard._store = Mock(async_save=AsyncMock(side_effect=OSError("storage unavailable")))
        previous_calls = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "Cannot persist manual heater recovery"):
            await guard.async_acknowledge_emergency()
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(guard.diagnostics["manual_recovery_required"])
        self.assertTrue(guard.blocked)
        self.assertEqual(guard._saved_state()["binding_identity"], before["binding_identity"])
        self.assertIsNotNone(guard.diagnostics["persistence_error"])
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_cancelled_manual_reset_remains_blocked_until_save_is_durable(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        await self.prepare_manual_recovery()
        save_entered, release_save = asyncio.Event(), asyncio.Event()
        original_save = guard._store.async_save
        proposed = []

        async def held_save(data):
            if not data.get("emergency_ac_latched"):
                proposed.append(data)
                save_entered.set()
                await release_save.wait()
            await original_save(data)

        guard._store.async_save = held_save
        previous_calls = len(self.calls)
        caller = asyncio.create_task(guard.async_acknowledge_emergency())
        await self.wait_for(save_entered.is_set)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        try:
            self.assertEqual(len(proposed), 1)
            self.assertFalse(proposed[0]["emergency_ac_latched"])
            self.assertTrue(guard.diagnostics["emergency_ac_latched"])
            self.assertTrue(guard.diagnostics["manual_recovery_required"])
            self.assertTrue(guard.blocked)
            self.assertFalse(guard._recovery_task.cancelled())
            guard._tripped_at = guard._clock() - power.COOLDOWN_SECONDS - 1
            guard._safe_since = guard._clock() - power.SAFE_LOAD_SECONDS - 1
            for _ in range(3):
                guard._observe()
                await asyncio.sleep(0)
            self.assertTrue(guard.diagnostics["emergency_ac_latched"])
            self.assertTrue(guard.blocked)
            with self.assertRaises(HAActivationDeferred):
                guard.check_activation(BASE.heater, {"hvac_mode": "heat"})
            self.assertEqual(len(self.calls), previous_calls)
        finally:
            release_save.set()
            await guard._recovery_task
            await self.hass.async_block_till_done()
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_direct_native_off_succeeds_without_shared_ac_cut(self):
        guard = await self.start()
        await self.trip()
        self.assertEqual(self.hass.states.get(BASE.heater).state, "off")
        self.assertEqual(self.switch_calls(SHARED_AC), [])
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIsNone(guard.diagnostics["heater_current_a"])

    async def test_direct_failure_cuts_shared_ac_and_latches_manual_recovery(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(guard.diagnostics["manual_recovery_required"])
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "emergency_ac_off")
        self.assertTrue(guard.power_cut_confirmed)
        self.refresh_safe_measurements()
        guard._tripped_at = guard._clock() - 600
        guard._safe_since = guard._clock() - 61
        for _ in range(3):
            guard._observe()
            await self.hass.async_block_till_done()
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])

    async def test_disconnected_old_outlet_off_is_not_direct_heater_isolation(self):
        self.hass.states.async_set(BASE.heater_switch, "off")
        self.ignore_climate = True
        guard = await self.start()
        self.assertNotIn(BASE.heater_switch, guard.entity_ids)
        self.assertNotIn(BASE.heater_current, guard.entity_ids)
        self.assertFalse(guard.power_cut_confirmed)
        await self.trip()
        self.assertEqual(self.switch_calls(BASE.heater_switch), [])
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)

    async def test_dedicated_success_prevents_shared_ac_escalation(self):
        self.ignore_climate = True
        bindings = replace(BASE, emergency_ac_switch=SHARED_AC,
                           emergency_ac_fallback_enabled=True)
        guard = await self.start(bindings)
        await self.trip()
        self.assertEqual(len(self.switch_calls(BASE.heater_switch)), 1)
        self.assertEqual(self.switch_calls(SHARED_AC), [])
        self.assertTrue(guard.power_cut_confirmed)
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "dedicated_cutoff")

    async def test_dedicated_failure_escalates_once_to_shared_ac(self):
        self.ignore_climate = self.ignore_dedicated = True
        bindings = replace(BASE, emergency_ac_switch=SHARED_AC,
                           emergency_ac_fallback_enabled=True)
        guard = await self.start(bindings)
        await self.trip()
        self.assertGreaterEqual(len(self.switch_calls(BASE.heater_switch)), 1)
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        order = [call[2]["entity_id"] for call in self.calls]
        self.assertLess(order.index(BASE.heater), order.index(BASE.heater_switch))
        self.assertLess(order.index(BASE.heater_switch), order.index(SHARED_AC))
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])

    async def test_unavailable_dedicated_cutoff_cannot_confirm_heater_stop(self):
        self.ignore_climate = self.ignore_dedicated = True
        self.hass.states.async_set(BASE.heater_switch, "unavailable")
        bindings = replace(BASE, emergency_ac_switch=SHARED_AC,
                           emergency_ac_fallback_enabled=True)
        guard = await self.start(bindings)
        await self.trip()
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "emergency_ac_off")

    async def test_legacy_binding_never_discovers_or_cuts_shared_ac(self):
        self.ignore_climate = self.ignore_dedicated = True
        guard = await self.start(BASE)
        await self.trip()
        self.assertNotIn(SHARED_AC, guard.entity_ids)
        self.assertEqual(self.switch_calls(SHARED_AC), [])
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertFalse(guard.power_cut_confirmed)

    async def test_lost_shared_ack_remains_unconfirmed_without_repeat_or_restore(self):
        self.ignore_climate = self.ignore_ac = True
        guard = await self.start()
        await self.trip()
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "unconfirmed")
        self.assertFalse(guard.power_cut_confirmed)
        for _ in range(4):
            guard._observe()
            await self.hass.async_block_till_done()
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])
        with self.assertRaises(HAActivationDeferred):
            guard.check_activation(BASE.heater, {"hvac_mode": "heat"})

    async def test_shadow_never_dispatches_any_shutdown_stage(self):
        self.ignore_climate = True
        guard = await self.start(live=False)
        await self.trip()
        self.assertTrue(guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_disabled_direct_fallback_inhibits_heat_and_never_cuts_shared_ac(self):
        self.ignore_climate = True
        guard = await self.start(self.direct_bindings(enabled=False))
        with self.assertRaises(HAActivationDeferred):
            guard.check_activation(BASE.heater, {"hvac_mode": "heat"})
        await self.trip()
        self.assertEqual(self.switch_calls(SHARED_AC), [])
        self.assertTrue(guard.blocked)
        self.assertFalse(guard.power_cut_confirmed)

    async def test_configured_unavailable_meter_still_inhibits_activation(self):
        self.hass.states.async_set(BASE.heater_current, "unavailable")
        guard = await self.start(self.direct_bindings(current=BASE.heater_current))
        self.assertTrue(guard.blocked)
        with self.assertRaises(HAActivationDeferred):
            guard.check_activation(BASE.heater, {"hvac_mode": "heat"})
        self.assertIn(BASE.heater_current, guard.diagnostics["reason"])

    async def test_fresh_post_off_current_contradiction_escalates(self):
        self.current_after_native_off = 3
        guard = await self.start(self.direct_bindings(current=BASE.heater_current))
        await self.trip()
        self.assertEqual(len(self.switch_calls(SHARED_AC)), 1)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])

    async def test_restart_and_feature_disable_preserve_emergency_inhibition(self):
        self.ignore_climate = True
        first = await self.start()
        await self.trip()
        saved = first._saved_state()
        self.assertTrue(await first.async_close())
        self.ignore_climate = False
        self.refresh_safe_measurements()
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"}, force_update=True)
        previous_calls = len(self.calls)
        second = await self.start(self.direct_bindings(enabled=False), saved=saved)
        self.assertTrue(second.diagnostics["emergency_ac_latched"])
        self.assertTrue(second.blocked)
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])
        with self.assertRaises(HAActivationDeferred):
            second.check_activation(BASE.heater, {"hvac_mode": "heat"})

    async def test_manual_acknowledgement_never_energizes_any_outlet(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        self.ignore_climate = False
        self.refresh_safe_measurements()
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"}, force_update=True)
        await self.hass.async_block_till_done()
        guard._tripped_at = guard._clock() - 600
        guard._safe_since = guard._clock() - 61
        previous_calls = len(self.calls)
        self.assertTrue(await guard.async_acknowledge_emergency())
        self.assertEqual(len(self.calls), previous_calls)
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])

    async def test_late_actual_ac_off_job_blocks_reset_and_unload(self):
        self.ignore_climate = True
        self.hold_ac = True
        guard = await self.start()
        await self.trip(wait=False)
        await self.wait_for(self.ac_entered.is_set)
        await asyncio.sleep(.06)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        with self.assertRaises(RuntimeError):
            await guard.async_acknowledge_emergency()
        self.assertFalse(await guard.async_close())
        self.ac_release.set()
        await self.hass.async_block_till_done()
        self.assertTrue(await guard.async_close())
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "off")
        self.assertEqual(self.switch_calls(SHARED_AC, "turn_on"), [])


class EmergencyACNativeBridgeTests(unittest.IsolatedAsyncioTestCase):
    """Keep real worker transport and ownership accounting in the stop path."""

    runtime = native_controls.NativeControlTests.runtime
    claim = native_controls.NativeControlTests.claim
    settle_watchdog = native_controls.NativeControlTests.settle_watchdog
    until = native_fixtures.NativeTests.until

    async def asyncSetUp(self):
        await native_fixtures.NativeTests.asyncSetUp(self)
        self.guards = []
        self.ignore_ac = False
        self.current_after_off = None
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for patcher in self.patchers:
            patcher.start()
        for service in ("turn_on", "turn_off"):
            self.hass.services.async_register("switch", service, self.handle)
        EmergencyACGuardTests.refresh_safe_measurements(self)
        self.hass.states.async_set(SHARED_AC, "on")

    async def asyncTearDown(self):
        self.ignore_ac = False
        self.current_after_off = None
        EmergencyACGuardTests.refresh_safe_measurements(self)
        self.hass.states.async_set(SHARED_AC, "off", force_update=True)
        try:
            await native_fixtures.NativeTests.asyncTearDown(self)
        finally:
            for guard in reversed(self.guards):
                await guard.async_close()
            for patcher in reversed(self.patchers):
                patcher.stop()

    async def handle(self, call):
        if call.domain == "switch":
            self.calls.append((call.domain, call.service, dict(call.data)))
            if not self.ignore_ac:
                self.hass.states.async_set(call.data["entity_id"],
                                           "off" if call.service == "turn_off" else "on",
                                           force_update=True)
            return
        await native_fixtures.NativeTests.handle(self, call)
        if (call.domain == "climate" and call.data["entity_id"] == self.cfg.heater_entity
                and call.service == "set_hvac_mode" and call.data.get("hvac_mode") == "off"):
            old = self.hass.states.get(self.cfg.heater_entity)
            self.hass.states.async_set(old.entity_id, "off",
                                       {**old.attributes, "hvac_action": "off"}, force_update=True)
            if self.current_after_off is not None:
                self.hass.states.async_set(BASE.heater_current, str(self.current_after_off),
                                           {"unit_of_measurement": "A"}, force_update=True)

    async def guarded_runtime(self, *, aggregate=None, metered=False):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        bindings = replace(
            BASE, heater=runtime.config.heater_entity, heater_switch=None,
            heater_current=BASE.heater_current if metered else None,
            emergency_ac_switch=SHARED_AC, emergency_ac_fallback_enabled=True,
            aggregate_stop_confirmation=aggregate)
        guard = power.HeaterPowerGuard(self.hass, bindings, live=True,
                                       entry_id="native-emergency-stop", notify=lambda: None)
        self.guards.append(guard)
        await guard.async_load()
        guard.async_start()
        runtime.power_guard = runtime.api.power_guard = runtime.controls.power_guard = guard
        guard.pending_external_jobs = lambda: runtime.api.service_jobs((bindings.heater, SHARED_AC))
        await self.hass.async_block_till_done()
        return runtime, guard

    async def test_native_api_off_does_not_wait_for_aggregate_corroboration(self):
        policy = AggregateStopConfig(900, 300, 30, settle_seconds=.12, timeout_seconds=.5)
        runtime, guard = await self.guarded_runtime(aggregate=policy)
        heater = runtime.config.heater_entity
        self.hass.states.async_set(heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(BASE.output_power, "1500", {"unit_of_measurement": "W"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        original_request = runtime.api._async_request

        async def short_budget(path, body, deadline, **kwargs):
            return await original_request(path, body, min(deadline, time.monotonic() + .08), **kwargs)

        # Exercise the real thread-safe request, service job, controls ownership
        # and guard await under a budget shorter than aggregate settling.
        with patch.object(runtime.api, "_async_request", side_effect=short_budget):
            await asyncio.wait_for(asyncio.to_thread(
                runtime.api.service, "climate", "set_hvac_mode", entity_id=heater,
                hvac_mode="off"), timeout=.3)
        self.assertEqual(self.hass.states.get(heater).state, "off")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(runtime.last_fault, {})
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.hass.states.async_set(BASE.output_power, "200", {"unit_of_measurement": "W"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        await asyncio.sleep(.125)
        self.hass.states.async_set(BASE.output_power, "205", {"unit_of_measurement": "W"},
                                   force_update=True)
        await self.hass.async_block_till_done()
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "aggregate_corroborated")
        self.assertEqual(runtime.controls.service_inflight, 0)
        self.assertFalse(any(call[:2] == ("switch", "turn_off") for call in self.calls))

    async def test_native_off_with_current_contradiction_retains_ownership(self):
        runtime, guard = await self.guarded_runtime(metered=True)
        self.ignore_ac = True
        self.current_after_off = 3
        heater = runtime.config.heater_entity
        self.hass.states.async_set(heater, "heat", {"hvac_action": "heating"})
        await self.hass.async_block_till_done()
        # Isolate the owned auxiliary so another head cannot hide a premature
        # ownership release behind its own independent shutdown barrier.
        runtime.controls.owned = (heater,)
        runtime.controls._auxiliary_heat_possible = True
        with self.assertRaises(HAError):
            await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                    entity_id=heater, hvac_mode="off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(heater).state, "off")
        self.assertFalse(guard.shutdown_confirmed)
        self.assertFalse(guard.power_cut_confirmed)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "unconfirmed")
        self.assertFalse(runtime.controls._is_off(heater))
        await runtime.controls._clear_if_off()
        self.assertIn(heater, runtime.controls.owned)
        self.assertFalse(await runtime.async_stop())

    async def test_failed_unload_can_acknowledge_manual_recovery_then_close(self):
        from custom_components.house_climate.button import AcknowledgeHeaterRecovery

        runtime, guard = await self.guarded_runtime(metered=True)
        heater = runtime.config.heater_entity
        self.ignore_ac = True
        self.current_after_off = 3
        self.hass.states.async_set(heater, "heat", {"hvac_action": "heating"})
        runtime.controls.owned = (heater,)
        runtime.controls._auxiliary_heat_possible = True
        self.assertFalse(await runtime.async_stop())
        await self.hass.async_block_till_done()
        self.assertFalse(await guard.async_close())
        self.assertTrue(runtime.stop_event.is_set())
        self.assertTrue(guard._closing)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])

        self.current_after_off = None
        # Simulate verified local recovery; none of these state reports issues
        # a device command. Low current and thermostat Off follow AC restoration.
        self.hass.states.async_set(SHARED_AC, "off", force_update=True)
        self.hass.states.async_set(SHARED_AC, "on", force_update=True)
        EmergencyACGuardTests.refresh_safe_measurements(self)
        self.hass.states.async_set(heater, "off", {"hvac_action": "off"}, force_update=True)
        await self.hass.async_block_till_done()
        previous_calls = len(self.calls)
        button = AcknowledgeHeaterRecovery(SimpleNamespace(
            entry_id="native-emergency-stop", runtime_data=runtime))
        button.hass = self.hass
        self.assertTrue(button.available)
        await button.async_press()
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(guard.blocked)
        with self.assertRaises(HAActivationDeferred):
            guard.check_activation(heater, {"hvac_mode": "heat"})
        with self.assertRaises(HAError):
            await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                    entity_id=heater, hvac_mode="heat", temperature=86)
        self.assertEqual(len(self.calls), previous_calls)
        self.assertTrue(await runtime.async_stop())
        self.assertEqual(runtime.controls.owned, ())
        self.assertTrue(guard._closed)
        self.assertFalse(button.available)
        self.assertFalse(any(call[:2] == ("switch", "turn_on") for call in self.calls))


if __name__ == "__main__":
    unittest.main()
