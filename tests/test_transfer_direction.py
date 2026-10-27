"""Transfer air must help its destination, including during head transitions."""
import unittest
from datetime import timedelta
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.policy import Plan, Policy
from tests.test_rooms import NOW, configuration, states


class TransferDirectionTests(unittest.TestCase):
    def setUp(self):
        raw = configuration()
        raw.update(off_settle_seconds=60, head_cool_call_offset_f=5,
                   head_heat_call_offset_f=5)
        raw["rooms"]["beta"]["profile"] = {"target_f": 72, "half_band_f": 1}
        raw["rooms"]["alpha"]["profile"]["half_band_f"] = 1
        self.cfg = Config.from_dict(raw)
        self.settings = Settings.read({}, self.cfg)
        self.policy = Policy(self.cfg)

    def data(self, destination, source, mode="off", *, seconds=0, action=None, modes=None):
        now = NOW + timedelta(seconds=seconds)
        result = states(self.cfg, {"alpha": destination, "beta": source, "gamma": 68}, now)
        head = result[self.cfg.heads["shared"]]
        head.update(state=mode, last_reported=now.isoformat())
        if action is not None:
            head["attributes"]["hvac_action"] = action
        if modes is not None:
            head["attributes"]["hvac_modes"] = modes
        return result

    def decide(self, data, seconds=0):
        return self.policy.decide(data, NOW + timedelta(seconds=seconds), self.settings)

    def assert_fan(self, plan, percentage):
        self.assertEqual(plan.fans["fan.test_airflow"], percentage, plan.as_dict())

    def test_planned_cooling_vetoes_warming_gradient_for_a_cold_destination(self):
        plan = self.decide(self.data(69, 78))
        self.assertEqual(plan.mode, "cool")
        self.assertIn("shared", plan.head_targets_f)
        self.assert_fan(plan, 0)
        self.assertFalse(any(self.policy.transfer_on.values()))
        self.assertTrue(any("cooling at its source" in warning for warning in plan.warnings))
        self.assertEqual(plan.fans["fan.test_rest"], 60)

    def test_planned_heating_vetoes_cooling_gradient_for_a_warm_destination(self):
        plan = self.decide(self.data(73.1, 64))
        self.assertEqual(plan.mode, "heat")
        self.assertIn("shared", plan.head_targets_f)
        self.assert_fan(plan, 0)
        self.assertTrue(any("heating at its source" in warning for warning in plan.warnings))

    def test_same_direction_conditioning_assistance_is_preserved(self):
        for destination, source, mode in ((74.1, 74.3, "cool"), (70, 69.8, "heat")):
            with self.subTest(mode=mode):
                self.policy = Policy(self.cfg)
                plan = self.decide(self.data(destination, source))
                self.assertEqual(plan.mode, mode)
                self.assert_fan(plan, 60)
                self.assertEqual(plan.fan_head_assists, {"fan.test_airflow": "shared"})

    def test_known_idle_source_preserves_useful_ambient_transfer(self):
        plan = self.decide(self.data(69, 73.5))
        self.assertEqual(plan.mode, "off")
        self.assert_fan(plan, 60)

    def test_observed_cooling_veto_survives_a_removed_call_and_delayed_off(self):
        for stamp in (0, 120):
            plan = self.decide(self.data(69, 73.5, "cool", seconds=stamp), stamp)
            self.assertEqual(plan.mode, "off")
            self.assert_fan(plan, 0)
        for stamp, expected in ((135, 0), (180, 0), (195, 60)):
            plan = self.decide(self.data(69, 73.5, seconds=stamp), stamp)
            self.assert_fan(plan, expected)

    def test_stale_off_cannot_clear_recent_conditioning(self):
        self.assert_fan(self.decide(self.data(69, 73.5, "cool")), 0)
        for stamp in (15, 150):
            data = self.data(69, 73.5, seconds=stamp)
            data[self.cfg.heads["shared"]]["last_reported"] = (NOW - timedelta(seconds=30)).isoformat()
            self.assert_fan(self.decide(data, stamp), 0)
        self.assert_fan(self.decide(self.data(69, 73.5, seconds=165), 165), 0)
        self.assert_fan(self.decide(self.data(69, 73.5, seconds=225), 225), 60)

    def test_report_on_change_off_is_fresh_then_waits_for_settle(self):
        self.assert_fan(self.decide(self.data(69, 73.5, "cool")), 0)
        self.assert_fan(self.decide(self.data(69, 73.5, seconds=15), 15), 0)
        stale = self.data(69, 73.5, seconds=90)
        stale[self.cfg.heads["shared"]]["last_reported"] = (NOW + timedelta(seconds=15)).isoformat()
        self.assert_fan(self.decide(stale, 90), 60)
        self.assert_fan(self.decide(self.data(69, 73.5, seconds=105), 105), 60)

    def test_cached_off_from_before_a_planned_start_is_not_a_fresh_acknowledgement(self):
        tracker = self.policy.transfer_airflow
        data = self.data(69, 73.5)
        tracker.observe(data, NOW, Plan(mode="cool", head_targets_f={"shared": 72}))
        tracker.observe(data, NOW + timedelta(seconds=120), Plan())
        self.assertEqual(tracker.opposing_direction("shared", 69, 72), "cooling")
        data[self.cfg.heads["shared"]]["last_reported"] = (NOW + timedelta(seconds=135)).isoformat()
        tracker.observe(data, NOW + timedelta(seconds=135), Plan())
        self.assertEqual(tracker.opposing_direction("shared", 69, 72), "cooling")
        tracker.observe(data, NOW + timedelta(seconds=195), Plan())
        self.assertIsNone(tracker.opposing_direction("shared", 69, 72))

    def test_off_with_active_hvac_action_is_not_idle(self):
        plan = self.decide(self.data(69, 73.5, action="cooling"))
        self.assertEqual(plan.mode, "off")
        self.assert_fan(plan, 0)
        self.assert_fan(self.decide(self.data(69, 73.5, seconds=90, action="cooling"), 90), 0)

    def test_pending_opposite_mode_keeps_residual_until_idle_settles(self):
        for previous, requested, temperature, opposing in (
                ("cool", "heat", 69, "cooling"), ("heat", "cool", 75, "heating")):
            with self.subTest(previous=previous, requested=requested):
                self.policy = Policy(self.cfg)
                tracker = self.policy.transfer_airflow
                tracker.observe(self.data(temperature, 72, previous), NOW, Plan())
                pending = Plan(mode=requested, head_targets_f={"shared": 72})
                for stamp in (15, 60):
                    tracker.observe(self.data(temperature, 72, seconds=stamp),
                                    NOW + timedelta(seconds=stamp), pending)
                    self.assertEqual(tracker.opposing_direction("shared", temperature, 72), opposing)
                tracker.observe(self.data(temperature, 72, seconds=75),
                                NOW + timedelta(seconds=75), pending)
                self.assertIsNone(tracker.opposing_direction("shared", temperature, 72))

    def test_changeover_assistance_waits_for_previous_direction_to_settle(self):
        for previous, requested, destination, source in (
                ("cool", "heat", 70, 69.8), ("heat", "cool", 74.1, 74.3)):
            with self.subTest(previous=previous, requested=requested):
                self.policy = Policy(self.cfg)
                first = self.decide(self.data(destination, source, previous))
                self.assertEqual(first.mode, requested)
                self.assert_fan(first, 0)
                for stamp, expected in ((15, 0), (60, 0), (75, 60)):
                    plan = self.decide(self.data(destination, source, seconds=stamp), stamp)
                    self.assertEqual(plan.mode, requested)
                    self.assert_fan(plan, expected)

    def test_opposite_observation_without_idle_cannot_erase_residual(self):
        tracker = self.policy.transfer_airflow
        tracker.observe(self.data(69, 72, "cool"), NOW, Plan())
        tracker.observe(self.data(69, 72, "heat", seconds=15), NOW + timedelta(seconds=15), Plan())
        self.assertEqual(tracker.opposing_direction("shared", 69, 72), "cooling")
        self.assertEqual(tracker.opposing_direction("shared", 75, 72), "heating")

    def test_contradictory_report_vetoes_both_directions(self):
        for destination, source in ((69, 73.5), (73.5, 69)):
            with self.subTest(destination=destination):
                self.policy = Policy(self.cfg)
                plan = self.decide(self.data(destination, source, "heat", action="cooling"))
                self.assert_fan(plan, 0)

    def test_unknown_source_does_not_claim_useful_ambient_transfer(self):
        for mode, modes in (("unavailable", None), ("off", [])):
            with self.subTest(mode=mode, modes=modes):
                self.policy = Policy(self.cfg)
                plan = self.decide(self.data(69, 73.5, mode, modes=modes))
                self.assert_fan(plan, 0)

    def test_unknown_feedback_widens_both_directions_after_known_conditioning(self):
        for previous, temperature in (("cool", 75), ("heat", 69)):
            with self.subTest(previous=previous):
                self.policy = Policy(self.cfg)
                tracker = self.policy.transfer_airflow
                tracker.observe(self.data(temperature, 72, previous), NOW, Plan())
                tracker.observe(self.data(temperature, 72, "unavailable", seconds=15),
                                NOW + timedelta(seconds=15),
                                Plan(mode=previous, head_targets_f={"shared": 72}))
                self.assertIsNotNone(tracker.opposing_direction("shared", temperature, 72))

    def test_cross_head_transfer_uses_source_not_destination_conditioning(self):
        raw = self.cfg.to_dict()
        raw["rooms"]["beta"]["head"] = "second"
        self.cfg = Config.from_dict(raw)
        self.settings = Settings.read({}, self.cfg)
        self.policy = Policy(self.cfg)
        plan = self.decide(self.data(69, 78))
        self.assertEqual(plan.mode, "cool")
        self.assertIn("second", plan.head_targets_f)
        self.assertNotIn("shared", plan.head_targets_f)
        self.assert_fan(plan, 0)

    def test_fast_destination_change_removes_assist_without_reactivation(self):
        data = self.data(74.1, 74.3)
        self.policy.decide = Mock(wraps=self.policy.decide)
        cadence = DecisionCadence(self.cfg, self.policy, clock=lambda: 0)
        first = cadence.decide(data, NOW, self.settings, monotonic_now=0)
        self.assert_fan(first, 60)
        for stamp, destination, expected in ((15, 69, 0), (30, 73, 0), (90, 73, 60)):
            data = self.data(destination, 78, seconds=stamp)
            plan = cadence.decide(data, NOW + timedelta(seconds=stamp), self.settings,
                                  monotonic_now=stamp)
            self.assertIn("shared", plan.head_targets_f)
            self.assert_fan(plan, expected)
            self.assertEqual(self.policy.decide.call_count, 1 if stamp < 90 else 2)

    def test_fast_source_feedback_vetoes_a_cached_ambient_fan_call(self):
        self.policy.decide = Mock(wraps=self.policy.decide)
        cadence = DecisionCadence(self.cfg, self.policy, clock=lambda: 0)
        first = cadence.decide(self.data(69, 73.5), NOW, self.settings, monotonic_now=0)
        self.assertEqual(first.mode, "off")
        self.assert_fan(first, 60)
        stopped = cadence.decide(self.data(69, 73.5, "cool", seconds=15),
                                 NOW + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assert_fan(stopped, 0)
        self.assertTrue(any("cooling at its source" in warning for warning in stopped.warnings))
        self.assertEqual(self.policy.decide.call_count, 1)


if __name__ == "__main__":
    unittest.main()
