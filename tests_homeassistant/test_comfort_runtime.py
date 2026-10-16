"""Dashboard comfort edits reach the running controller without a reload."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.house_climate.controller.config import Config
from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as control_fixtures


class ComfortRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeTests.asyncTearDown
    handle = native_fixtures.NativeTests.handle
    until = native_fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime

    async def start_live(self):
        # Startup still needs its normal acknowledged off barrier. Fast-forward
        # only those initial polls; a later card edit must wake the 30s worker.
        cfg = replace(Config.from_dict(self.cfg.to_dict()), poll_seconds=30)
        runtime, switch = await self.runtime(cfg=cfg, entry_id="comfort_live")
        runtime.async_start()
        await switch.async_turn_on()
        async with asyncio.timeout(4):
            while not (runtime.status.get("event") == "live"
                       and runtime.status.get("phase") == "running"
                       and self.hass.states.get(cfg.heads["guest"]).state == "cool"):
                runtime.wake_event.set()
                await asyncio.sleep(.01)
        runtime.wake_event.clear()
        await asyncio.sleep(.05)
        return runtime, switch

    def entries_without_configuration_writes(self):
        manager = Mock()
        manager.async_reload = AsyncMock(side_effect=AssertionError("No config entry reload"))
        manager.async_update_entry = Mock(side_effect=AssertionError("No config entry update"))
        return manager

    async def test_target_edit_wakes_live_worker_and_keeps_ownership_and_direction(self):
        runtime, switch = await self.start_live()
        thread, api = runtime.thread, runtime.api
        original_config = runtime.config.to_dict()
        owned = set(runtime.controls.owned)
        entries = self.entries_without_configuration_writes()
        self.calls.clear()

        with patch.object(self.hass, "config_entries", entries):
            await runtime.comfort.async_set_target("guest", 74)
            await self.until(lambda: runtime.status.get("room_targets", {}).get("guest", {}).get("target_f") == 74)
            await self.until(lambda: any(
                domain == "climate" and service == "set_temperature"
                and data["entity_id"] == runtime.config.heads["guest"]
                and data["temperature"] == 74 and data["hvac_mode"] == "cool"
                for domain, service, data in self.calls))
            entries.async_update_entry.assert_not_called()
            entries.async_reload.assert_not_awaited()

        self.assertTrue(switch.is_on)
        self.assertIs(runtime.thread, thread)
        self.assertIs(runtime.api, api)
        self.assertTrue(thread.is_alive())
        self.assertEqual(runtime.config.to_dict(), original_config)
        self.assertEqual(set(runtime.controls.owned), owned)
        self.assertEqual(runtime.fault_count, 0)
        self.assertTrue(runtime.comfort.effective("guest")["hold"])
        self.assertFalse(any(service == "set_hvac_mode" and data.get("hvac_mode") == "off"
                             for _, service, data in self.calls))

    async def test_range_edit_updates_live_plan_and_rejects_invalid_limits_atomically(self):
        runtime, switch = await self.start_live()
        await runtime.comfort.async_set_range("guest", 73.5, 74.5)
        await self.until(lambda: (
            runtime.status.get("room_targets", {}).get("guest", {}).get("low_f"),
            runtime.status.get("room_targets", {}).get("guest", {}).get("high_f")) == (73.5, 74.5))
        before = dict(runtime.comfort.effective("guest"))
        with self.assertRaises(ValueError):
            await runtime.comfort.async_set_range("guest", 65, 76)
        self.assertEqual(runtime.comfort.effective("guest"), before)
        self.assertEqual(runtime.status["room_targets"]["guest"]["low_f"], 73.5)
        self.assertEqual(runtime.status["room_targets"]["guest"]["high_f"], 74.5)
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)

    async def test_hard_upper_guard_still_cools_when_desired_target_is_at_limit(self):
        runtime, switch = await self.start_live()
        await runtime.comfort.async_set_range("guest", 74, 75)
        await runtime.comfort.async_set_target("guest", 75)
        await self.until(lambda: runtime.status.get("room_targets", {}).get("guest", {}).get("target_f") == 75)
        target = runtime.status["room_targets"]["guest"]
        self.assertEqual(target["high_f"], runtime.config.high_f)
        self.assertTrue(target["cool_requested"])
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 73.5)
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)

    async def test_shadow_edit_wakes_preview_without_any_actuator_calls(self):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), poll_seconds=30)
        runtime, switch = await self.runtime(live=False, cfg=cfg, entry_id="comfort_shadow")
        original_config = runtime.config.to_dict()
        runtime.async_start()
        await self.until(lambda: runtime.status.get("event") == "shadow"
                         and "guest" in runtime.status.get("room_targets", {}))
        await runtime.comfort.async_set_target("guest", 74)
        await runtime.comfort.async_set_range("guest", 73.5, 74.5)
        await self.until(lambda: runtime.status.get("room_targets", {}).get("guest", {}).get("target_f") == 74
                         and runtime.status["room_targets"]["guest"]["low_f"] == 73.5
                         and runtime.status["room_targets"]["guest"]["high_f"] == 74.5)
        self.assertEqual(runtime.status["event"], "shadow")
        self.assertEqual(runtime.status["room_targets"]["guest"]["low_f"], 73.5)
        self.assertEqual(runtime.status["room_targets"]["guest"]["high_f"], 74.5)
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 74)
        self.assertFalse(switch.is_on)
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(runtime.config.to_dict(), original_config)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
