"""Probe disagreements must not become competing comfort calls."""
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Plan, Policy
from house_climate.sensors import Reading
from tests.test_controller import FakeHA, NIGHT


def configuration():
    return Config.from_dict({
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.synthetic_shared"},
        "rooms": {"zone": {"name": "Synthetic room", "head": "shared",
            "sensors": [{"entity_id": f"sensor.synthetic_{key}"} for key in ("a", "b", "c")],
            "profile": {"target_f": 72, "half_band_f": .5}}},
        "forecast_minutes": 0, "head_heat_call_offset_f": 0,
        "head_cool_call_offset_f": 0, "head_minimum_run_seconds": 0,
        "head_minimum_off_seconds": 0,
    })


def states(values):
    result = {f"sensor.synthetic_{key}": {
        "state": str(value), "last_reported": NIGHT.isoformat(),
        "attributes": {"unit_of_measurement": "°F"}}
        for key, value in zip(("a", "b", "c"), values)}
    result["climate.synthetic_shared"] = {"state": "off", "attributes": {
        "hvac_modes": ["off", "heat", "cool"], "temperature": 72,
        "min_temp": 60, "max_temp": 90, "target_temp_step": 1}}
    return result


class ComfortFusionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configuration()
        self.settings = Settings.read({}, self.cfg)

    def confirmed_plan(self, values):
        policy = Policy(self.cfg)
        data = states(values)
        policy.decide(data, NIGHT, self.settings)
        return policy.decide(data, NIGHT + timedelta(seconds=61), self.settings)

    def test_persistent_cold_and_hot_outliers_do_not_create_false_comfort_calls(self):
        plan = self.confirmed_plan((68.9, 71.6, 72.9))
        reading = plan.rooms["zone"]
        self.assertEqual(reading["temperature"], 71.6)
        self.assertEqual(reading["confirmed_low"], 68.9)
        self.assertEqual(reading["confirmed_high"], 72.9)
        self.assertEqual(plan.mode, "off")
        self.assertFalse(plan.room_targets["zone"]["heat_requested"])
        self.assertFalse(plan.room_targets["zone"]["cool_requested"])

    def test_confirmed_actual_system_limit_violations_still_request_protection(self):
        for values, mode in (((65, 71.6, 72.9), "heat"), ((71.6, 72, 76), "cool")):
            with self.subTest(mode=mode):
                plan = self.confirmed_plan(values)
                self.assertEqual(plan.mode, mode)
                self.assertIn("shared", plan.head_targets_f)
                self.assertTrue(plan.room_targets["zone"][f"{mode}_requested"])

    def test_latched_comfort_calls_stop_at_median_target_despite_persistent_outliers(self):
        policy = Policy(self.cfg)
        policy.calls = {("zone", "heat"): True, ("zone", "cool"): True}
        reading = Reading(72, 68.9, 72.9, 68.9, 72.9, 0, 3)
        demand = policy.demand("zone", reading, (71.5, 72.5, 72), 1)
        self.assertFalse(demand["heat"]["active"])
        self.assertFalse(demand["cool"]["active"])

    def test_comfort_error_score_uses_median_instead_of_an_outlier(self):
        for mode, temperature in (("heat", 71.8), ("cool", 72.2)):
            with self.subTest(mode=mode):
                policy = Policy(self.cfg)
                policy.calls["zone", mode] = True
                reading = Reading(temperature, 68.9, 72.9, 68.9, 72.9, 0, 3)
                demand = policy.demand("zone", reading, (71.5, 72.5, 72), 1)
                self.assertTrue(demand[mode]["active"])
                self.assertAlmostEqual(demand[mode]["score"][3], .2)

    def test_confirmed_near_limit_trend_guards_remain_independent_of_median(self):
        cfg = replace(self.cfg, forecast_minutes=10)
        for mode, reading in (
            ("heat", Reading(72, 67.5, 73, 67.5, 73, -.1, 3)),
            ("cool", Reading(72, 71, 73.5, 71, 73.5, .1, 3)),
        ):
            with self.subTest(mode=mode):
                demand = Policy(cfg).demand("zone", reading, (71.5, 72.5, 72), 1)
                self.assertTrue(demand[mode]["active"])
                self.assertGreater(demand[mode]["score"][1], 0)

    def test_run_hold_comfort_edges_use_median_but_hard_guards_use_raw_extremes(self):
        cfg = replace(self.cfg, head_minimum_run_seconds=300)
        targets = {"zone": {"low_f": 71.5, "high_f": 72.5,
                            "target_f": 72, "comfort_enabled": True}}
        reading = {"zone": {"temperature": 72, "low": 71, "high": 73,
                            "confirmed_low": 71, "confirmed_high": 73}}
        entity = cfg.heads["shared"]
        for mode in ("heat", "cool"):
            with self.subTest(mode=mode):
                ha = FakeHA(states((71, 72, 73)))
                executor = Executor(cfg, ha, "°F")
                executor.phase, executor.direction = "running", mode
                active = Plan(mode=mode, head_targets_f={"shared": 72},
                              rooms=deepcopy(reading), room_targets=deepcopy(targets))
                executor.step(deepcopy(active), ha.states(), 0)
                executor.step(deepcopy(active), ha.states(), 15)
                idle = Plan(rooms=deepcopy(reading), room_targets=deepcopy(targets))
                executor.step(idle, ha.states(), 30)
                self.assertEqual(ha.data[entity]["state"], mode)
                self.assertEqual(executor.cycle_holds[entity]["kind"], "minimum_run")
                unsafe = Plan(rooms=deepcopy(reading), room_targets=deepcopy(targets))
                unsafe.rooms["zone"]["low" if mode == "cool" else "high"] = 66.25 if mode == "cool" else 74.75
                executor.step(unsafe, ha.states(), 45)
                self.assertEqual(ha.data[entity]["state"], "off")
