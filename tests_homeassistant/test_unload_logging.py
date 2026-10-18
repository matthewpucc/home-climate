"""Deferred unload logs expose blockers without changing shutdown semantics."""
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.storage import Store

from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as control_fixtures


LOGGER = "custom_components.house_climate.runtime"


class UnloadLoggingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeTests.asyncTearDown
    handle = native_fixtures.NativeTests.handle
    until = native_fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime
    claim = control_fixtures.NativeControlTests.claim
    settle_watchdog = control_fixtures.NativeControlTests.settle_watchdog

    async def blocked_runtime(self):
        runtime, _ = await self.runtime(entry_id="blocked_unload")
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head = runtime.config.heads["guest"]

        async def unacknowledged_off(call):
            if call.data["entity_id"] == head:
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", unacknowledged_off)
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, "unavailable", dict(old.attributes))
        return runtime, head

    async def unblock(self, runtime, head):
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, "off", dict(old.attributes))
        self.assertTrue(await runtime.async_stop())

    async def test_unconfirmed_owned_head_warns_once_and_does_not_create_controller_fault(self):
        runtime, head = await self.blocked_runtime()
        try:
            with self.assertLogs(LOGGER, level="WARNING") as captured:
                self.assertFalse(await runtime.async_stop())
                self.assertFalse(await runtime.async_stop())
            records = [record for record in captured.records
                       if "Controller unload deferred" in record.getMessage()]
            self.assertEqual(len(records), 1)
            message = records[0].getMessage()
            self.assertIn(head, message)
            self.assertIn("Off acknowledgement", message)
            self.assertIn("in-flight activations=0", message)
            self.assertIn("owning integration reload pending=false", message.lower())
            self.assertEqual(runtime.fault_count, 0)
            self.assertNotEqual(runtime.status["event"], "worker_failed")
            self.assertIn(head, runtime.controls.owned)
            self.assertTrue(runtime.controls.active)
        finally:
            await self.unblock(runtime, head)

    async def test_pending_circulation_fan_alone_does_not_warn_or_block_unload(self):
        fan = self.cfg.fans["office"]

        async def no_fan_feedback(call):
            self.calls.append((call.domain, call.service, dict(call.data)))

        self.hass.services.async_register("fan", "turn_off", no_fan_feedback)
        old = self.hass.states.get(fan)
        self.hass.states.async_set(fan, "on", {**old.attributes, "percentage": 60})
        store = Store(self.hass, 1, "house_climate.fan_only_unload.ownership")
        await store.async_save({"devices": [], "pending_fans": [fan]})
        runtime, _ = await self.runtime(live=False, entry_id="fan_only_unload")
        try:
            with patch("custom_components.house_climate.runtime._LOGGER.warning") as warning:
                self.assertTrue(await runtime.async_stop())
            warning.assert_not_called()
            self.assertFalse(runtime.controls.owned)
            self.assertFalse(runtime.controls.active)
            self.assertEqual(runtime.controls.pending_fans, (fan,))
            self.assertEqual((await store.async_load())["pending_fans"], [fan])
            self.assertEqual(runtime.fault_count, 0)
        finally:
            self.hass.services.async_register("fan", "turn_off", self.handle)
            old = self.hass.states.get(fan)
            self.hass.states.async_set(fan, "off", dict(old.attributes))

    async def test_owning_integration_reload_blocker_is_included_in_unload_warning(self):
        runtime, _ = await self.runtime(entry_id="reload_unload")
        with patch.object(runtime.recovery, "async_close", new=AsyncMock(return_value=False)):
            with self.assertLogs(LOGGER, level="WARNING") as captured:
                self.assertFalse(await runtime.async_stop())
        records = [record for record in captured.records
                   if "Controller unload deferred" in record.getMessage()]
        self.assertEqual(len(records), 1)
        self.assertIn("owning integration reload pending=true", records[0].getMessage().lower())
        # This pre-existing failure remains a worker-failed report; only an
        # unconfirmed device Off is deliberately kept out of fault_count.
        self.assertEqual(runtime.status["event"], "worker_failed")
        self.assertEqual(runtime.fault_count, 1)

    async def test_failed_warning_output_cannot_change_deferred_unload_result(self):
        runtime, head = await self.blocked_runtime()
        try:
            with patch("custom_components.house_climate.runtime._LOGGER.warning",
                       side_effect=RuntimeError("logging output failed")) as warning:
                self.assertFalse(await runtime.async_stop())
            warning.assert_called_once()
            self.assertIn(head, runtime.controls.owned)
            self.assertTrue(runtime.controls.active)
            self.assertEqual(runtime.fault_count, 0)
        finally:
            await self.unblock(runtime, head)


if __name__ == "__main__":
    unittest.main()
