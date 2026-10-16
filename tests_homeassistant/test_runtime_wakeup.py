"""Readiness transitions must wake control even when user intent is unchanged."""
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.runtime import ControllerRuntime
from tests_homeassistant import test_native as fixtures


class ReadinessWakeupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    async def test_recovered_ownership_wakes_enabled_worker(self):
        runtime = ControllerRuntime(self.hass, Config.from_dict(self.cfg.to_dict()), True)
        self.runtimes.append(runtime)
        await runtime.async_setup_controls("readiness_wakeup")
        controls = runtime.controls
        # The user still wants control; only the shutdown recovery interlock changes.
        controls.enabled = True
        controls.recovering = True
        controls._publish()
        runtime.wake_event.clear()
        controls.recovering = False
        controls._publish()
        self.assertTrue(runtime.wake_event.is_set())
        self.assertTrue(controls.enabled)
        self.assertIsNone(runtime.status["control_block_reason"])

    async def test_pending_integration_reload_defers_unload_but_still_stops_controls(self):
        runtime = ControllerRuntime(self.hass, Config.from_dict(self.cfg.to_dict()), True)
        self.runtimes.append(runtime)
        await runtime.async_setup_controls("reload_shutdown")
        with patch.object(runtime.recovery, "async_close", new=AsyncMock(return_value=False)):
            with patch.object(runtime.controls, "async_set_enabled", new=AsyncMock()) as disable:
                with patch.object(runtime.controls, "async_close", new=AsyncMock(return_value=True)) as close:
                    with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
                        self.assertFalse(await runtime.async_stop())
        disable.assert_awaited_once_with(False)
        close.assert_awaited_once()
        self.assertTrue(runtime.stop_event.is_set())
        self.assertIn("unload deferred", runtime.status["error"])
