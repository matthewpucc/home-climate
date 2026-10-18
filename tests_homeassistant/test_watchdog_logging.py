"""Forced global shutdown causes must be visible once in ordinary HA logs."""
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.helpers.storage import Store

from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as control_fixtures


LOGGER = "custom_components.house_climate.controls"


class WatchdogLoggingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = control_fixtures.NativeControlTests.runtime
    claim = control_fixtures.NativeControlTests.claim
    settle_watchdog = control_fixtures.NativeControlTests.settle_watchdog

    async def running_devices(self, runtime):
        for entity in runtime.config.climate_entities:
            old = self.hass.states.get(entity)
            mode = "heat" if entity == runtime.config.heater_entity else "cool"
            self.hass.states.async_set(entity, mode, dict(old.attributes))
        for entity in runtime.config.fans.values():
            old = self.hass.states.get(entity)
            self.hass.states.async_set(entity, "on", {**old.attributes, "percentage": 60})
        await self.settle_watchdog(runtime.controls)
        return set(runtime.controls.owned)

    def assert_all_off(self, expected):
        self.assertTrue(expected)
        for entity in expected:
            self.assertEqual(self.hass.states.get(entity).state, "off", entity)
        off_calls = {
            data["entity_id"] for domain, service, data in self.calls
            if ((domain == "climate" and service == "set_hvac_mode" and data["hvac_mode"] == "off")
                or (domain == "fan" and service == "turn_off"))
        }
        self.assertEqual(off_calls, expected)

    async def expire(self, controls, age=130):
        # Patch only the controller's clock, not asyncio or HA's scheduling.
        clock = SimpleNamespace(monotonic=lambda: controls.heartbeat_monotonic + age)
        with patch("custom_components.house_climate.controls.time", clock):
            await controls.async_check()
            await self.settle_watchdog(controls)

    async def test_expired_lease_logs_cause_age_and_full_scope_once_while_stopping_all_devices(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        expected = await self.running_devices(runtime)
        self.calls.clear()
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            await self.expire(runtime.controls)
            for _ in range(3):
                await runtime.controls.async_check()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        message = captured.records[0].getMessage()
        self.assertIn("controller heartbeat expired", message)
        self.assertIn("age 130.0s", message)
        self.assertIn("lease limit 120s", message)
        self.assertIn(f"stopping all {len(expected)} owned devices", message)
        for entity in expected:
            self.assertIn(entity, message)
        self.assertFalse(runtime.controls.enabled)
        self.assertFalse(runtime.controls.owned)
        self.assertIn("heartbeat expired", runtime.controls.problem)
        self.assert_all_off(expected)

    async def test_new_expiry_after_rearming_logs_a_new_incident(self):
        runtime, _ = await self.runtime()
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            for age in (130, 140):
                await self.claim(runtime)
                expected = await self.running_devices(runtime)
                self.calls.clear()
                await self.expire(runtime.controls, age)
                self.assert_all_off(expected)
        self.assertEqual(len(captured.records), 2)
        self.assertIn("age 130.0s", captured.records[0].getMessage())
        self.assertIn("age 140.0s", captured.records[1].getMessage())
        self.assertTrue(all(record.levelno == logging.WARNING for record in captured.records))

    async def test_worker_pause_logs_reason_and_scope_once_until_rearmed(self):
        runtime, _ = await self.runtime()
        reason = "Control paused: controller worker stopped after unexpected exit"
        for _ in range(2):
            await self.claim(runtime)
            expected = await self.running_devices(runtime)
            self.calls.clear()
            with self.assertLogs(LOGGER, level="WARNING") as captured:
                runtime.controls.pause(reason)
                runtime.controls.pause(reason)
                await self.settle_watchdog(runtime.controls)
                runtime.controls.pause(reason)
                await self.settle_watchdog(runtime.controls)
            self.assertEqual(len(captured.records), 1)
            self.assertEqual(captured.records[0].levelno, logging.WARNING)
            message = captured.records[0].getMessage()
            self.assertIn(reason, message)
            self.assertIn(f"stopping all {len(expected)} owned devices", message)
            for entity in expected:
                self.assertIn(entity, message)
            self.assertEqual(runtime.controls.problem, reason)
            self.assertFalse(runtime.controls.enabled)
            self.assert_all_off(expected)

    async def test_manual_disable_with_old_heartbeat_is_not_reported_as_watchdog_failure(self):
        runtime, switch = await self.runtime()
        await self.claim(runtime)
        expected = await self.running_devices(runtime)
        runtime.controls.heartbeat_monotonic -= 130
        self.calls.clear()
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await switch.async_turn_off()
            await self.settle_watchdog(runtime.controls)
            await runtime.controls.async_check()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.INFO)
        message = captured.records[0].getMessage()
        self.assertIn("Controller disabled", message)
        self.assertNotIn("heartbeat expired", message)
        self.assertIn(f"stopping all {len(expected)} owned devices", message)
        self.assertFalse(runtime.controls.enabled)
        self.assertIsNone(runtime.controls.problem)
        self.assert_all_off(expected)

    async def test_restored_ownership_logs_original_entity_scope_at_info(self):
        old_head, old_fan = "climate.previous_head", "fan.previous_purifier"
        expected = {old_head, old_fan}
        self.hass.states.async_set(old_head, "heat", {})
        self.hass.states.async_set(old_fan, "on", {"percentage": 60})
        store = Store(self.hass, 1, "house_climate.restored_log.ownership")
        await store.async_save({"devices": [old_head, old_fan]})
        self.calls.clear()
        with self.assertLogs(LOGGER, level="INFO") as captured:
            runtime, _ = await self.runtime(entry_id="restored_log")
            await self.settle_watchdog(runtime.controls)
            await runtime.controls.async_check()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.INFO)
        message = captured.records[0].getMessage()
        self.assertIn("Recovering previously owned devices", message)
        self.assertIn("stopping all 2 owned devices", message)
        for entity in expected:
            self.assertIn(entity, message)
        self.assertFalse(runtime.controls.owned)
        self.assert_all_off(expected)

    async def test_pending_fan_save_failure_logs_global_stop_scope_once_across_retries(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        expected = await self.running_devices(runtime)
        old_fan = "fan.previous_purifier"
        self.hass.states.async_set(old_fan, "off", {})
        # A previous mapping's optional cleanup obligation coexists with a live
        # critical lease. Losing the ability to persist its completion disables
        # control and stops that lease's devices on the next watchdog pass.
        runtime.controls.pending_fans = (old_fan,)
        await runtime.controls._save(runtime.controls.owned, runtime.controls.pending_fans)
        self.calls.clear()
        with patch.object(runtime.controls.store, "async_save", side_effect=OSError("disk full")):
            with self.assertLogs(LOGGER, level="ERROR") as captured:
                for _ in range(3):
                    await runtime.controls.async_check()
                await runtime.controls.async_set_enabled(False)
                await self.settle_watchdog(runtime.controls)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn("Could not save controller shutdown: disk full", message)
        self.assertIn(f"stopping all {len(expected)} owned devices", message)
        for entity in expected:
            self.assertIn(entity, message)
        self.assertFalse(runtime.controls.enabled)
        self.assertTrue(runtime.controls.recovering)
        self.assertEqual(set(runtime.controls.owned), expected)
        self.assert_all_off(expected)
        await runtime.controls.async_check()
        self.assertFalse(runtime.controls.owned)

    async def test_fresh_local_heartbeat_needs_no_new_temperature_reports_or_warning(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        expected = await self.running_devices(runtime)
        reports = {
            probe.entity_id: self.hass.states.get(probe.entity_id).last_reported
            for room in runtime.config.zones.values() for probe in room.probes
        }
        self.calls.clear()
        clock = SimpleNamespace(monotonic=lambda: 1000)
        with self.assertNoLogs(LOGGER, level="WARNING"):
            with patch("custom_components.house_climate.controls.time", clock):
                await runtime.controls.async_service(
                    "input_datetime", "set_datetime",
                    {"entity_id": runtime.config.entity("heartbeat", "input_datetime")},
                )
                await runtime.controls.async_check()
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(set(runtime.controls.owned), expected)
        self.assertEqual(self.calls, [])
        for entity, reported in reports.items():
            self.assertEqual(self.hass.states.get(entity).last_reported, reported)
