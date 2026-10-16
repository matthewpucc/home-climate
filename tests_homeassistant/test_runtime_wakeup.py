"""Readiness transitions must wake control even when user intent is unchanged."""
import unittest

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
