"""Comfort decisions stay slow; safety, settings and maintenance stay responsive."""
import copy
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Policy
from tests.test_controller import FakeHA
from tests.test_rooms import NOW, configuration, states


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class CadenceTests(unittest.TestCase):
    def setup_controller(self, raw=None, temperatures=None, **changes):
        cfg = replace(Config.from_dict(raw or configuration()),
                      decision_interval_seconds=90, head_minimum_run_seconds=0,
                      head_minimum_off_seconds=0, **changes)
        self.clock = Clock()
        self.cfg = cfg
        self.settings = Settings.read({}, cfg)
        self.policy = Policy(cfg)
        self.policy.decide = Mock(wraps=self.policy.decide)
        self.cadence = DecisionCadence(cfg, self.policy, self.clock)
        self.data = states(cfg, temperatures or {"alpha": 74, "beta": 71, "gamma": 68})
        return cfg

    def tick(self, seconds, **temperatures):
        self.clock.now = seconds
        now = NOW + timedelta(seconds=seconds)
        for name, value in temperatures.items():
            for probe in self.cfg.zones[name].probes:
                self.data[probe.entity_id]["state"] = str(value)
                self.data[probe.entity_id]["last_reported"] = now.isoformat()
        return self.cadence.decide(self.data, now, self.settings)

    def test_wake_flood_does_not_recompute_or_activate_another_head(self):
        cfg = self.setup_controller(off_settle_seconds=.01)
        ha = FakeHA(self.data)
        executor = Executor(cfg, ha, "°F")
        for stamp in (0, .02, .04, .06):
            plan = self.tick(stamp)
            executor.step(plan, ha.states(), stamp)
        self.assertEqual(ha.data[cfg.heads["shared"]]["state"], "cool")
        for stamp in range(1, 90):
            plan = self.tick(stamp, gamma=74)
            executor.step(plan, ha.states(), stamp)
            self.assertNotIn("second", plan.head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertFalse(any(call[2].get("entity_id") == cfg.heads["second"] and
                             call[2].get("hvac_mode") == "cool" for call in ha.calls))
        self.assertEqual(self.cadence.status()["decision_remaining_seconds"], 1)
        plan = self.tick(90, gamma=74)
        self.assertEqual(self.policy.decide.call_count, 2)
        self.assertIn("second", plan.head_targets_f)
        self.assertEqual(self.cadence.status()["decision_interval_seconds"], 90)

    def test_biased_cooling_stops_at_confirmed_comfort_edge_and_latches(self):
        self.setup_controller(head_cool_call_offset_f=5)
        self.assertIn("shared", self.tick(0).head_targets_f)
        self.cadence.cached_plan.head_call_targets_f["shared"] = 67
        stopped = self.tick(15, alpha=71.5)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertNotIn("shared", stopped.head_call_targets_f)
        self.assertIn("opposite room comfort limit", stopped.inhibited_heads["shared"])
        self.assertNotIn("shared", self.tick(30, alpha=74).head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertIn("shared", self.tick(90).head_targets_f)

    def test_biased_heating_stops_at_confirmed_comfort_edge_and_latches(self):
        self.setup_controller(temperatures={"alpha": 70, "beta": 71, "gamma": 68},
                              head_heat_call_offset_f=5)
        self.assertIn("shared", self.tick(0).head_targets_f)
        stopped = self.tick(15, alpha=72.5)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertNotIn("shared", stopped.head_call_targets_f)
        self.assertIn("opposite room comfort limit", stopped.inhibited_heads["shared"])
        self.assertNotIn("shared", self.tick(30, alpha=70).head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertIn("shared", self.tick(90).head_targets_f)

    def test_biased_call_checks_every_attached_room_comfort_edge(self):
        raw = configuration()
        raw["rooms"]["beta"]["profile"] = {"target_f": 70, "half_band_f": .5}
        self.setup_controller(raw, temperatures={"alpha": 74, "beta": 70, "gamma": 68},
                              head_cool_call_offset_f=5)
        self.assertIn("shared", self.tick(0).head_targets_f)
        stopped = self.tick(15, beta=69.5)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertIn("shared", stopped.inhibited_heads)
        self.assertEqual(self.policy.decide.call_count, 1)

    def test_single_raw_outlier_needs_confirmation_for_comfort_stop(self):
        raw = configuration()
        raw["rooms"]["alpha"]["sensors"].append({"entity_id": "sensor.test_extra"})
        self.setup_controller(raw, head_cool_call_offset_f=5)
        self.assertIn("shared", self.tick(0).head_targets_f)
        probe = self.cfg.zones["alpha"].probes[0].entity_id
        self.data[probe]["state"] = "71"
        for stamp in (15, 45):
            maintained = self.tick(stamp)
            self.assertIn("shared", maintained.head_targets_f)
            self.assertEqual(maintained.rooms["alpha"]["low"], 71)
            self.assertEqual(maintained.rooms["alpha"]["confirmed_low"], 74)
        stopped = self.tick(75)
        self.assertEqual(stopped.rooms["alpha"]["confirmed_low"], 71)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)

    def test_zero_direction_offset_keeps_existing_slow_comfort_behavior(self):
        self.setup_controller(head_cool_call_offset_f=0, head_heat_call_offset_f=5)
        self.tick(0)
        self.assertIn("shared", self.tick(15, alpha=71.5).head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.setup_controller(temperatures={"alpha": 70, "beta": 71, "gamma": 68},
                              head_heat_call_offset_f=0, head_cool_call_offset_f=5)
        self.tick(0)
        self.assertIn("shared", self.tick(15, alpha=72.5).head_targets_f)
        self.assertEqual(self.policy.decide.call_count, 1)

    def test_missing_attached_room_immediately_inhibits_and_latches_head(self):
        self.setup_controller()
        self.assertIn("shared", self.tick(0).head_targets_f)
        for probe in self.cfg.zones["beta"].probes:
            self.data[probe.entity_id]["state"] = "unavailable"
        missing = self.tick(15)
        self.assertNotIn("shared", missing.head_targets_f)
        self.assertIn("shared", missing.inhibited_heads)
        self.assertNotIn("beta", missing.rooms)
        restored = self.tick(30, beta=71)
        self.assertNotIn("shared", restored.head_targets_f)
        self.assertIn("shared", restored.inhibited_heads)
        self.assertIn("beta", restored.rooms)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertIn("shared", self.tick(90).head_targets_f)

    def test_raw_opposite_limit_in_any_attached_zone_trims_without_redecision(self):
        self.setup_controller()
        self.tick(0)
        blocked = self.tick(15, beta=66.2)
        self.assertNotIn("shared", blocked.head_targets_f)
        self.assertIn("opposite temperature limit", blocked.inhibited_heads["shared"])
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertNotIn("shared", self.tick(30, beta=71).head_targets_f)
        self.assertIn("shared", self.tick(90).head_targets_f)

    def test_heat_also_stops_at_raw_opposite_limit(self):
        self.setup_controller(temperatures={"alpha": 70, "beta": 71, "gamma": 68})
        self.assertEqual(self.tick(0).mode, "heat")
        stopped = self.tick(15, beta=74.8)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertIn("heating blocked", stopped.inhibited_heads["shared"])
        self.assertEqual(self.policy.decide.call_count, 1)

    def test_lost_mode_capability_trims_only_that_head_and_latches(self):
        self.setup_controller(temperatures={"alpha": 74, "beta": 71, "gamma": 70})
        first = self.tick(0)
        self.assertEqual(set(first.head_targets_f), {"shared", "second"})
        entity = self.cfg.heads["shared"]
        self.data[entity]["attributes"]["hvac_modes"] = ["off", "heat"]
        blocked = self.tick(15)
        self.assertEqual(set(blocked.head_targets_f), {"second"})
        self.assertIn("HA modes: off, heat", blocked.inhibited_heads["shared"])
        self.data[entity]["attributes"]["hvac_modes"] = ["off", "heat", "cool"]
        self.assertNotIn("shared", self.tick(30).head_targets_f)
        self.assertIn("shared", self.tick(90).head_targets_f)

    def auxiliary_config(self):
        raw = configuration()
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha", heater_cutoff_f=70)
        raw["rooms"]["alpha"]["profile"] = {"target_f": 68, "half_band_f": .5}
        return raw

    def test_auxiliary_heat_uses_fresh_comfort_cutoff_and_latches(self):
        self.setup_controller(self.auxiliary_config(),
                              temperatures={"alpha": 67, "beta": 71, "gamma": 68})
        self.assertTrue(self.tick(0).heater_on)
        self.assertFalse(self.tick(15, alpha=69).heater_on)
        self.assertFalse(self.tick(30, alpha=67).heater_on)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertTrue(self.tick(90, alpha=67).heater_on)

    def test_fast_auxiliary_cutoff_resets_thermostat_hysteresis(self):
        self.setup_controller(self.auxiliary_config(),
                              temperatures={"alpha": 67, "beta": 71, "gamma": 68})
        self.assertTrue(self.tick(0).heater_on)
        self.assertFalse(self.tick(15, alpha=69).heater_on)
        # After the cutoff, a new full decision requires the low edge (67.5),
        # rather than carrying an old heating latch through a fresh safety stop.
        self.assertFalse(self.tick(90, alpha=68.2).heater_on)
        self.assertTrue(self.tick(180, alpha=67).heater_on)

    def test_missing_auxiliary_probes_stop_heat_and_do_not_restart_on_restore(self):
        self.setup_controller(self.auxiliary_config(),
                              temperatures={"alpha": 67, "beta": 71, "gamma": 68})
        self.assertTrue(self.tick(0).heater_on)
        for probe in self.cfg.zones["alpha"].probes:
            self.data[probe.entity_id]["state"] = "unavailable"
        self.assertFalse(self.tick(15).heater_on)
        self.assertFalse(self.tick(30, alpha=67).heater_on)
        self.assertTrue(self.tick(90).heater_on)

    def test_auxiliary_fan_only_cold_guard_is_fast_and_latched(self):
        self.setup_controller(self.auxiliary_config(),
                              temperatures={"alpha": 70, "beta": 71, "gamma": 68})
        self.assertTrue(self.tick(0).auxiliary_fan_only)
        self.assertFalse(self.tick(15, alpha=66.2).auxiliary_fan_only)
        self.assertFalse(self.tick(30, alpha=70).auxiliary_fan_only)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertTrue(self.tick(90).auxiliary_fan_only)

    def test_circulation_stops_on_cold_or_missing_room_and_stays_off(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.setup_controller()
                self.assertEqual(self.tick(0).fans["fan.test_airflow"], 60)
                if missing:
                    for probe in self.cfg.zones["alpha"].probes:
                        self.data[probe.entity_id]["state"] = "unavailable"
                    stopped = self.tick(15)
                else:
                    stopped = self.tick(15, alpha=66.2)
                self.assertEqual(stopped.fans["fan.test_airflow"], 0)
                self.assertEqual(self.tick(30, alpha=74).fans["fan.test_airflow"], 0)
                self.assertEqual(self.policy.decide.call_count, 1)
                self.assertEqual(self.tick(90).fans["fan.test_airflow"], 60)

    def test_explicit_profile_number_toggle_and_schedule_changes_are_immediate(self):
        self.setup_controller()
        self.tick(0)
        self.settings.room_profiles["alpha"] = replace(self.cfg.zones["alpha"].profile, target_f=73)
        self.assertEqual(self.tick(5).room_targets["alpha"]["target_f"], 73)
        self.settings.numbers["mode_hold_minutes"] += 1
        self.tick(10)
        self.settings.toggles["auxiliary_fan_enabled"] = False
        self.tick(15)
        self.assertEqual(self.policy.decide.call_count, 4)

        # At the real wall-clock schedule boundary, a long interval has not expired.
        self.setup_controller()
        before = NOW.replace(hour=4, minute=59, second=50)
        self.data = states(self.cfg, {"alpha": 74, "beta": 71, "gamma": 68}, now=before)
        self.cadence.decide(self.data, before, self.settings, monotonic_now=0)
        self.cadence.decide(self.data, before + timedelta(seconds=10), self.settings, monotonic_now=10)
        self.assertEqual(self.policy.decide.call_count, 2)

    def test_executor_or_caller_mutations_cannot_escape_cached_copy(self):
        self.setup_controller()
        first = self.tick(0)
        expected = copy.deepcopy(first)
        first.mode = "heat"
        first.head_targets_f.clear()
        first.inhibited_heads["shared"] = "external mutation"
        first.fans["fan.test_airflow"] = 100
        first.rooms.clear()
        first.room_targets["alpha"]["target_f"] = 66
        first.warnings.append("external mutation")
        second = self.tick(15)
        self.assertEqual(second.mode, expected.mode)
        self.assertEqual(second.head_targets_f, expected.head_targets_f)
        self.assertEqual(second.fans, expected.fans)
        self.assertEqual(second.room_targets, expected.room_targets)
        self.assertNotIn("external mutation", second.warnings)
        self.assertEqual(second.inhibited_heads, {})

    def test_new_confirmed_hard_limit_can_decide_before_normal_deadline(self):
        for temperature, mode in ((76, "cool"), (65, "heat")):
            with self.subTest(mode=mode):
                self.setup_controller(temperatures={"alpha": 72, "beta": 71, "gamma": 68})
                self.assertEqual(self.tick(0).mode, "off")
                self.assertEqual(self.tick(15, alpha=temperature).mode, mode)
                self.assertEqual(self.policy.decide.call_count, 2)
                self.tick(30, alpha=temperature)
                self.assertEqual(self.policy.decide.call_count, 2)

    def test_fast_reader_confirmation_is_preserved_for_emergency_decision(self):
        raw = configuration()
        raw["rooms"]["alpha"]["sensors"].append({"entity_id": "sensor.test_extra"})
        self.setup_controller(raw, temperatures={"alpha": 72, "beta": 71, "gamma": 68})
        self.assertEqual(self.tick(0).mode, "off")
        probe = self.cfg.zones["alpha"].probes[0].entity_id
        for stamp in (15, 30, 45, 60):
            self.data[probe]["state"] = "76"
            self.data[probe]["last_reported"] = (NOW + timedelta(seconds=stamp)).isoformat()
            self.assertEqual(self.tick(stamp).mode, "off")
        emergency = self.tick(75)
        self.assertEqual(self.policy.decide.call_count, 2)
        self.assertEqual(emergency.rooms["alpha"]["confirmed_high"], 76)
        self.assertIn("shared", emergency.head_targets_f)

    def test_fresh_sensor_warnings_clear_after_restore_without_losing_latch(self):
        self.setup_controller()
        self.tick(0)
        probe = self.cfg.zones["beta"].probes[0].entity_id
        self.data[probe]["state"] = "unavailable"
        self.assertTrue(any(probe in warning for warning in self.tick(15).warnings))
        self.data[probe]["state"] = "71"
        restored = self.tick(30)
        self.assertFalse(any(probe in warning for warning in restored.warnings))
        self.assertEqual(self.policy.decide.call_count, 1)

    def test_reset_invalidates_plan_and_deadline(self):
        self.setup_controller()
        self.tick(0)
        self.assertEqual(self.cadence.status()["decision_remaining_seconds"], 90)
        self.cadence.reset()
        self.assertEqual(self.cadence.status()["decision_remaining_seconds"], 0)
        next_plan = self.tick(1, alpha=72)
        self.assertEqual(next_plan.mode, "off")
        self.assertEqual(self.cadence.status()["decision_remaining_seconds"], 90)


if __name__ == "__main__":
    unittest.main()
