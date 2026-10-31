"""Dedicated watt-meter behavior against real HA states, services and storage."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.ha import HAActivationDeferred


class NativeHeaterWattTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.b = replace(example_bindings().power, heater_current=None,
                         heater_power="sensor.fixture_heater_watts")
        self.origin = round(datetime.now(timezone.utc).timestamp(), 3)
        self.wall = self.origin
        self.calls = []
        self.ignore_climate = False
        self.ignore_switch = False
        self.climate_reports_idle = False
        self.report_meter_after_off = True
        self.report_zero_after_cut = False
        self.resume_heat_after_on = False
        self.delayed_on = False
        self.ignore_on_cancellation = False
        self.on_started = asyncio.Event()
        self.release_on = asyncio.Event()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.b, live=True, entry_id="watt-fixture",
            notify=lambda: None, wall_clock=lambda: self.wall,
            clock=lambda: self.wall - self.origin)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for p in self.patchers:
            p.start()
        for domain, services in (("climate", ("set_hvac_mode",)),
                                 ("switch", ("turn_on", "turn_off"))):
            for service in services:
                self.hass.services.async_register(domain, service, self.handle)
        for entity, value, unit in (
            (self.b.circuit_power, 140, "W"), (self.b.input_power, 130, "W"),
            (self.b.output_power, 150, "W"), (self.b.battery, 90, "%"),
            (self.b.wall_phase_a_voltage, 124, "V"),
            (self.b.wall_phase_b_voltage, 123, "V"),
            (self.b.heater_power, 5.5, "W"),
        ):
            self.put(entity, value, {"unit_of_measurement": unit})
        self.put(self.b.heater, "off", {"hvac_action": "off"})
        self.put(self.b.heater_switch, "on")

    async def asyncTearDown(self):
        self.ignore_climate = self.ignore_switch = self.delayed_on = False
        self.report_meter_after_off = True
        self.release_on.set()
        await self.hass.async_block_till_done()
        await self.guard.async_close()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()
        for p in self.patchers:
            p.stop()

    def put(self, entity, value, attrs=None, *, timestamp=None, force=False):
        self.hass.states.async_set(entity, str(value), attrs or {},
                                   timestamp=self.wall if timestamp is None else timestamp,
                                   force_update=force)

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "climate":
            plug = self.hass.states.get(self.b.heater_switch)
            if not self.ignore_climate and plug is not None and plug.state == "on":
                self.put(self.b.heater, "heat" if self.climate_reports_idle else "off",
                         {"hvac_action": "idle" if self.climate_reports_idle else "off"},
                         timestamp=self.wall + .001, force=True)
                if self.report_meter_after_off:
                    self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"},
                             timestamp=self.wall + .002)
            return
        if self.ignore_switch:
            return
        if call.service == "turn_on" and self.delayed_on:
            self.on_started.set()
            while not self.release_on.is_set():
                try:
                    await self.release_on.wait()
                except asyncio.CancelledError:
                    if not self.ignore_on_cancellation:
                        raise
        desired = "on" if call.service == "turn_on" else "off"
        self.put(self.b.heater_switch, desired, timestamp=self.wall + .001, force=True)
        if desired == "off":
            self.put(self.b.heater, "unavailable", timestamp=self.wall + .001)
            if self.report_zero_after_cut:
                self.put(self.b.heater_power, 0, {"unit_of_measurement": "W"},
                         timestamp=self.wall + .002)
        elif self.resume_heat_after_on:
            # An appliance can resume retained heating when mains returns.
            self.put(self.b.heater, "heat", {"hvac_action": "heating"},
                     timestamp=self.wall + .001, force=True)

    async def start(self, *, listen=False, saved=None):
        if saved is None:
            await self.guard.async_load()
        else:
            self.store = Mock(async_load=AsyncMock(return_value=saved), async_save=AsyncMock())
            with patch("homeassistant.helpers.storage.Store", return_value=self.store):
                await self.guard.async_load()
        if listen:
            self.guard.async_start()
            await self.hass.async_block_till_done()
        else:
            self.guard._observe()
            await self.hass.async_block_till_done()

    async def refresh(self, elapsed, *, omit=(), changes=None):
        """Write a coherent snapshot before an explicit observation."""
        self.write_snapshot(elapsed, omit=omit, changes=changes)
        self.guard._observe()
        await self.hass.async_block_till_done()

    def write_snapshot(self, elapsed, *, omit=(), changes=None):
        self.wall = self.origin + elapsed
        for entity in self.guard.entity_ids:
            if entity in omit:
                continue
            previous = self.hass.states.get(entity)
            if previous is None:
                continue
            value, attrs = (changes or {}).get(entity, (previous.state, previous.attributes))
            self.put(entity, value, attrs, force=True)

    async def advance_isolated(self, elapsed, *, omit=(), changes=None):
        """Keep observing isolation without inventing powered-off reports."""
        omitted = (self.b.heater_power, self.b.heater, self.b.heater_switch, *omit)
        while self.wall - self.origin < elapsed:
            await self.refresh(min(elapsed, self.wall - self.origin + 30),
                               omit=omitted, changes=changes)

    def store_claim(self, *, owned=False, identity=None):
        return {"latched": True, "switch_cut_owned": owned, "restore_attempts": 0,
                "trip_wall": self.origin - 600,
                "binding_identity": self.guard._binding_identity() if identity is None else identity}

    async def verified_cut(self, *, listen=True):
        """Observe a real guard-issued cutoff, then stop all meter reports."""
        self.report_zero_after_cut = True
        self.ignore_climate = True
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
        self.put(self.b.battery, 40, {"unit_of_measurement": "%"})
        await self.start(listen=listen)
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.ignore_climate = False
        self.put(self.b.battery, 90, {"unit_of_measurement": "%"})

    async def test_verified_cut_can_recover_after_off_meter_expires(self):
        await self.verified_cut()
        self.resume_heat_after_on = True
        await self.advance_isolated(181)
        self.assertTrue(self.guard.blocked)
        self.assertIsNone(self.guard._reading.heater_w)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        await self.advance_isolated(301)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported.timestamp(),
                           self.origin + 301)

    async def test_isolated_recovery_ignores_missing_or_high_advisory_circuit(self):
        for index, circuit in enumerate(("unavailable", 5000)):
            with self.subTest(circuit=circuit):
                if index:
                    await self.guard.async_close()
                    self.wall = self.origin
                    self.calls.clear()
                    self.guard = power.HeaterPowerGuard(
                        self.hass, self.b, live=True, entry_id="watt-fixture",
                        notify=lambda: None, wall_clock=lambda: self.wall,
                        clock=lambda: self.wall - self.origin)
                    self.write_snapshot(0)
                self.put(self.b.circuit_power, circuit, {"unit_of_measurement": "W"})
                await self.verified_cut()
                self.resume_heat_after_on = True
                await self.advance_isolated(181)
                self.assertIsNone(self.guard._reading.heater_w)
                self.assertNotIn("turn_on", [call[1] for call in self.calls])
                await self.advance_isolated(301)
                self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
                self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
                self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
                self.assertFalse(self.guard.blocked)
                self.assertFalse(self.guard.diagnostics["latched"])
                self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported.timestamp(),
                                   self.origin + 301)

    async def test_repeated_trusted_off_reports_preserve_safe_isolation_interval(self):
        await self.verified_cut()
        for elapsed in range(30, 300, 30):
            # Some integrations force state writes even when Off is unchanged.
            await self.refresh(elapsed, omit=(self.b.heater_power, self.b.heater))
            self.assertNotIn("turn_on", [call[1] for call in self.calls])
        await self.refresh(300, omit=(self.b.heater_power, self.b.heater))
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.states.get(self.b.heater).state, "off")

    async def test_verified_isolation_never_hides_unsafe_external_power(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        await self.advance_isolated(301, changes={
            self.b.battery: (40, {"unit_of_measurement": "%"})})
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")

    async def test_coalesced_manual_on_off_relinquishes_cutoff_ownership(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        self.wall = self.origin + 200
        self.put(self.b.heater_switch, "on", timestamp=self.wall + .001, force=True)
        self.put(self.b.heater_switch, "off", timestamp=self.wall + .002, force=True)
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.switch_cut_owned)
        await self.advance_isolated(301)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])

    async def test_unavailable_relay_prevents_isolated_restoration(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        self.ignore_switch = True
        self.wall = self.origin + 200
        self.put(self.b.heater_switch, "unavailable", force=True)
        await self.hass.async_block_till_done()
        await self.advance_isolated(301)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertTrue(self.guard.blocked)

    async def test_coalesced_unavailability_restarts_safe_isolation_interval(self):
        await self.verified_cut()
        await self.advance_isolated(281)
        self.wall = self.origin + 290
        self.put(self.b.heater_switch, "unavailable", timestamp=self.wall + .001, force=True)
        self.put(self.b.heater_switch, "off", timestamp=self.wall + .002, force=True)
        await self.hass.async_block_till_done()
        await self.advance_isolated(301)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        await self.advance_isolated(351)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)

    async def test_fresh_high_watts_during_isolation_are_not_ignored(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"}, force=True)
        await self.hass.async_block_till_done()
        await self.advance_isolated(301)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertTrue(self.guard.blocked)

    async def test_isolated_restore_without_new_low_watts_recuts_and_stays_latched(self):
        await self.verified_cut()
        self.report_meter_after_off = False
        self.resume_heat_after_on = True
        await self.advance_isolated(301)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.blocked)
        # The recut may produce a fresh zero, but cannot complete restoration.
        self.assertTrue(self.guard.power_cut_confirmed)

    async def test_high_output_restores_only_with_new_native_off_and_low_watts(self):
        await self.verified_cut()
        self.put(self.b.output_power, 2500, {"unit_of_measurement": "W"}, force=True)
        await self.advance_isolated(301)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported,
                           self.hass.states.get(self.b.heater).last_reported)
        self.assertEqual(self.guard._reading.heater_w, 5.5)
        self.assertFalse(self.guard.blocked)

    async def test_high_output_does_not_replace_post_off_watt_proof(self):
        await self.verified_cut()
        self.put(self.b.output_power, 2500, {"unit_of_measurement": "W"}, force=True)
        self.report_meter_after_off = False
        self.resume_heat_after_on = True
        await self.advance_isolated(301)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.blocked)

    async def test_isolated_restore_rechecks_external_load_after_persisting_intent(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        entered, release = asyncio.Event(), asyncio.Event()

        async def save(data):
            if data.get("restoring"):
                entered.set()
                await release.wait()

        self.guard._store = Mock(async_save=AsyncMock(side_effect=save))
        self.write_snapshot(301, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.guard._observe()
        try:
            await asyncio.wait_for(entered.wait(), .5)
            self.put(self.b.battery, 40, {"unit_of_measurement": "%"}, force=True)
        finally:
            release.set()
        await self.hass.async_block_till_done()
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.blocked)

    async def test_manual_off_during_restore_intent_requires_operator_recovery(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        entered, release = asyncio.Event(), asyncio.Event()

        async def save(data):
            if data.get("restoring"):
                entered.set()
                await release.wait()

        self.guard._store = Mock(async_save=AsyncMock(side_effect=save))
        self.write_snapshot(301, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.guard._observe()
        try:
            await asyncio.wait_for(entered.wait(), .5)
            self.put(self.b.heater_switch, "on", timestamp=self.wall + .001, force=True)
            self.put(self.b.heater_switch, "off", timestamp=self.wall + .002, force=True)
            await asyncio.sleep(0)
        finally:
            release.set()
        await self.hass.async_block_till_done()
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertTrue(self.guard.blocked)

    async def test_manual_on_aborts_before_guard_on_then_later_off_stays_unowned(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        entered, release = asyncio.Event(), asyncio.Event()

        async def save(data):
            if data.get("restoring"):
                entered.set()
                await release.wait()

        self.guard._store = Mock(async_save=AsyncMock(side_effect=save))
        self.write_snapshot(301, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.guard._observe()
        try:
            await asyncio.wait_for(entered.wait(), .5)
            self.put(self.b.heater_switch, "on", timestamp=self.wall + .001, force=True)
            await asyncio.sleep(0)
        finally:
            release.set()
        await self.hass.async_block_till_done()
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        # The operator requests Off only AFTER the aborted transaction ends.
        self.wall = self.origin + 302
        self.put(self.b.heater_switch, "off", timestamp=self.wall + .001, force=True)
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.switch_cut_owned)
        saved = self.guard._saved_state()
        self.assertFalse(saved["switch_cut_owned"])
        self.assertTrue(saved["manual_recovery_required"])
        await self.guard.async_close()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.b, live=True, entry_id="watt-fixture",
            notify=lambda: None, wall_clock=lambda: self.wall,
            clock=lambda: self.wall - self.origin)
        await self.start(listen=True, saved=saved)
        await self.refresh(363)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])

    async def test_fresh_low_watts_do_not_override_untrusted_relay_off(self):
        for label, attrs in (("restored", {"restored": True}),
                             ("assumed", {"assumed_state": True}),
                             ("no_post_load_report", {})):
            with self.subTest(label=label):
                await self.guard.async_close()
                self.wall = self.origin
                self.calls.clear()
                self.guard = power.HeaterPowerGuard(
                    self.hass, self.b, live=True, entry_id="watt-fixture",
                    notify=lambda: None, wall_clock=lambda: self.wall,
                    clock=lambda: self.wall - self.origin)
                self.put(self.b.heater_switch, "off", attrs)
                self.put(self.b.heater, "off", {"hvac_action": "off"})
                await self.start(saved=self.store_claim(owned=True))
                omit = (self.b.heater_switch,) if not attrs else ()
                await self.refresh(1, omit=omit)
                await self.refresh(62, omit=omit)
                self.assertIsNotNone(self.guard._reading.heater_w)
                self.assertNotIn("turn_on", [call[1] for call in self.calls])
                self.assertTrue(self.guard.blocked)

    async def test_unobserved_gap_requires_new_relay_evidence_before_restore(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        # No local observations for longer than the telemetry freshness limit.
        await self.refresh(401, omit=(self.b.heater_power, self.b.heater,
                                      self.b.heater_switch))
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.write_snapshot(402, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.put(self.b.heater_switch, "off", timestamp=self.wall + .001, force=True)
        await self.hass.async_block_till_done()
        await self.advance_isolated(463)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)

    async def test_restart_claim_needs_fresh_post_load_relay_confirmation(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        saved = self.guard._saved_state()
        await self.guard.async_close()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.b, live=True, entry_id="watt-fixture",
            notify=lambda: None, wall_clock=lambda: self.wall,
            clock=lambda: self.wall - self.origin)
        await self.start(listen=True, saved=saved)
        await self.advance_isolated(301)
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        self.wall = self.origin + 302
        self.put(self.b.heater_switch, "off", timestamp=self.wall + .001, force=True)
        await self.hass.async_block_till_done()
        await self.advance_isolated(363)
        self.assertEqual([call[1] for call in self.calls].count("turn_on"), 1)
        self.assertFalse(self.guard.blocked)

    async def test_cancelled_isolated_restore_compensates_late_uncancellable_on(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        self.delayed_on = self.ignore_on_cancellation = True
        self.write_snapshot(301, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.guard._observe()
        await asyncio.wait_for(self.on_started.wait(), .5)
        task = self.guard._action_task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.release_on.set()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.blocked)

    async def test_isolated_restore_unload_compensates_late_on(self):
        await self.verified_cut()
        await self.advance_isolated(181)
        self.delayed_on = self.ignore_on_cancellation = True
        self.write_snapshot(301, omit=(self.b.heater_power, self.b.heater,
                                       self.b.heater_switch))
        self.guard._observe()
        await asyncio.wait_for(self.on_started.wait(), .5)
        self.assertFalse(await self.guard.async_close())
        self.release_on.set()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.blocked)

    async def test_fresh_native_off_and_post_request_watts_confirm_electrical_stop(self):
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
        await self.start()
        self.assertTrue(await self.guard.async_request_heater_off())
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertEqual(self.guard.diagnostics["heater_meter_kind"], "power")
        self.assertTrue(self.guard.diagnostics["heater_power_configured"])
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])

    async def test_cached_low_watts_are_not_post_command_proof(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        await self.start()
        # Native acknowledgement is bounded independently of meter updates.
        self.assertTrue(await asyncio.wait_for(self.guard.async_request_heater_off(), .25))
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIsNone(self.guard._heater_meter_stopped())
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.wall += .01
        self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"})
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertNotIn("turn_off", [call[1] for call in self.calls])

    async def test_thermostat_idle_is_not_native_off_acknowledgement(self):
        self.climate_reports_idle = True
        self.put(self.b.heater, "heat", {"hvac_action": "idle"})
        await self.start()
        await self.guard.async_request_heater_off()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.calls])
        self.assertTrue(self.guard.power_cut_confirmed)

    async def test_late_high_watts_after_off_invokes_dedicated_cutoff(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        await self.start(listen=True)
        self.assertTrue(await self.guard.async_request_heater_off())
        self.wall += .01
        self.put(self.b.heater_power, 200, {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard._shutdown_contradicted)
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.calls])
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")

    async def test_unchanged_watt_report_refreshes_last_reported_not_last_updated(self):
        await self.start()
        before = self.hass.states.get(self.b.heater_power).last_updated
        await self.refresh(181, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.blocked)
        self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"})
        self.assertEqual(self.hass.states.get(self.b.heater_power).last_updated, before)
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported, before)
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_stale_zero_watts_inhibit_activation_without_startup_trip(self):
        self.put(self.b.heater_power, 0, {"unit_of_measurement": "W"})
        await self.start()
        await self.refresh(181, omit=(self.b.heater_power,))
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(self.b.heater)
        self.assertIn("reading not current", self.guard.diagnostics["reason"])
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])

    async def test_stale_high_watts_stop_running_heat_without_inventing_zero(self):
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.report_meter_after_off = False
        await self.start()
        await self.refresh(181, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertIn(("climate", "set_hvac_mode"), [call[:2] for call in self.calls])
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_threshold_and_untrusted_watts_never_become_stop_proof(self):
        await self.start()
        self.guard._off_request_wall = self.origin - 1
        for value, attrs, expected in (
            (99.999, {"unit_of_measurement": "W"}, True),
            (100, {"unit_of_measurement": "W"}, False),
            (0, {"unit_of_measurement": "W", "restored": True}, None),
            (0, {"unit_of_measurement": "W", "assumed_state": True}, None),
        ):
            with self.subTest(value=value, attrs=attrs):
                self.wall += .01
                self.put(self.b.heater_power, value, attrs)
                self.assertIs(self.guard._heater_meter_stopped(), expected)
        self.assertEqual(self.calls, [])

    async def test_manual_recovery_quarantines_changed_meter_then_cooldown_clears(self):
        old = example_bindings().power
        old_identity = {key: getattr(old, key) for key in
                        ("heater", "heater_switch", "heater_current", "emergency_ac_switch")}
        await self.start(saved=self.store_claim(owned=True, identity=old_identity))
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertTrue(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertFalse(self.guard.switch_cut_owned)
        await self.refresh(1)
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(62)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_legacy_current_identity_accepts_absent_or_null_watt_key(self):
        old = example_bindings().power
        self.put(old.heater_current, .05, {"unit_of_measurement": "A"})
        identity = {key: getattr(old, key) for key in
                    ("heater", "heater_switch", "heater_current", "emergency_ac_switch")}
        for stored_identity in (identity, {**identity, "heater_power": None}):
            with self.subTest(stored_identity=stored_identity):
                await self.guard.async_close()
                self.guard = power.HeaterPowerGuard(
                    self.hass, old, live=True, entry_id="watt-fixture",
                    notify=lambda: None, wall_clock=lambda: self.wall,
                    clock=lambda: self.wall - self.origin)
                store = Mock(async_load=AsyncMock(return_value=self.store_claim(identity=stored_identity)),
                             async_save=AsyncMock())
                with patch("homeassistant.helpers.storage.Store", return_value=store):
                    await self.guard.async_load()
                self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
                self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
                self.assertEqual(self.guard.diagnostics["heater_meter_kind"], "current")
        self.assertEqual(self.calls, [])

    async def test_pre_recovery_low_watts_cannot_acknowledge_changed_wiring(self):
        identity = {**self.guard._binding_identity(), "heater_power": "sensor.retired_meter"}
        await self.start(saved=self.store_claim(identity=identity))
        await self.refresh(1, omit=(self.b.heater_power,))
        with self.assertRaisesRegex(RuntimeError, "Fresh low heater"):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.calls, [])

    async def test_ordinary_cooldown_needs_post_off_watts_before_safe_interval(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        # The persisted trip has aged out, but runtime Off proof must still
        # come from this request, not the cached pre-request low meter value.
        await self.start(saved=self.store_claim())
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.shutdown_confirmed)
        self.assertIsNone(self.guard._heater_meter_stopped())
        await self.refresh(61, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertIsNone(self.guard._heater_meter_stopped())
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
        # The first trustworthy post-Off watt report starts a NEW safe-load
        # interval; the preceding 61 seconds cannot be credited to recovery.
        await self.refresh(62)
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(121)
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(122)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertNotIn("turn_on", [call[1] for call in self.calls])

    async def test_relay_off_with_expired_meter_does_not_automatically_restore(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(181, omit=(self.b.heater_power, self.b.heater_switch))
        await self.refresh(242, omit=(self.b.heater_power, self.b.heater_switch))
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertNotIn("turn_on", [call[1] for call in self.calls])

    async def test_owned_restore_requires_new_low_watts_after_power_on_and_off_request(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        # A saved claim is not post-start relay proof. A fresh report starts
        # the safe interval; pre-load cached Off cannot backdate that minute.
        await self.refresh(1)
        self.assertNotIn("turn_on", [call[1] for call in self.calls])
        await self.refresh(61, omit=(self.b.heater_switch,))
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertIn(("switch", "turn_on"), [call[:2] for call in self.calls])
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported.timestamp(),
                           self.origin + 61)

    async def test_cached_low_watts_cannot_complete_owned_restore(self):
        self.report_meter_after_off = False
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(1)
        await self.refresh(61, omit=(self.b.heater_switch,))
        self.assertIn(("switch", "turn_on"), [call[:2] for call in self.calls])
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_late_power_on_is_compensated_and_never_left_energized(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(1)
        self.delayed_on = True
        self.wall = self.origin + 61
        task = self.hass.async_create_task(self.guard._restore(), eager_start=False)
        await asyncio.wait_for(self.on_started.wait(), .5)
        await asyncio.sleep(.04)
        self.assertTrue(self.guard.diagnostics["power_restore_inflight"])
        self.assertFalse(await self.guard.async_close())
        self.release_on.set()
        await self.hass.async_block_till_done()
        await asyncio.gather(task, return_exceptions=True)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertFalse(self.guard.diagnostics["power_restore_inflight"])
        self.assertTrue(self.guard.blocked)

    async def test_preview_never_issues_watt_trip_or_restoration_commands(self):
        self.guard.live = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.put(self.b.battery, 40, {"unit_of_measurement": "%"})
        await self.start(listen=True)
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.calls, [])

