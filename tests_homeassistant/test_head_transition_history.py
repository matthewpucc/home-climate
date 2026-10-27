"""Native worker transition evidence reaches HA diagnostic entities."""
import unittest

from custom_components.house_climate.sensor import ControllerStatus, RecoveryStatus
from tests_homeassistant import test_call_targets as calls
from tests_homeassistant import test_native as fixtures


class HeadTransitionNativeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = calls.CallTargetTests.runtime
    start_biased_control = calls.CallTargetTests.start_biased_control

    async def test_command_and_device_evidence_survives_native_worker_and_clean_stop(self):
        runtime, switch, entry = await self.start_biased_control()
        head = self.cfg.heads["guest"]
        # Device state can update before the worker publishes its receipt.
        await self.until(lambda: any(event["event"] == "command_acknowledged"
                                    and event.get("observed_mode") == "cool"
                                    for event in runtime.status.get("head_transition_history", {}).get(head, ())))
        history = runtime.status["head_transition_history"][head]
        requested = next(event for event in history if event["event"] == "command_requested"
                         and event["service"] == "set_temperature")
        acknowledged = next(event for event in history if event["event"] == "command_acknowledged"
                            and event["command_id"] == requested["sequence"])
        self.assertLess(requested["sequence"], acknowledged["sequence"])
        self.assertEqual(acknowledged["observed_mode"], "cool")
        for entity in (ControllerStatus(entry), RecoveryStatus(entry)):
            self.assertEqual(entity.extra_state_attributes["head_transition_history"],
                             runtime.status["head_transition_history"])
        await switch.async_turn_off()
        await self.until(lambda: runtime.status.get("event") == "shadow"
                         and self.hass.states.get(head).state == "off")
        history = runtime.status["head_transition_history"][head]
        self.assertTrue(any(event["event"] == "command_requested"
                            and event["service"] == "set_hvac_mode"
                            and event["data"] == {"hvac_mode": "off"} for event in history))
        self.assertTrue(runtime.status["head_stop_reasons"][head]["controller_off_requested"])
        self.assertEqual(runtime.fault_count, 0)
