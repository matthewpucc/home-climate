"""Native worker maintains safety and its lease between comfort decisions."""
import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.policy import Policy
from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as control_fixtures


class ControlCadenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeTests.asyncTearDown
    handle = native_fixtures.NativeTests.handle
    until = native_fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime

    def temperatures(self, room_id, value):
        for probe in self.cfg.zones[room_id].probes:
            old = self.hass.states.get(probe.entity_id)
            self.hass.states.async_set(
                probe.entity_id, str(value), dict(old.attributes))

    async def start_control(self, *, living=68):
        # This initial reserve-zone call is already known to cadence. Losing
        # its probes and restoring the same readings must not create a new
        # protection edge or bypass the decision interval.
        self.temperatures("guest", 74.8)
        self.temperatures("living", living)
        self.decision_times = []
        original = Policy.decide

        def record_decision(policy, *args, **kwargs):
            self.decision_times.append(time.monotonic())
            return original(policy, *args, **kwargs)

        tracker = patch.object(Policy, "decide", new=record_decision)
        tracker.start()
        self.addCleanup(tracker.stop)
        cfg = replace(Config.from_dict(self.cfg.to_dict()),
                      poll_seconds=.03, decision_interval_seconds=.6,
                      head_minimum_run_seconds=0, head_minimum_off_seconds=0,
                      off_settle_seconds=.04)
        runtime, switch = await self.runtime(cfg=cfg, entry_id="cadence")
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: (
            runtime.status.get("event") == "live"
            and runtime.status.get("phase") == "running"
            and self.hass.states.get(cfg.heads["guest"]).state == "cool"
            and (living >= 67.5 or self.hass.states.get(cfg.heater_entity).state == "heat")))
        # Start each scenario just after a full decision, leaving most of the
        # 600ms interval available for maintenance-only assertions.
        if time.monotonic() - self.decision_times[-1] >= .1:
            count = len(self.decision_times)
            await self.until(lambda: len(self.decision_times) > count)
        self.calls.clear()
        return runtime, switch

    async def assert_stays(self, runtime, predicate, seconds=.18, *, wake=False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.assertTrue(predicate())
            if wake:
                runtime.wake_event.set()
            await asyncio.sleep(.01)
        self.assertTrue(predicate())

    async def test_wake_flood_keeps_comfort_call_until_decision_and_renews_lease(self):
        runtime, switch = await self.start_control()
        head = runtime.config.heads["guest"]
        count = len(self.decision_times)
        heartbeat = runtime.controls.heartbeat_monotonic
        self.temperatures("guest", 72)

        await self.assert_stays(
            runtime, lambda: self.hass.states.get(head).state == "cool", wake=True)
        self.assertEqual(len(self.decision_times), count)
        self.assertGreater(runtime.controls.heartbeat_monotonic, heartbeat)
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)
        self.assertFalse(any(
            service == "set_hvac_mode" and data["entity_id"] == head
            and data["hvac_mode"] == "off" for _, service, data in self.calls))

        await self.until(lambda: self.hass.states.get(head).state == "off")
        self.assertGreater(len(self.decision_times), count)
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)
        self.assertEqual(runtime.fault_count, 0)

    async def test_dashboard_target_edit_replans_before_next_comfort_tick(self):
        runtime, switch = await self.start_control()
        head = runtime.config.heads["guest"]
        count = len(self.decision_times)
        worker = runtime.thread
        started = time.monotonic()
        await runtime.comfort.async_set_target("guest", 74)
        async with asyncio.timeout(.25):
            while not (
                runtime.status.get("room_targets", {}).get("guest", {}).get("target_f") == 74
                and self.hass.states.get(head).attributes.get("temperature") == 74
            ):
                await asyncio.sleep(.005)
        self.assertLess(time.monotonic() - started, .25)
        self.assertGreater(len(self.decision_times), count)
        self.assertIs(runtime.thread, worker)
        self.assertEqual(self.hass.states.get(head).state, "cool")
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)
        self.assertEqual(runtime.fault_count, 0)

    async def test_missing_probes_stop_head_fast_and_restore_waits_for_full_decision(self):
        runtime, switch = await self.start_control()
        head = runtime.config.heads["guest"]
        count = len(self.decision_times)
        self.temperatures("guest", "unavailable")
        async with asyncio.timeout(.2):
            while self.hass.states.get(head).state != "off":
                await asyncio.sleep(.005)
        self.assertEqual(len(self.decision_times), count)
        self.temperatures("guest", 74.8)
        await self.assert_stays(
            runtime, lambda: self.hass.states.get(head).state == "off", wake=True)
        self.assertEqual(len(self.decision_times), count)
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)

        await self.until(lambda: self.hass.states.get(head).state == "cool")
        self.assertGreater(len(self.decision_times), count)
        self.assertEqual(runtime.fault_count, 0)

    async def test_auxiliary_comfort_cutoff_is_fast_and_restart_waits_for_decision(self):
        # Ordinary auxiliary demand below 67.5°F, but above the 67°F house
        # reserve. Re-entering the house reserve may legitimately replan early.
        runtime, switch = await self.start_control(living=67.2)
        heater = runtime.config.heater_entity
        count = len(self.decision_times)
        # Below the independent 70F emergency cutoff, but above the active
        # comfort range's 68.5F high. The fast controller guard must stop heat.
        self.temperatures("living", 69)
        async with asyncio.timeout(.2):
            while self.hass.states.get(heater).state != "off":
                await asyncio.sleep(.005)
        self.assertEqual(len(self.decision_times), count)
        self.temperatures("living", 67.2)
        await self.assert_stays(
            runtime, lambda: self.hass.states.get(heater).state == "off", wake=True)
        self.assertEqual(len(self.decision_times), count)
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")
        self.assertTrue(switch.is_on)

        await self.until(lambda: self.hass.states.get(heater).state == "heat")
        self.assertGreater(len(self.decision_times), count)
        self.assertEqual(runtime.fault_count, 0)


if __name__ == "__main__":
    unittest.main()
