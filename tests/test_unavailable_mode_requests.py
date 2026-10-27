"""Unavailable-mode recovery evidence must retain real thermal eligibility."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.policy import Policy
from tests.test_rooms import NOW, configuration, states


class UnavailableModeRequestsTests(unittest.TestCase):
    def setup_controller(self, temperatures):
        self.cfg = replace(Config.from_dict(configuration()), forecast_minutes=0,
                           decision_interval_seconds=90)
        self.data = states(self.cfg, temperatures)
        self.settings = Settings.read({}, self.cfg)
        self.policy = Policy(self.cfg)
        self.cadence = DecisionCadence(self.cfg, self.policy)
        self.shared = self.cfg.heads["shared"]
        self.data[self.shared]["attributes"]["hvac_modes"] = ["off", "heat"]

    def test_available_direction_arbitrates_while_eligible_missing_request_is_visible(self):
        self.setup_controller({"alpha": 73.5, "beta": 72, "gamma": 66.9})
        plan = self.cadence.decide(self.data, NOW, self.settings, monotonic_now=0)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("cool", plan.unavailable_mode_requests["shared"])
        self.assertNotIn("shared", plan.head_targets_f)

    def test_opposing_raw_house_floor_blocks_missing_cool_recovery_priority(self):
        self.setup_controller({"alpha": 76, "beta": 66.9, "gamma": 68})
        plan = self.cadence.decide(self.data, NOW, self.settings, monotonic_now=0)
        self.assertNotIn("cool", plan.unavailable_mode_requests.get("shared", {}))
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "house_limit")

    def test_maintenance_removes_cleared_missing_request_before_next_decision(self):
        self.setup_controller({"alpha": 73.5, "beta": 72, "gamma": 68})
        plan = self.cadence.decide(self.data, NOW, self.settings, monotonic_now=0)
        self.assertIn("cool", plan.unavailable_mode_requests["shared"])
        stamp = NOW + timedelta(seconds=15)
        for probe in self.cfg.zones["alpha"].probes:
            self.data[probe.entity_id].update(state="71.5", last_reported=stamp.isoformat())
        plan = self.cadence.decide(self.data, stamp, self.settings, monotonic_now=15)
        self.assertNotIn("cool", plan.unavailable_mode_requests.get("shared", {}))
        self.assertEqual(self.cadence.next_decision, 90)

    def test_dispatch_metadata_restoration_removes_obsolete_missing_request(self):
        self.setup_controller({"alpha": 73.5, "beta": 72, "gamma": 68})
        self.cadence.decide(self.data, NOW, self.settings, monotonic_now=0)
        self.data[self.shared]["attributes"]["hvac_modes"] = ["off", "heat", "cool"]
        plan = self.cadence.decide(self.data, NOW + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assertFalse(plan.unavailable_mode_requests)


if __name__ == "__main__":
    unittest.main()
