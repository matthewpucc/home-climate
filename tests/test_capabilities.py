"""Known unsupported modes must not win shared-system arbitration."""
import unittest

from house_climate.config import Settings
from house_climate.policy import Policy
from tests.test_controller import NIGHT, config, fixture


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def decide(self, states):
        return Policy(self.cfg).decide(states, NIGHT, Settings.read({}, self.cfg))

    def test_unused_cooling_only_head_does_not_block_other_rooms(self):
        states = fixture(self.cfg, guest=76)
        states[self.cfg.heads["living"]]["attributes"]["hvac_modes"] = ["off", "cool"]
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("guest", plan.head_targets_f)

    def test_unservable_heat_demand_cannot_displace_reachable_cooling(self):
        states = fixture(self.cfg, living=64, guest=76)
        states[self.cfg.heater_entity]["state"] = "unavailable"
        states[self.cfg.heads["living"]]["attributes"]["hvac_modes"] = ["off", "cool"]
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("guest", plan.head_targets_f)
        self.assertNotIn("living", plan.head_targets_f)
        self.assertTrue(any("heat requested but unavailable" in message
                            and "off, cool" in message for message in plan.warnings))

    def test_cooling_only_head_can_still_cool_its_room(self):
        states = fixture(self.cfg, living=76)
        states[self.cfg.heads["living"]]["attributes"]["hvac_modes"] = ["off", "cool"]
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("living", plan.head_targets_f)

    def test_mode_return_is_used_on_the_next_decision(self):
        states = fixture(self.cfg, living=64, guest=76)
        states[self.cfg.heater_entity]["state"] = "unavailable"
        entity = self.cfg.heads["living"]
        states[entity]["attributes"]["hvac_modes"] = ["off", "cool"]
        self.assertEqual(self.decide(states).mode, "cool")
        states[entity]["attributes"]["hvac_modes"] = ["off", "heat", "cool"]
        plan = self.decide(states)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("living", plan.head_targets_f)

    def test_empty_metadata_keeps_existing_initialization_recovery_behavior(self):
        states = fixture(self.cfg, guest=76)
        states[self.cfg.heads["guest"]]["attributes"]["hvac_modes"] = []
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("guest", plan.head_targets_f)

    def test_auxiliary_missing_heat_does_not_hide_shared_head_fallback(self):
        states = fixture(self.cfg, living=64)
        states[self.cfg.heater_entity]["attributes"]["hvac_modes"] = ["off", "fan_only"]
        plan = self.decide(states)
        self.assertFalse(plan.heater_on)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("living", plan.head_targets_f)
        self.assertTrue(any("Auxiliary heat requested but unavailable" in warning
                            for warning in plan.warnings))
