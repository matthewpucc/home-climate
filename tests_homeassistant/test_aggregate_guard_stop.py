"""Native aggregate stop evidence uses real HA reports without commanding shared AC."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.aggregate_stop import AggregateStopConfig
from custom_components.house_climate.controller.bindings import example_bindings


BASE = example_bindings().power
SHARED_AC = "switch.aggregate_fixture_ac_output"


class NativeAggregateGuardStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.calls = []
        self.guard = None
        self.ignore_climate = False
        self.output_reports = [180, 185]
        self.report_delay = .015
        self.force_reports = True
        self.report_tasks = []
        self.report_times = []
        self.two_reports_sent = asyncio.Event()
        self.release_third = asyncio.Event()
        self.pause_after_two = False
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for patcher in self.patchers:
            patcher.start()
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        for service in ("turn_off", "turn_on"):
            self.hass.services.async_register("switch", service, self.handle)
        for entity, value, unit in (
            (BASE.circuit_power, 140, "W"), (BASE.input_power, 130, "W"),
            (BASE.output_power, 1000, "W"), (BASE.battery, 80, "%"),
            (BASE.wall_phase_a_voltage, 124, "V"),
            (BASE.wall_phase_b_voltage, 123, "V"),
        ):
            self.hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})
        self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(SHARED_AC, "on")
        # These disconnected legacy entities deliberately exist. They are not
        # part of this direct path and cannot confirm the real heater is Off.
        self.hass.states.async_set(BASE.heater_switch, "off")
        self.hass.states.async_set(BASE.heater_current, "0", {"unit_of_measurement": "A"})

    async def asyncTearDown(self):
        self.release_third.set()
        if self.guard is not None:
            await self.guard.async_close()
        await self.hass.async_block_till_done()
        if self.guard is not None:
            await self.guard.async_close()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()
        for patcher in reversed(self.patchers):
            patcher.stop()

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "climate":
            if not self.ignore_climate:
                self.hass.states.async_set(BASE.heater, "off", {"hvac_action": "off"},
                                           force_update=True)
            # Meter reports arrive separately after the service has completed.
            # Service completion therefore cannot stand in for measured evidence.
            if self.output_reports:
                task = self.hass.async_create_task(self.emit_output_reports())
                self.report_tasks.append(task)
            return
        self.assertEqual(call.data["entity_id"], SHARED_AC)
        self.assertEqual(call.service, "turn_off", "Shared AC must never be automatically restored")
        self.hass.states.async_set(SHARED_AC, "off", force_update=True)
        self.hass.states.async_set(BASE.heater, "unavailable")

    async def emit_output_reports(self):
        for index, watts in enumerate(self.output_reports):
            await asyncio.sleep(self.report_delay)
            self.hass.states.async_set(BASE.output_power, str(watts),
                                       {"unit_of_measurement": "W"},
                                       force_update=self.force_reports)
            state = self.hass.states.get(BASE.output_power)
            self.report_times.append(state.last_reported.timestamp())
            if index == 1:
                self.two_reports_sent.set()
                if self.pause_after_two:
                    await self.release_third.wait()

    async def start(self, *, minimum_samples=2, interval_seconds=None):
        policy = AggregateStopConfig(
            minimum_drop_w=500, residual_ceiling_w=300, noise_margin_w=20,
            settle_seconds=.01, timeout_seconds=.1, max_age_seconds=30,
            minimum_samples=minimum_samples)
        bindings = replace(BASE, heater_switch=None, heater_current=None,
                           emergency_ac_switch=SHARED_AC,
                           emergency_ac_fallback_enabled=True,
                           aggregate_stop_confirmation=policy)
        self.guard = power.HeaterPowerGuard(
            self.hass, bindings, live=True, entry_id="aggregate-stop-native-test",
            notify=lambda: None)
        await self.guard.async_load()
        if interval_seconds is None:
            self.guard.async_start()
        else:
            with patch.object(power, "CHECK_SECONDS", interval_seconds):
                self.guard.async_start()
        await self.hass.async_block_till_done()
        return self.guard

    async def trip(self, *, action="heating"):
        self.hass.states.async_set(BASE.heater, "heat", {"hvac_action": action})
        # Use the existing electrical input budget, not a new temperature rule.
        self.hass.states.async_set(BASE.input_power, "1500", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()

    async def wait_for(self, predicate, timeout=1):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.005)

    def assert_native_only(self, guard):
        self.assertEqual(self.hass.states.get(BASE.heater).state, "off")
        self.assertEqual(self.hass.states.get(SHARED_AC).state, "on")
        self.assertFalse(guard.diagnostics["emergency_ac_latched"])
        self.assertTrue(self.calls)
        self.assertTrue(all(domain == "climate" for domain, _, _ in self.calls))

    async def test_two_fresh_output_reports_corroborate_native_off(self):
        guard = await self.start()
        await self.trip()
        await self.wait_for(lambda: guard.diagnostics["shutdown_confirmation"] == "aggregate_corroborated")
        self.assert_native_only(guard)
        outcome = guard.diagnostics["aggregate_stop"]
        self.assertEqual((outcome["status"], outcome["classification"], outcome["sample_count"]),
                         ("corroborated", "drop", 2))
        self.assertEqual(len(set(self.report_times)), 2)
        self.assertGreaterEqual(outcome["evidence_seconds"], .01)

    async def test_three_reports_are_required_when_configured(self):
        self.output_reports = [180, 185, 180]
        self.pause_after_two = True
        guard = await self.start(minimum_samples=3)
        task = asyncio.create_task(self.trip())
        try:
            await asyncio.wait_for(self.two_reports_sent.wait(), timeout=1)
            await asyncio.sleep(.005)
            outcome = guard.diagnostics["aggregate_stop"]
            self.assertEqual(outcome["sample_count"], 2)
            self.assertNotEqual(outcome["status"], "corroborated")
            self.assertNotEqual(guard.diagnostics["shutdown_confirmation"], "aggregate_corroborated")
        finally:
            self.release_third.set()
            await task
        await self.wait_for(lambda: guard.diagnostics["shutdown_confirmation"] == "aggregate_corroborated")
        self.assertEqual(guard.diagnostics["aggregate_stop"]["sample_count"], 3)
        self.assertEqual(len(set(self.report_times)), 3)
        self.assert_native_only(guard)

    async def test_unchanged_reports_advance_via_real_guard_interval(self):
        self.output_reports = [180, 180]
        self.report_delay = .02
        self.force_reports = False
        guard = await self.start(interval_seconds=.005)
        await self.trip()
        await self.wait_for(lambda: guard.diagnostics["shutdown_confirmation"] == "aggregate_corroborated")
        self.assertEqual(len(set(self.report_times)), 2)
        self.assertEqual(guard.diagnostics["aggregate_stop"]["sample_count"], 2)
        self.assert_native_only(guard)

    async def test_high_other_load_keeps_state_only_without_global_cut(self):
        self.output_reports = [700, 705]
        guard = await self.start(interval_seconds=.005)
        await self.trip()
        await self.wait_for(lambda: guard.diagnostics["aggregate_stop"]["status"] == "unknown")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIn("timed out", guard.diagnostics["aggregate_stop"]["reason"])
        self.assert_native_only(guard)

    async def test_stale_baseline_keeps_fresh_native_off_state_only(self):
        self.output_reports = []
        guard = await self.start()
        cached = self.hass.states.get(BASE.output_power)
        old = datetime.now(timezone.utc) - timedelta(seconds=31)
        cached.last_reported = cached.last_updated = cached.last_changed = old
        await self.trip()
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertEqual(guard.diagnostics["aggregate_stop"]["status"], "unknown")
        self.assertIn("stale", guard.diagnostics["aggregate_stop"]["reason"])
        self.assert_native_only(guard)

    async def test_noisy_low_reports_do_not_provoke_global_cut(self):
        self.output_reports = [180, 240, 190, 250]
        guard = await self.start(interval_seconds=.005)
        await self.trip()
        await self.wait_for(lambda: guard.diagnostics["aggregate_stop"]["status"] == "unknown")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIn("timed out", guard.diagnostics["aggregate_stop"]["reason"])
        self.assert_native_only(guard)

    async def test_no_post_command_reports_time_out_as_state_only(self):
        self.output_reports = []
        guard = await self.start(interval_seconds=.005)
        await self.trip()
        await self.wait_for(lambda: guard.diagnostics["aggregate_stop"]["status"] == "unknown")
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIn("timed out", guard.diagnostics["aggregate_stop"]["reason"])
        self.assertEqual(guard.diagnostics["aggregate_stop"]["sample_count"], 0)
        self.assert_native_only(guard)

    async def test_other_load_drop_without_native_off_does_not_corroborate(self):
        self.ignore_climate = True
        self.output_reports = [180, 185, 180]
        self.report_delay = .005
        guard = await self.start(minimum_samples=3)
        await self.trip()
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(guard.diagnostics["shutdown_confirmation"], "emergency_ac_off")
        self.assertNotEqual(guard.diagnostics["aggregate_stop"]["status"], "corroborated")
        self.assertIsNone(guard.diagnostics["aggregate_stop"]["classification"])
        cuts = [call for call in self.calls if call[0] == "switch"]
        self.assertEqual(cuts, [("switch", "turn_off", {"entity_id": SHARED_AC})])

    async def test_already_idle_output_has_its_own_confirmation_classification(self):
        self.output_reports = [148, 150]
        self.hass.states.async_set(BASE.output_power, "150", {"unit_of_measurement": "W"},
                                   force_update=True)
        guard = await self.start()
        await self.trip(action="idle")
        await self.wait_for(lambda: guard.diagnostics["shutdown_confirmation"] == "aggregate_corroborated")
        outcome = guard.diagnostics["aggregate_stop"]
        self.assertEqual(outcome["classification"], "already_low")
        self.assertEqual((outcome["baseline_w"], outcome["sample_count"]), (150, 2))
        self.assert_native_only(guard)


if __name__ == "__main__":
    unittest.main()
