"""Display room comfort separately from the biased physical head call."""
import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.climate import RoomClimate
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.policy import Policy
from custom_components.house_climate.number import RoomDesiredTemperature
from custom_components.house_climate.sensor import ControllerStatus, RoomPlan
from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as control_fixtures


class CallTargetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeTests.asyncTearDown
    handle = native_fixtures.NativeTests.handle
    until = native_fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime

    async def start_biased_control(self, *, metric=False, onboard=74, offset=5):
        if metric:
            self.hass.config.units = METRIC_SYSTEM
            for entity in self.cfg.climate_entities:
                old = self.hass.states.get(entity)
                self.hass.states.async_set(entity, old.state, {
                    **old.attributes, "min_temp": 10, "max_temp": 32,
                    "target_temp_step": .5, "temperature": 22,
                    "current_temperature": 23,
                })
        head = self.cfg.heads["guest"]
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, old.state, {
            **old.attributes, "current_temperature": onboard,
        })
        cfg = replace(Config.from_dict(self.cfg.to_dict()),
                      head_heat_call_offset_f=offset, head_cool_call_offset_f=offset)
        runtime, switch = await self.runtime(cfg=cfg, entry_id="biased_calls")
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: (
            runtime.status.get("event") == "live"
            and self.hass.states.get(head).state == "cool"
            and "guest" in runtime.status.get("head_call_targets_f", {})
            and head not in runtime.status.get("pending_head_commands", {})))
        entry = SimpleNamespace(entry_id="biased_calls", runtime_data=runtime)
        return runtime, switch, entry

    def plan_sensor(self, entry, room_id="guest"):
        sensor = RoomPlan(entry, room_id)
        sensor.hass = self.hass
        return sensor

    def guest_commands(self):
        return [data for domain, service, data in self.calls
                if domain == "climate" and service == "set_temperature"
                and data["entity_id"] == self.cfg.heads["guest"]]

    async def test_physical_cooling_call_is_biased_but_card_desired_temperature_stays(self):
        runtime, switch, entry = await self.start_biased_control()
        self.assertEqual(self.guest_commands()[-1]["temperature"], 67)
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 72)
        self.assertEqual(runtime.status["head_call_targets_f"]["guest"], 67)
        self.assertEqual(runtime.status["room_targets"]["guest"]["target_f"], 72)

        card = RoomClimate(entry, "guest")
        card.hass = self.hass
        desired = RoomDesiredTemperature(entry, "guest")
        desired.hass = self.hass
        self.assertEqual(desired.native_value, 72)
        self.assertEqual(card.extra_state_attributes["desired_temperature_f"], 72)
        self.assertEqual(card.target_temperature_low, 71.5)
        self.assertEqual(card.target_temperature_high, 72.5)
        sensor = self.plan_sensor(entry)
        self.assertIn("Plan: cool head toward 72 °F (head setpoint 67 °F)", sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["head_target_f"], 72)
        self.assertEqual(sensor.extra_state_attributes["head_call_target_f"], 67)
        self.assertEqual(ControllerStatus(entry).extra_state_attributes["head_call_targets_f"]["guest"], 67)
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)

    async def test_metric_device_command_and_display_use_actual_quantized_call(self):
        runtime, switch, entry = await self.start_biased_control(metric=True, onboard=23)
        self.assertEqual(self.guest_commands()[-1]["temperature"], 19.5)
        self.assertAlmostEqual(runtime.status["head_call_targets_f"]["guest"], 67.1)
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 72)
        sensor = self.plan_sensor(entry)
        self.assertIn("toward 22.2 °C (head setpoint 19.5 °C)", sensor.native_value)
        self.assertAlmostEqual(sensor.extra_state_attributes["head_call_target_f"], 67.1)
        card = RoomClimate(entry, "guest")
        card.hass = self.hass
        self.assertEqual(card.extra_state_attributes["desired_temperature_f"], 72)
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)

    async def test_head_call_reports_supported_clamp_instead_of_unreachable_request(self):
        runtime, _, entry = await self.start_biased_control(onboard=64)
        # min(72F comfort, 64F onboard) - 5F requests 59F. This head only
        # supports 60F and above, so expose the actual 60F command.
        self.assertEqual(self.guest_commands()[-1]["temperature"], 60)
        self.assertEqual(runtime.status["head_call_targets_f"]["guest"], 60)
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 72)
        self.assertIn("head setpoint 60 °F", self.plan_sensor(entry).native_value)

    async def test_zero_offset_keeps_comfort_setpoint_despite_cold_onboard_sensor(self):
        runtime, _, entry = await self.start_biased_control(onboard=64, offset=0)
        self.assertEqual(self.guest_commands()[-1]["temperature"], 72)
        self.assertEqual(runtime.status["head_call_targets_f"]["guest"], 72)
        self.assertIn("cool head to 72 °F", self.plan_sensor(entry).native_value)
        self.assertNotIn("head setpoint", self.plan_sensor(entry).native_value)

    async def test_minimum_run_keeps_acknowledged_biased_call_across_comfort_decisions(self):
        source = Config.from_dict(self.cfg.to_dict())
        cfg = replace(
            source, heads={"guest": source.heads["guest"]},
            zones={"guest": source.zones["guest"]}, fans={}, fan_controls={},
            heater_entity=None, auxiliary_room=None,
            poll_seconds=.02, decision_interval_seconds=.06,
            head_minimum_run_seconds=.4, head_minimum_off_seconds=0,
            off_settle_seconds=.02, head_heat_call_offset_f=5,
            head_cool_call_offset_f=5,
        )
        for probe in cfg.zones["guest"].probes:
            old = self.hass.states.get(probe.entity_id)
            self.hass.states.async_set(probe.entity_id, "74.8", dict(old.attributes))
        head = cfg.heads["guest"]
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, old.state, {
            **old.attributes, "current_temperature": 74,
        })
        decisions = []
        original = Policy.decide

        def track_decision(policy, *args, **kwargs):
            decisions.append(time.monotonic())
            return original(policy, *args, **kwargs)

        tracker = patch.object(Policy, "decide", new=track_decision)
        tracker.start()
        self.addCleanup(tracker.stop)
        runtime, switch = await self.runtime(cfg=cfg, entry_id="biased_run_hold")
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: (
            self.hass.states.get(head).state == "cool"
            and runtime.status.get("head_call_targets_f", {}).get("guest") == 67))
        # Allow the worker to observe the successful activation before dropping
        # ordinary demand; minimum-run timing starts from acknowledgement.
        await asyncio.sleep(.05)
        for probe in cfg.zones["guest"].probes:
            old = self.hass.states.get(probe.entity_id)
            self.hass.states.async_set(probe.entity_id, "72", dict(old.attributes))
        await self.until(lambda: runtime.status.get("cycle_holds", {}).get(head, {}).get("kind") == "minimum_run")
        count = len(decisions)
        heartbeat = runtime.controls.heartbeat_monotonic
        # A changed onboard reading would request a colder call for new demand.
        # A retained run must keep its last acknowledged call instead.
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, old.state, {
            **old.attributes, "current_temperature": 64,
        })
        async with asyncio.timeout(.22):
            while len(decisions) < count + 2:
                self.assertEqual(self.hass.states.get(head).state, "cool")
                await asyncio.sleep(.005)
        self.assertEqual(self.hass.states.get(head).attributes["temperature"], 67)
        self.assertEqual(runtime.status["head_targets_f"]["guest"], 72)
        self.assertEqual(runtime.status["head_call_targets_f"]["guest"], 67)
        self.assertEqual(runtime.comfort.effective("guest")["target_f"], 72)
        self.assertGreater(runtime.controls.heartbeat_monotonic, heartbeat)
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)

        await self.until(lambda: (
            self.hass.states.get(head).state == "off"
            and runtime.status.get("mode") == "off"))
        self.assertTrue(switch.is_on)
        self.assertTrue(runtime.controls.owned)
        self.assertEqual(runtime.fault_count, 0)

    def shared_plan(self, *, call_target=67):
        cfg = Config.from_dict(self.cfg.to_dict())
        limits = {"low_f": 71.5, "high_f": 72.5, "target_f": 72,
                  "comfort_enabled": True, "heat_requested": False,
                  "cool_requested": True}
        status = {"event": "shadow", "mode": "cool",
                  "head_targets_f": {"bonus": 72},
                  "room_targets": {"office": limits},
                  "rooms": {"office": {"temperature": 74}}}
        if call_target is not None:
            status["head_call_targets_f"] = {"bonus": call_target}
        return SimpleNamespace(entry_id="shared_call", runtime_data=SimpleNamespace(
            config=cfg, status=status))

    async def test_shadow_shared_head_plan_names_room_goal_and_head_setpoint(self):
        entry = self.shared_plan()
        sensor = self.plan_sensor(entry, "office")
        self.assertIn("Would cool shared head toward 72 °F (head setpoint 67 °F)", sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["rooms_sharing_head"], ["Office", "Entertainment"])
        self.assertEqual(sensor.extra_state_attributes["head_call_target_f"], 67)

    async def test_unchanged_or_missing_call_target_preserves_existing_plan_text(self):
        for call_target in (None, 72):
            with self.subTest(call_target=call_target):
                sensor = self.plan_sensor(self.shared_plan(call_target=call_target), "office")
                self.assertEqual(sensor.native_value,
                                 "Would cool shared head to 72 °F · comfort 71.5 °F–72.5 °F")


if __name__ == "__main__":
    unittest.main()
