"""A shared-room veto is readable without opening truncated warning attributes."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from homeassistant.core import State
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.runtime import ControllerRuntime
from custom_components.house_climate.sensor import (
    ControllerStatus, CurrentIssues, Overview, RecoveryStatus, RoomPlan,
)
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures


class SharedGuardStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    def entry(self):
        raw = configuration()
        for room_id, name in (("alpha", "Media area"), ("beta", "Workroom")):
            raw["rooms"][room_id]["name"] = name
            raw["rooms"][room_id]["profile"] = {"target_f": 72, "half_band_f": 1}
        config = Config.from_dict(raw)
        runtime = ControllerRuntime(self.hass, config, False)
        self.runtimes.append(runtime)
        entry = SimpleNamespace(entry_id="shared_guard", runtime_data=runtime)
        target = {"low_f": 71, "high_f": 73, "target_f": 72,
                  "comfort_enabled": True, "priority": 1,
                  "heat_requested": False, "cool_requested": False}
        blocker = {"kind": "comfort_limit", "room": "beta", "room_name": "Workroom",
                   "mode": "cool", "temperature_f": 70.9, "boundary_f": 71,
                   "required_f": 71.5,
                   "reason": "Workroom 70.9°F reached its 71°F comfort floor; cool stopped at opposite room comfort limit"}
        runtime._async_report("live", {
            "phase": "settling", "settle_remaining_seconds": 0,
            "mode": "off", "head_targets_f": {}, "head_call_targets_f": {},
            "head_constraints": {"shared": {"cool": blocker}}, "head_protections": {},
            "rooms": {"alpha": {"temperature": 73.4}, "beta": {"temperature": 70.9}},
            "room_targets": {"alpha": {**target, "cool_requested": True},
                             "beta": {**target, "heat_requested": True}},
            "warnings": ["Expected probe disagreement " * 15, "Single ambient source"],
            "fans": {}, "heater_on": False,
        })
        return entry

    def rendered(self, sensor):
        sensor.hass = self.hass
        result = sensor._async_calculate_state()
        State("sensor.test_guard", result.state, result.attributes)
        return result.state

    async def test_current_soft_block_names_room_floor_and_waiting_demand_before_warning_noise(self):
        entry = self.entry()
        expected = "Workroom 70.9°F is below its 71°F comfort floor; Media area cooling waits"
        for sensor in (Overview(entry), RecoveryStatus(entry), CurrentIssues(entry)):
            text = self.rendered(sensor)
            self.assertIn(expected, text)
            self.assertLessEqual(len(text), 255)
            self.assertNotIn("Ready; no heat-pump demand", text)
            self.assertLess(text.index("Workroom"), text.index("Expected probe") if "Expected probe" in text else 255)
        room = self.rendered(RoomPlan(entry, "alpha"))
        self.assertIn("keep shared head off; " + expected, room)
        self.assertEqual(self.calls, [])

    async def test_rearm_explains_temperature_headroom_and_uses_ha_display_units(self):
        entry = self.entry()
        constraint = entry.runtime_data.status["head_constraints"]["shared"]["cool"]
        constraint.update(kind="comfort_rearm", temperature_f=71.4)
        self.hass.config.units = METRIC_SYSTEM
        text = self.rendered(RecoveryStatus(entry))
        self.assertIn("Workroom 21.89°C needs 21.94°C before cooling restarts", text)
        self.assertIn("Media area cooling waits", text)
        self.assertNotIn("°F", text)

    async def test_active_house_protection_explains_soft_floor_exception_without_claiming_feedback(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status.update(mode="cool", phase="running", head_targets_f={"shared": 72},
                      head_call_targets_f={"shared": 65},
                      head_protections={"shared": {"mode": "cool", "rooms": ["alpha"],
                                                   "reason": "Cool for Media area house protection"}})
        # A previous or opposite-direction block must not describe a running plan as Off.
        status["head_constraints"]["shared"]["heat"] = {
            "kind": "house_limit", "room": "alpha", "mode": "heat",
            "reason": "Media area: heat blocked by opposite house limit"}
        expected = "Cooling for Media area house protection; Workroom comfort floor relaxed"
        self.assertIn(expected, self.rendered(Overview(entry)))
        self.assertEqual(self.rendered(RecoveryStatus(entry)), expected)
        self.assertIn("Plan: cool shared head", self.rendered(RoomPlan(entry, "alpha")))
        self.assertIn(expected, self.rendered(RoomPlan(entry, "alpha")))
        self.assertNotIn("cooling waits", self.rendered(CurrentIssues(entry)))
        self.assertNotIn("blocked by opposite house limit", self.rendered(RecoveryStatus(entry)))
        self.assertEqual(self.calls, [])

    async def test_old_stop_reason_is_not_promoted_to_a_current_constraint(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status.update(phase="running", head_constraints={}, warnings=[], head_stop_reasons={
            "climate.test_shared": {"reason": "Old Workroom comfort cutoff", "mode": "cool"}})
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")
        self.assertNotIn("Old Workroom", self.rendered(Overview(entry)))

    async def test_heating_explains_the_opposing_warm_room_and_house_override_symmetrically(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status["head_constraints"] = {"shared": {"heat": {
            "kind": "comfort_limit", "room": "alpha", "mode": "heat",
            "temperature_f": 73.4, "boundary_f": 73, "required_f": 72.5}}}
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "Media area 73.4°F is above its 73°F comfort ceiling; Workroom heating waits")
        status.update(mode="heat", phase="running", head_targets_f={"shared": 72},
                      head_protections={"shared": {"mode": "heat", "rooms": ["beta"]}})
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "Heating for Workroom house protection; Media area comfort ceiling relaxed")

    async def test_unrequested_head_does_not_invent_a_waiting_call(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status.update(phase="running", warnings=[], head_constraints={"second": {"cool": {
            "kind": "comfort_rearm", "room": "gamma", "mode": "cool", "temperature_f": 71.2,
            "boundary_f": 71, "required_f": 71.5, "reason": "Unrequested other room cooling"}}})
        status["room_targets"]["gamma"] = {"heat_requested": False, "cool_requested": False}
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")

    async def test_long_labels_leave_the_temperature_limit_visible_in_the_state(self):
        entry = self.entry()
        config = entry.runtime_data.config
        entry.runtime_data.config = replace(config, zones={
            **config.zones, "beta": replace(config.zones["beta"], name="Shared work area " * 30),
            "alpha": replace(config.zones["alpha"], name="Shared media area " * 30)})
        text = self.rendered(Overview(entry))
        self.assertIn("70.9°F is below its 71°F comfort floor", text)
        self.assertLessEqual(len(text), 255)

    async def test_house_guard_distinguishes_forecast_from_current_reading_in_metric_units(self):
        entry = self.entry()
        entry.runtime_data.status["head_constraints"]["shared"]["cool"] = {
            "kind": "house_limit", "room": "beta", "mode": "cool",
            "temperature_f": 67.2, "projected_f": 66.8, "boundary_f": 67}
        self.hass.config.units = METRIC_SYSTEM
        text = self.rendered(RecoveryStatus(entry))
        self.assertIn("Workroom: cooling blocked by 19.4°C house guard", text)
        self.assertIn("reading 19.6°C, forecast 19.3°C", text)
        self.assertIn("Media area cooling waits", text)
        self.assertNotIn("°F", text)

    async def test_hardware_timing_and_recovery_keep_priority_over_room_rearm(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status.update(phase="running", cycle_holds={"climate.test_shared": {
            "kind": "minimum_off", "remaining_seconds": 90}})
        self.assertTrue(self.rendered(RecoveryStatus(entry)).startswith(
            "Media area, Workroom: Waiting for minimum off time: 90s remaining"))
        self.assertTrue(self.rendered(Overview(entry)).startswith("Live plan · Waiting for head cycle timing"))
        status.update(cycle_holds={}, device_issues={"climate.test_shared": "Waiting for device to reconnect"})
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Media area, Workroom: Waiting for device to reconnect")
        self.assertNotIn("comfort floor", self.rendered(RoomPlan(entry, "alpha")))

    async def test_fault_heartbeat_and_disabled_preview_keep_priority(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status["heartbeat"] = {"state": "delayed", "blocked": True, "reason": "HA confirmation delayed"}
        self.assertEqual(self.rendered(Overview(entry)), "Live plan · HA confirmation delayed")
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "HA confirmation delayed")
        self.assertTrue(self.rendered(CurrentIssues(entry)).startswith("HA confirmation delayed"))
        status.update(event="shadow", control_block_reason="Controller disabled")
        self.assertTrue(self.rendered(Overview(entry)).startswith("Preview only · Controller disabled"))
        self.assertTrue(self.rendered(RecoveryStatus(entry)).startswith("Preview only · Controller disabled"))
        self.assertTrue(self.rendered(RoomPlan(entry, "alpha")).startswith("Would keep shared head off"))
        status.update(event="fault", error="Controller stopped after a failure")
        for sensor in (Overview(entry), RecoveryStatus(entry), CurrentIssues(entry)):
            self.assertEqual(self.rendered(sensor), "Controller fault: Controller stopped after a failure")

    async def test_structured_explanation_keeps_current_thresholds_without_mutating_status(self):
        entry = self.entry()
        status = entry.runtime_data.status
        before = deepcopy(status)
        controller = ControllerStatus(entry)
        self.assertEqual(controller.extra_state_attributes["head_constraints"], status["head_constraints"])
        for sensor in (Overview(entry), RecoveryStatus(entry), CurrentIssues(entry)):
            self.rendered(sensor)
            self.assertEqual(sensor.extra_state_attributes["head_constraints"], status["head_constraints"])
        room = RoomPlan(entry, "alpha")
        self.assertEqual(room.extra_state_attributes["head_constraints"], status["head_constraints"]["shared"])
        self.assertEqual(status, before)


if __name__ == "__main__":
    unittest.main()
