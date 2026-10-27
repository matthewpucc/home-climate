"""Replay shared-room comfort cutoffs and bound-priority recovery with real policy."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.head_guard import (
    COMFORT_REARM_F, HeadGuard, active_protection, head_constraint, protection_requested,
)
from house_climate.policy import Policy
from house_climate.sensors import Reading

NOW = datetime(2026, 10, 27, 2, 30, tzinfo=timezone.utc)


def configuration(**changes):
    raw = {
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.shared_guard_head"},
        "rooms": {name: {
            "name": label, "head": "shared",
            "sensors": [{"entity_id": f"sensor.{name}_{i}"} for i in range(2)],
            "profile": {"target_f": 72, "half_band_f": 1},
        } for name, label in (("lounge", "Warm area"), ("study", "Work area"))},
        "forecast_minutes": 0, "decision_interval_seconds": 90,
        "head_minimum_run_seconds": 300, "head_minimum_off_seconds": 180,
        "head_heat_call_offset_f": 5, "head_cool_call_offset_f": 5,
    }
    raw.update(changes)
    return Config.from_dict(raw)


def reading(temperature, **changes):
    data = dict(temperature=temperature, low=temperature, high=temperature,
                confirmed_low=temperature, confirmed_high=temperature,
                slope_f_per_minute=0, sources=2)
    data.update(changes)
    return Reading(**data)


def targets(cfg):
    return {name: {"low_f": 71, "high_f": 73, "target_f": 72,
                   "comfort_enabled": True} for name in cfg.zones}


class SharedHeadGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configuration()
        self.settings = Settings.read({}, self.cfg)
        self.cadence = DecisionCadence(self.cfg)
        self.data = {}
        self.mode = "off"

    def tick(self, elapsed, lounge, study):
        now = NOW + timedelta(seconds=elapsed)
        for name, temperature in (("lounge", lounge), ("study", study)):
            for probe in self.cfg.zones[name].probes:
                self.data[probe.entity_id] = {
                    "state": str(temperature), "last_reported": now.isoformat(),
                    "attributes": {"unit_of_measurement": "°F"}}
        self.data[self.cfg.heads["shared"]] = {
            "state": self.mode, "attributes": {"hvac_modes": ["off", "heat", "cool"],
                "temperature": 65 if self.mode == "cool" else 80,
                "min_temp": 65, "max_temp": 90, "target_temp_step": 1}}
        return self.cadence.decide(self.data, now, self.settings, monotonic_now=elapsed)

    def test_recorded_ordinary_restart_waits_for_half_degree_headroom(self):
        self.assertIn("shared", self.tick(0, 73.5, 72.5).head_targets_f)
        self.mode = "cool"
        stopped = self.tick(15, 72, 70.88)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.mode = "off"
        deferred = self.tick(90, 73.42, 71.42)
        self.assertNotIn("shared", deferred.head_targets_f)
        reason = deferred.head_constraints["shared"]["cool"]
        self.assertEqual(reason["kind"], "comfort_rearm")
        self.assertEqual(reason["room"], "study")
        self.assertEqual(reason["required_f"], 71 + COMFORT_REARM_F)
        self.assertNotIn("shared", self.tick(180, 73.42, 71.49).head_targets_f)
        self.assertIn("shared", self.tick(270, 73.42, 71.5).head_targets_f)

    def test_restoring_known_hot_room_does_not_advance_decision_after_probe_loss(self):
        self.assertIn("shared", self.tick(0, 74.5, 72).head_targets_f)
        self.mode = "cool"
        for probe in self.cfg.zones["lounge"].probes:
            self.data[probe.entity_id]["state"] = "unavailable"
        missing = self.cadence.decide(self.data, NOW + timedelta(seconds=15), self.settings,
                                      monotonic_now=15)
        self.assertNotIn("shared", missing.head_targets_f)
        self.mode = "off"
        restored = self.tick(30, 74.5, 72)
        self.assertNotIn("shared", restored.head_targets_f)
        self.assertEqual(self.cadence.next_decision, 90)
        self.assertIn("shared", self.tick(90, 74.5, 72).head_targets_f)

    def test_new_hot_condition_after_missing_ordinary_room_preempts_slow_decision(self):
        self.assertNotIn("shared", self.tick(0, 72, 72).head_targets_f)
        for probe in self.cfg.zones["lounge"].probes:
            self.data[probe.entity_id]["state"] = "unavailable"
        self.cadence.decide(self.data, NOW + timedelta(seconds=15), self.settings,
                            monotonic_now=15)
        urgent = self.tick(30, 74.5, 72)
        self.assertIn("shared", urgent.head_targets_f)
        self.assertEqual(self.cadence.next_decision, 120)

    def test_new_start_also_requires_headroom_without_prior_cutoff(self):
        deferred = self.tick(0, 73.42, 71.42)
        self.assertNotIn("shared", deferred.head_targets_f)
        self.assertEqual(deferred.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")

    def test_house_hot_override_starts_before_ceiling_and_latches_to_target(self):
        self.assertNotIn("shared", self.tick(0, 73.4, 70.88).head_targets_f)
        # A newly confirmed protection condition does not wait the remaining75s.
        urgent = self.tick(15, 74.1, 70.88)
        self.assertIn("shared", urgent.head_targets_f)
        self.assertEqual(urgent.head_protections["shared"]["rooms"], ["lounge"])
        self.mode = "cool"
        recovering = self.tick(30, 73.3, 70.4)
        self.assertIn("shared", recovering.head_targets_f)
        self.assertIn("shared", recovering.head_protections)
        done = self.tick(45, 72, 70.2)
        self.assertNotIn("shared", done.head_targets_f)
        self.assertNotIn("shared", done.head_protections)
        self.assertEqual(done.head_constraints["shared"]["cool"]["room"], "study")

    def test_fixed_house_ten_minute_forecast_can_authorize_recorded_restart(self):
        self.cfg = replace(self.cfg, forecast_minutes=10)
        self.settings, self.cadence = Settings.read({}, self.cfg), DecisionCadence(self.cfg)
        self.tick(0, 72, 71.42)
        forecast = self.tick(90, 73.42, 71.42)
        self.assertLess(forecast.rooms["lounge"]["temperature"], 74)
        self.assertIn("shared", forecast.head_targets_f)
        self.assertIn("shared", forecast.head_protections)
        self.mode = "cool"
        # The original90s short call now continues below the soft Officefloor.
        continued = self.tick(105, 73.42, 70.88)
        self.assertIn("shared", continued.head_targets_f)

    def test_opposing_raw_house_limit_wins_even_when_other_area_is_overheated(self):
        plan = self.tick(0, 76, 66.9)
        self.assertEqual(plan.head_targets_f, {})
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "house_limit")
        self.assertEqual(plan.head_constraints["shared"]["heat"]["kind"], "house_limit")
        self.assertEqual(plan.head_protections, {})

    def test_heat_protection_is_symmetric_and_releases_at_target(self):
        urgent = self.tick(0, 73.1, 66.9)
        self.assertEqual(urgent.mode, "heat")
        self.assertIn("shared", urgent.head_protections)
        self.mode = "heat"
        self.assertIn("shared", self.tick(15, 73.3, 68).head_targets_f)
        stopped = self.tick(30, 73.3, 72)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertNotIn("shared", stopped.head_protections)

    def test_mapping_feedback_missing_room_and_forged_stale_protection_fail_closed(self):
        room_targets = targets(self.cfg)
        rooms = {"lounge": reading(75).__dict__, "study": reading(70.5).__dict__}
        protection = {"mode": "cool", "rooms": ["lounge"]}
        self.assertIsNone(head_constraint(self.cfg, "shared", "cool", rooms, room_targets,
                                         protection=protection))
        rooms["study"]["low"] = 66.9
        self.assertEqual(head_constraint(self.cfg, "shared", "cool", rooms, room_targets,
                                        protection=protection)["kind"], "house_limit")
        rooms.pop("study")
        self.assertEqual(head_constraint(self.cfg, "shared", "cool", rooms, room_targets,
                                        protection=protection)["kind"], "missing")
        rooms = {"lounge": reading(72), "study": reading(70.5)}
        self.assertEqual(head_constraint(self.cfg, "shared", "cool", rooms, room_targets,
                                        protection=protection)["kind"], "comfort_limit")

    def test_protection_uses_confirmed_extremes_but_opposite_cutoff_uses_raw(self):
        unconfirmed = reading(72, high=76)
        self.assertFalse(protection_requested(self.cfg, unconfirmed, "cool"))
        self.assertTrue(protection_requested(self.cfg, replace(unconfirmed, confirmed_high=76), "cool"))
        room_targets = targets(self.cfg)
        detail = head_constraint(self.cfg, "shared", "heat",
                                 {"lounge": unconfirmed, "study": reading(70)}, room_targets)
        self.assertEqual(detail["kind"], "house_limit")

    def test_limits_only_protection_releases_at_existing_safe_interior_stops(self):
        for mode, start, finish in (("cool", 75, 73.5), ("heat", 66, 67.5)):
            with self.subTest(mode=mode):
                room_targets = targets(self.cfg)
                room_targets["lounge"].update(comfort_enabled=False, low_f=66, high_f=75,
                                              target_f=70.5)
                guard = HeadGuard(self.cfg)
                guard.observe({"lounge": reading(start), "study": reading(72)}, room_targets)
                self.assertIn(("shared", mode), guard.protections)
                guard.observe({"lounge": reading(finish), "study": reading(72)}, room_targets)
                self.assertNotIn(("shared", mode), guard.protections)

    def test_comfort_band_touching_house_bound_keeps_existing_stop_threshold(self):
        for mode, key, bound, finish in (("cool", "high_f", 75, 73.5),
                                         ("heat", "low_f", 66, 67.5)):
            with self.subTest(mode=mode):
                room_targets = targets(self.cfg)
                room_targets["lounge"][key] = bound
                protection = {"mode": mode, "rooms": ["lounge"]}
                self.assertIsNone(active_protection(self.cfg, "shared", mode,
                    {"lounge": reading(finish), "study": reading(72)}, room_targets, protection))

    def test_changed_target_and_profile_can_release_old_protection_and_rearm(self):
        room_targets = targets(self.cfg)
        rooms = {"lounge": reading(73), "study": reading(71.42)}
        protection = {"mode": "cool", "rooms": ["lounge"]}
        self.assertIsNotNone(active_protection(self.cfg, "shared", "cool", rooms, room_targets,
                                              protection))
        room_targets["lounge"]["target_f"] = 73
        self.assertIsNone(active_protection(self.cfg, "shared", "cool", rooms, room_targets,
                                           protection))
        room_targets["study"]["low_f"] = 70
        self.assertIsNone(head_constraint(self.cfg, "shared", "cool", rooms, room_targets,
                                         rearm=True))

    def test_new_protection_preempts_high_priority_soft_direction_hold(self):
        raw = {
            "schema_version": 2, "timezone": "Etc/UTC",
            "heads": {"shared": "climate.shared", "other": "climate.other"},
            "rooms": {name: {"head": head,
                "sensors": [{"entity_id": f"sensor.{name}"}],
                "profile": {"target_f": 72, "half_band_f": 1, "priority": priority}}
                for name, head, priority in (("warm", "shared", 1), ("cold", "other", 10))},
            "forecast_minutes": 0, "mode_hold_seconds": 1200,
        }
        cfg, policy = Config.from_dict(raw), None
        policy, settings = Policy(cfg), Settings.read({}, cfg)
        data = {entity: {"state": "off", "attributes": {"hvac_modes": ["off", "heat", "cool"]}}
                for entity in cfg.heads.values()}
        def decide(seconds, warm, cold):
            now = NOW + timedelta(seconds=seconds)
            for name, value in (("warm", warm), ("cold", cold)):
                data[f"sensor.{name}"] = {"state": str(value), "last_reported": now.isoformat(),
                    "attributes": {"unit_of_measurement": "°F"}}
            return policy.decide(data, now, settings)
        self.assertEqual(decide(0, 72, 70).mode, "heat")
        changed = decide(15, 74.1, 70)
        self.assertEqual(changed.mode, "cool")
        self.assertIn("shared", changed.head_protections)
        self.assertEqual(decide(30, 73.4, 70).mode, "cool")


if __name__ == "__main__":
    unittest.main()
