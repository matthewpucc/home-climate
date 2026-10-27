"""Ambient gradients must stop conditioning before delayed Off can overshoot."""
import copy
import unittest
from dataclasses import replace
from datetime import timedelta

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Policy
from house_climate.sensors import Reading
from house_climate.temperature_guard import heating_cutoff_reached, opposite_limit_reached
from tests.test_controller import FakeHA, NIGHT


def configuration():
    return Config.from_dict({
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"gradient": "climate.gradient", "healthy": "climate.healthy"},
        "rooms": {
            key: {"head": key,
                  "sensors": [{"entity_id": f"sensor.{key}_{i}"} for i in range(3)],
                  "profile": {"target_f": 72, "half_band_f": 1}}
            for key in ("gradient", "healthy")},
        "decision_interval_seconds": 90, "forecast_minutes": 0,
        "head_minimum_run_seconds": 300, "head_minimum_off_seconds": 0,
    })


def states(cfg, values=None, elapsed=0):
    values = values or {"gradient": (70, 70, 72.914), "healthy": (70, 70, 70)}
    stamp = (NIGHT + timedelta(seconds=elapsed)).isoformat()
    result = {}
    for name, zone in cfg.zones.items():
        for probe, value in zip(zone.probes, values[name]):
            result[probe.entity_id] = {
                "state": str(value), "last_reported": stamp,
                "attributes": {"unit_of_measurement": "°F"}}
    for entity in cfg.climate_entities:
        result[entity] = {
            "state": "off", "attributes": {
                "hvac_modes": ["off", "heat", "cool"], "temperature": 72,
                "min_temp": 60, "max_temp": 90, "target_temp_step": 1}}
    return result


class AmbientStoppingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configuration()
        self.settings = Settings.read({}, self.cfg)

    def test_recorded_warm_probe_replay_stops_early_without_stopping_healthy_head(self):
        # Keep two ambient sources cooler to exercise the raw gradient guard,
        # rather than an ordinary stop from the fused median reaching target.
        # The third source replays the observed warming trace from the watch.
        ha = FakeHA(states(self.cfg))
        cadence = DecisionCadence(self.cfg)
        executor = Executor(self.cfg, ha, "°F")
        executor.phase, executor.direction = "running", "heat"
        first_stopped = None
        for elapsed, hot_probe in ((0, 72.914), (15, 73.094), (30, 73.544),
                                   (45, 74.03), (60, 74.462), (75, 74.786)):
            new = states(self.cfg, {"gradient": (70, 70, hot_probe),
                                   "healthy": (70, 70, 70)}, elapsed)
            for zone in self.cfg.zones.values():
                for probe in zone.probes:
                    ha.data[probe.entity_id] = new[probe.entity_id]
            now = NIGHT + timedelta(seconds=elapsed)
            plan = cadence.decide(ha.states(), now, self.settings, monotonic_now=elapsed)
            executor.step(plan, ha.states(), elapsed)
            if elapsed >= 15 and ha.data["climate.gradient"]["state"] == "off":
                first_stopped = hot_probe if first_stopped is None else first_stopped
            self.assertEqual(ha.data["climate.healthy"]["state"], "heat")
        self.assertEqual(first_stopped, 74.03)
        self.assertLess(first_stopped + .36, 75)  # Same observed residual rise.
        self.assertNotIn("gradient", executor.cycle_holds)

    def test_fresh_safety_inhibition_stays_latched_until_next_comfort_decision(self):
        cadence = DecisionCadence(self.cfg)
        original = states(self.cfg)
        self.assertIn("gradient", cadence.decide(original, NIGHT, self.settings,
                                                 monotonic_now=0).head_targets_f)
        hot = states(self.cfg, {"gradient": (70, 70, 74.1), "healthy": (70, 70, 70)}, 15)
        blocked = cadence.decide(hot, NIGHT + timedelta(seconds=15), self.settings,
                                 monotonic_now=15)
        self.assertIn("gradient", blocked.inhibited_heads)
        recovered = states(self.cfg, elapsed=30)
        held = cadence.decide(recovered, NIGHT + timedelta(seconds=30), self.settings,
                              monotonic_now=30)
        self.assertNotIn("gradient", held.head_targets_f)
        self.assertIn("healthy", held.head_targets_f)
        renewed = cadence.decide(states(self.cfg, elapsed=90), NIGHT + timedelta(seconds=90),
                                 self.settings, monotonic_now=90)
        self.assertIn("gradient", renewed.head_targets_f)

    def test_policy_guard_cannot_be_undone_by_minimum_run_retention(self):
        ha = FakeHA(states(self.cfg))
        executor = Executor(self.cfg, ha, "°F")
        executor.phase, executor.direction = "running", "heat"
        policy = Policy(self.cfg)
        active = policy.decide(ha.states(), NIGHT, self.settings)
        executor.step(copy.deepcopy(active), ha.states(), 0)
        executor.step(copy.deepcopy(active), ha.states(), 15)
        self.assertEqual(executor.head_cycles["climate.gradient"].run_since, 15)
        hot = states(self.cfg, {"gradient": (70, 70, 74.1), "healthy": (70, 70, 70)}, 30)
        for zone in self.cfg.zones.values():
            for probe in zone.probes:
                ha.data[probe.entity_id] = hot[probe.entity_id]
        plan = policy.decide(ha.states(), NIGHT + timedelta(seconds=30), self.settings)
        # Policy eligibility has no maintenance inhibition marker. Retention
        # must independently honour the same margin while run time is <5min.
        self.assertNotIn("gradient", plan.head_targets_f)
        self.assertEqual(plan.inhibited_heads, {})
        executor.step(plan, ha.states(), 30)
        self.assertEqual(ha.data["climate.gradient"]["state"], "off")
        self.assertEqual(ha.data["climate.healthy"]["state"], "heat")

    def test_worsening_room_trend_stops_before_raw_margin(self):
        policy = Policy(self.cfg)
        initial = states(self.cfg, {"gradient": (69, 69, 73.85), "healthy": (70, 70, 70)})
        self.assertIn("gradient", policy.decide(initial, NIGHT, self.settings).head_targets_f)
        warmer = states(self.cfg, {"gradient": (69.4, 69.4, 73.85),
                                  "healthy": (70, 70, 70)}, 120)
        plan = policy.decide(warmer, NIGHT + timedelta(seconds=120), self.settings)
        self.assertLess(plan.rooms["gradient"]["high"], 74)
        self.assertAlmostEqual(plan.rooms["gradient"]["slope_f_per_minute"], .1)
        self.assertNotIn("gradient", plan.head_targets_f)
        self.assertIn("healthy", plan.head_targets_f)
        self.assertEqual(plan.mode, "heat")

    def test_cooling_guard_is_symmetric_and_trends_away_do_not_stop_calls(self):
        falling = Reading(70, 67.15, 73, 67.15, 73, -.1, 3)
        self.assertTrue(opposite_limit_reached(falling, "cool", 66, 75))
        self.assertFalse(opposite_limit_reached(replace(falling, slope_f_per_minute=.1),
                                                "cool", 66, 75))
        warming = Reading(70, 69, 73.85, 69, 73.85, .1, 3)
        self.assertTrue(opposite_limit_reached(warming, "heat", 66, 75))
        self.assertFalse(opposite_limit_reached(replace(warming, slope_f_per_minute=-.1),
                                                "heat", 66, 75))
        # These are reduction checks; ordinary demand/arbitration is untouched.
        self.assertFalse(opposite_limit_reached(warming, "off", 66, 75))

    def test_partial_probe_loss_keeps_running_and_total_loss_only_inhibits_its_head(self):
        cadence = DecisionCadence(self.cfg)
        data = states(self.cfg)
        cadence.decide(data, NIGHT, self.settings, monotonic_now=0)
        data["sensor.gradient_0"]["state"] = "unavailable"
        partial = cadence.decide(data, NIGHT + timedelta(seconds=15), self.settings,
                                  monotonic_now=15)
        self.assertIn("gradient", partial.head_targets_f)
        self.assertEqual(partial.rooms["gradient"]["sources"], 2)
        data["sensor.gradient_1"]["state"] = "unavailable"
        data["sensor.gradient_2"]["state"] = "unavailable"
        missing = cadence.decide(data, NIGHT + timedelta(seconds=30), self.settings,
                                  monotonic_now=30)
        self.assertNotIn("gradient", missing.head_targets_f)
        self.assertIn("healthy", missing.head_targets_f)
        self.assertEqual(missing.mode, "heat")

    def test_auxiliary_forecasts_its_existing_cutoff_without_erasing_comfort_band(self):
        zone = replace(self.cfg.zones["gradient"],
                       profile=replace(self.cfg.zones["gradient"].profile,
                                       target_f=68, half_band_f=.5))
        cfg = replace(self.cfg, zones={**self.cfg.zones, "gradient": zone},
                      heater_entity="climate.aux", auxiliary_room="gradient", heater_cutoff_f=70)
        policy, settings = Policy(cfg), Settings.read({}, cfg)
        initial = states(cfg, {"gradient": (67.4, 67.4, 67.4), "healthy": (70, 70, 70)})
        self.assertTrue(policy.decide(initial, NIGHT, settings).heater_on)
        rising = states(cfg, {"gradient": (68.35, 68.35, 68.35),
                              "healthy": (70, 70, 70)}, 60)
        plan = policy.decide(rising, NIGHT + timedelta(seconds=60), settings)
        self.assertLess(plan.rooms["gradient"]["high"], 68.5)
        self.assertFalse(plan.heater_on)
        self.assertFalse(heating_cutoff_reached(Reading(68.35, 68.35, 68.35,
                                                       68.35, 68.35, -.1, 3), 68.5, 75))


if __name__ == "__main__":
    unittest.main()
