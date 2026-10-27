"""Time-only environmental transitions fence actual queued integration reloads."""
import asyncio
from dataclasses import replace
from math import inf, nan
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.house_climate.controller.config import Config, Probe
from custom_components.house_climate.controller.protection import ProtectionTemperatures
from custom_components.house_climate.runtime import ControllerRuntime
from tests_homeassistant import test_capability_repair_flow as flow
from tests_homeassistant import test_native as fixtures


class EnvironmentInputTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    async def test_native_current_weather_and_expiry_gate_ignore_forecast_noise(self):
        weather = "weather.fixture_current_outdoor"
        cfg = replace(Config.from_dict(self.cfg.to_dict()), protection=ProtectionTemperatures(),
                      outdoor_probes=(Probe(weather, "fixture_weather_station"),))
        attrs = {"temperature": 50, "temperature_unit": "°F"}
        self.hass.states.async_set(weather, "sunny", attrs)
        runtime = ControllerRuntime(self.hass, cfg, False)
        capability = runtime.recovery.capabilities
        capability.status = {"recovery_inputs": capability.input_signature(),
                             "decision_valid_until_monotonic": time.monotonic() + 60}
        self.assertIn(weather, runtime.api.entity_ids)
        self.assertTrue(capability.fresh_inputs())
        self.hass.states.async_set(weather, "sunny", {**attrs, "forecast": [{"temperature": -40}]})
        self.assertTrue(capability.fresh_inputs(), "A forecast array changed a current-temperature decision")
        self.hass.states.async_set(weather, "sunny", {**attrs, "temperature": 30})
        self.assertFalse(capability.fresh_inputs(), "Current weather attributes were absent from native input fencing")
        self.assertEqual(self.calls, [])

    async def test_expired_or_invalid_timer_deadline_is_not_a_fresh_plan(self):
        runtime = ControllerRuntime(self.hass, self.cfg, False)
        capability = runtime.recovery.capabilities
        marker = capability.input_signature()
        for deadline in (time.monotonic() - .1, True, False, nan, inf, "later"):
            with self.subTest(deadline=deadline):
                capability.status = {"recovery_inputs": marker, "decision_valid_until_monotonic": deadline}
                self.assertFalse(capability.fresh_inputs())
        capability.status = {"recovery_inputs": marker, "decision_valid_until_monotonic": None}
        self.assertTrue(capability.fresh_inputs())
        self.assertEqual(self.calls, [])

    async def test_opposite_missing_mode_uses_actual_emergency_magnitude_and_weather_tie(self):
        runtime = ControllerRuntime(self.hass, self.cfg, False)
        capability = runtime.recovery.capabilities
        row = SimpleNamespace(entity=self.cfg.heads["guest"], missing=frozenset({"heat"}))
        for amount, opposing, tie, expected in ((2, 1, "cool", True), (1, 2, "heat", False),
                                                 (2, 2, "heat", True), (2, 2, "cool", False)):
            with self.subTest(amount=amount, opposing=opposing, tie=tie):
                capability.status = {
                    "mode": "cool", "unavailable_mode_requests": {
                        "guest": {"heat": {"emergency": True, "violation_f": amount}}},
                    "protection_context": {"enabled": True, "tie_mode": tie,
                        "emergencies": {"warm_room": {"cool": {"active": True, "violation_f": opposing}}}},
                }
                self.assertEqual(capability.required(row), expected)
        capability.status["unavailable_mode_requests"]["guest"]["heat"]["emergency"] = False
        self.assertFalse(capability.required(row), "Ordinary opposite comfort demand became a disruptive repair")
        self.assertEqual(self.calls, [])


class EnvironmentRepairQueueTests(unittest.IsolatedAsyncioTestCase):
    # Reuse real HA registries/ownership/services, not the old class's test cases.
    asyncSetUpBase = flow.CapabilityRepairFlowTests.asyncSetUpBase
    asyncTearDownBase = flow.CapabilityRepairFlowTests.asyncTearDownBase
    asyncSetUp = flow.CapabilityRepairFlowTests.asyncSetUp
    asyncTearDown = flow.CapabilityRepairFlowTests.asyncTearDown
    handle = flow.CapabilityRepairFlowTests.handle
    until = flow.CapabilityRepairFlowTests.until
    runtime = flow.CapabilityRepairFlowTests.runtime
    claim = flow.CapabilityRepairFlowTests.claim
    settle_watchdog = flow.CapabilityRepairFlowTests.settle_watchdog
    entry = flow.CapabilityRepairFlowTests.entry
    register = flow.CapabilityRepairFlowTests.register
    publish = flow.CapabilityRepairFlowTests.publish
    refresh = flow.CapabilityRepairFlowTests.refresh
    status = flow.CapabilityRepairFlowTests.status
    worker_reports = flow.CapabilityRepairFlowTests.worker_reports
    observe = flow.CapabilityRepairFlowTests.observe
    learn = flow.CapabilityRepairFlowTests.learn
    fail_rechecks = flow.CapabilityRepairFlowTests.fail_rechecks
    start_repair = flow.CapabilityRepairFlowTests.start_repair
    finish_repair = flow.CapabilityRepairFlowTests.finish_repair
    actuator_calls = flow.CapabilityRepairFlowTests.actuator_calls

    async def test_expired_deadline_prevents_new_disruptive_admission_without_raw_changes(self):
        await self.learn()
        status = {**self.status(), "decision_valid_until_monotonic": time.monotonic() - 1}
        await self.fail_rechecks(status=status)
        self.calls.clear()
        await self.observe(self.clock(), status)
        self.assertIsNone(self.manager._task)
        self.assertIsNone(self.manager.capabilities.lease)
        self.assertEqual(self.actuator_calls(), [])
        self.reload.assert_not_awaited()

    async def test_timer_expiring_while_reload_is_queued_cancels_and_safely_rejoins(self):
        await self.learn()
        entered, release = asyncio.Event(), asyncio.Event()
        capability = self.manager.capabilities
        original = capability._async_reload_checked

        async def queued(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        repair = None
        with patch.object(capability, "_async_reload_checked", side_effect=queued):
            try:
                await self.fail_rechecks()
                repair = await self.start_repair()
                await asyncio.wait_for(entered.wait(), 1)
                before = capability.input_signature()
                capability.status["decision_valid_until_monotonic"] = time.monotonic() + .02
                self.assertTrue(capability.fresh_inputs())
                await asyncio.sleep(.04)
                self.assertEqual(capability.input_signature(), before)
                self.assertFalse(capability.fresh_inputs())
                release.set()
                await self.until(lambda: self.manager.diagnostics.get("capability_repair", {}).get("phase") == "minimum_off")
                self.reload.assert_not_awaited()
                self.assertEqual(self.manager._attempts.get(self.owner.entry_id, 0), 0)
                self.assertTrue(all(self.hass.states.get(entity).state == "off" for entity in self.scope))
                self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.assertFalse(await self.finish_repair(repair))
                self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
            finally:
                release.set()
                if repair and not repair.done():
                    repair.cancel()
                    await asyncio.gather(repair, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
