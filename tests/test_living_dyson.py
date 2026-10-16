import unittest
from datetime import timedelta

from house_climate.config import Settings, helper
from house_climate.executor import ControlFault, Executor
from house_climate.policy import Plan, Policy
from tests.test_controller import FakeHA, NIGHT, add_helpers, config, fixture


class LivingDysonTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.settings = Settings.read({}, self.cfg)
        self.policy = Policy(self.cfg)
        self.ha = FakeHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")

    def decide(self, temperature):
        return self.policy.decide(fixture(self.cfg, living=temperature), NIGHT, self.settings)

    def step(self, plan, seconds):
        self.executor.step(plan, self.ha.states(), seconds)

    def test_one_device_selects_heat_fan_only_or_off(self):
        plan = self.decide(67)
        self.assertTrue(plan.heater_on)
        self.assertFalse(plan.auxiliary_fan_only)
        plan = self.decide(68.2)
        self.assertTrue(plan.heater_on)
        self.assertFalse(plan.auxiliary_fan_only)
        plan = self.decide(71)
        self.assertFalse(plan.heater_on)
        self.assertTrue(plan.auxiliary_fan_only)
        plan = self.decide(68)
        self.assertFalse(plan.heater_on)
        self.assertFalse(plan.auxiliary_fan_only)

    def test_fan_does_not_replace_heat_pump_cooling_demand(self):
        plan = self.decide(76)
        self.assertTrue(plan.auxiliary_fan_only)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("living", plan.head_targets_f)

    def test_ha_toggle_disables_only_fan_only_operation(self):
        states = fixture(self.cfg, living=71)
        add_helpers(states)
        states[helper("living_fan_enabled", "input_boolean")]["state"] = "off"
        settings = Settings.read(states, self.cfg, strict=True)
        plan = self.policy.decide(states, NIGHT, settings)
        self.assertFalse(plan.auxiliary_fan_only)
        self.assertIn("living", plan.head_targets_f)
        plan = self.policy.decide(fixture(self.cfg, living=67), NIGHT, settings)
        self.assertTrue(plan.heater_on)

    def test_unsupported_mode_is_reported_without_guessing_a_fan_entity(self):
        states = fixture(self.cfg, living=71)
        states[self.cfg.heater_entity]["attributes"]["hvac_modes"] = ["heat", "off"]
        plan = self.policy.decide(states, NIGHT, self.settings)
        self.assertFalse(plan.auxiliary_fan_only)
        self.assertTrue(any("fan_only" in warning for warning in plan.warnings))
        self.assertEqual(set(plan.fans), set(self.cfg.fans.values()))

    def test_missing_living_probes_stops_fan_only(self):
        self.assertTrue(self.decide(71).auxiliary_fan_only)
        states = fixture(self.cfg, living=71)
        for probe in self.cfg.zones["living"].probes:
            states[probe.entity_id]["last_updated"] = (NIGHT - timedelta(hours=1)).isoformat()
        plan = self.policy.decide(states, NIGHT, self.settings)
        self.assertFalse(plan.auxiliary_fan_only)
        self.assertFalse(plan.heater_on)

    def test_heat_is_confirmed_off_before_fan_only_without_setting_temperature(self):
        self.step(Plan(heater_on=True), 0)
        self.step(Plan(heater_on=True), 15)
        self.ha.calls.clear()
        plan = Plan(auxiliary_fan_only=True)
        self.step(plan, 30)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "off")
        self.step(plan, 45)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "off")
        self.step(plan, 60)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "fan_only")
        calls = [call for call in self.ha.calls if call[2]["entity_id"] == self.cfg.heater_entity]
        self.assertEqual([call[2]["hvac_mode"] for call in calls], ["off", "fan_only"])
        self.assertTrue(all(call[1] == "set_hvac_mode" and "temperature" not in call[2] for call in calls))
        self.step(plan, 75)
        self.assertEqual(len(self.ha.calls), len(calls))
        self.step(Plan(), 90)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "off")

    def test_unacknowledged_heat_is_superseded_before_starting_fan(self):
        self.ha.ignore.add(self.cfg.heater_entity)
        self.step(Plan(heater_on=True), 0)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "off")
        self.ha.ignore.clear()
        self.ha.calls.clear()
        self.step(Plan(auxiliary_fan_only=True), 15)
        self.assertEqual(self.ha.calls[0][2]["hvac_mode"], "off")
        self.assertFalse(any(call[2].get("hvac_mode") == "fan_only" for call in self.ha.calls))
        self.step(Plan(auxiliary_fan_only=True), 30)
        self.step(Plan(auxiliary_fan_only=True), 45)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "fan_only")

    def test_failed_heat_off_never_starts_fan_only(self):
        self.ha.data[self.cfg.heater_entity]["state"] = "heat"
        self.ha.ignore.add(self.cfg.heater_entity)
        self.step(Plan(auxiliary_fan_only=True), 0)
        with self.assertRaises(ControlFault):
            self.step(Plan(auxiliary_fan_only=True), 45)
        self.assertFalse(any(call[2].get("hvac_mode") == "fan_only" for call in self.ha.calls))

    def test_conflicting_auxiliary_requests_are_rejected_before_any_commands(self):
        with self.assertRaises(ControlFault):
            self.step(Plan(heater_on=True, auxiliary_fan_only=True), 0)
        self.assertEqual(self.ha.calls, [])
