"""Measured protection outcomes across real fusion, planning, and maintenance."""
from datetime import datetime, timedelta, timezone
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.policy import Policy

START = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.tick = 0

    def __call__(self):
        return self.tick

    @property
    def now(self):
        return START + timedelta(seconds=self.tick)


def configuration(*, shared=False, priorities=(10, .1), protection=None):
    heads = {"north_head": "climate.fixture_north", "south_head": "climate.fixture_south"}
    rooms = {}
    for index, name in enumerate(("north", "south")):
        rooms[name] = {
            "name": f"Synthetic {name} space", "head": "north_head" if shared else name + "_head",
            "sensors": [{"entity_id": f"sensor.{name}_ambient_{source}"} for source in range(3)],
            "profile": {"target_f": 72, "low_f": 71, "high_f": 73,
                        "half_band_f": 1, "priority": priorities[index]},
        }
    if shared:
        heads.pop("south_head")
    return Config.from_dict({
        "schema_version": 2, "timezone": "Etc/UTC", "heads": heads, "rooms": rooms,
        "low_f": 66, "high_f": 75,
        "head_heat_call_offset_f": 5, "head_cool_call_offset_f": 5,
        "decision_interval_seconds": 90, "mode_hold_seconds": 1200,
        "switch_confirmation_seconds": 120, "sensor_confirmation_seconds": 60,
        "protection": {"qualification_seconds": 0, **(protection or {})},
        "outdoor_probes": [{"entity_id": "sensor.fixture_outside", "source": "outside_station"}],
    })


def states(cfg, clock, temperatures, outdoor=50, *, modes=None):
    result = {}
    for room, zone in cfg.zones.items():
        values = temperatures.get(room, 72)
        if not isinstance(values, (list, tuple)):
            values = [values] * len(zone.probes)
        for probe, value in zip(zone.probes, values):
            result[probe.entity_id] = {"state": str(value), "last_reported": clock.now.isoformat(),
                                      "attributes": {"unit_of_measurement": "°F"}}
    for head, entity in cfg.heads.items():
        result[entity] = {"state": (modes or {}).get(head, "off"),
                          "last_reported": clock.now.isoformat(),
                          "attributes": {"hvac_modes": ["off", "heat", "cool"],
                                         "min_temp": 40, "max_temp": 95, "temperature": 72}}
    result["sensor.fixture_outside"] = {
        "state": "unavailable" if outdoor is None else str(outdoor),
        "last_reported": clock.now.isoformat(), "attributes": {"unit_of_measurement": "°F"},
    }
    return result


class ProtectionArbitrationTests(unittest.TestCase):
    def decide(self, cfg, clock, temperatures, outdoor=50, *, policy=None, modes=None):
        policy = policy or Policy(cfg, clock=clock)
        return policy.decide(states(cfg, clock, temperatures, outdoor, modes=modes),
                             clock.now, Settings.read({}, cfg))

    def test_larger_actual_violation_wins_even_against_weather_and_comfort_priority(self):
        for outside, floor in ((50, 60), (20, 65)):
            for heat_excess, cool_excess, wanted in ((.51, .5, "heat"), (.5, .51, "cool")):
                with self.subTest(outside=outside, wanted=wanted):
                    cfg = configuration(priorities=(.1, 10) if wanted == "heat" else (10, .1))
                    clock = Clock()
                    plan = self.decide(cfg, clock, {"north": floor - heat_excess,
                                                    "south": 80 + cool_excess}, outside)
                    self.assertEqual(plan.mode, wanted)
                    context = plan.protection_context
                    self.assertEqual(context["preferred_emergency_mode"], wanted)
                    self.assertEqual(context["low_f"], floor)
                    self.assertTrue(context["emergencies"]["north"]["heat"]["measured"])
                    self.assertTrue(context["emergencies"]["south"]["cool"]["measured"])
                    self.assertGreater(plan.mode_scores[wanted][0], plan.mode_scores["cool" if wanted == "heat" else "heat"][0])

    def test_exact_equal_emergencies_use_qualified_warm_or_cold_weather(self):
        for outside, floor, wanted, regime in ((50, 60, "cool", "warm"), (20, 65, "heat", "cold")):
            with self.subTest(regime=regime):
                cfg = configuration(priorities=(10, .1) if wanted == "cool" else (.1, 10))
                clock = Clock()
                plan = self.decide(cfg, clock, {"north": floor - .5, "south": 80.5}, outside)
                self.assertEqual(plan.protection_context["outdoor_regime"], regime)
                self.assertEqual(plan.protection_context["tie_mode"], wanted)
                self.assertEqual(plan.mode, wanted)

    def test_startup_exact_reference_and_unknown_default_to_heat_but_preferences_are_configurable(self):
        for outside, field, status in ((32, "boundary_tie_mode", "known"),
                                       (None, "unknown_outdoor_tie_mode", "unknown")):
            for wanted in ("heat", "cool"):
                with self.subTest(outside=outside, wanted=wanted):
                    cfg = configuration(protection={} if wanted == "heat" else {field: "cool"})
                    clock = Clock()
                    plan = self.decide(cfg, clock, {"north": 64.5, "south": 80.5}, outside)
                    self.assertEqual(plan.protection_context["outdoor_status"], status)
                    self.assertEqual(plan.protection_context["low_f"], 65)
                    self.assertEqual(plan.protection_context["tie_mode"], wanted)
                    self.assertEqual(plan.mode, wanted)

    def test_literal_indoor_trips_are_distinct_from_predictive_reserve(self):
        for north in (60, 60.5):
            cfg, clock = configuration(), Clock()
            reserve = self.decide(cfg, clock, {"north": north})
            self.assertFalse(reserve.protection_context["emergencies"].get("north", {}).get("heat", {}).get("active", False))
            self.assertEqual(reserve.mode_scores["heat"][0], 0)
        for outside, floor in ((50, 60), (20, 65), (None, 65)):
            cfg, clock = configuration(), Clock()
            heat = self.decide(cfg, clock, {"north": floor - .01}, outside)
            self.assertTrue(heat.protection_context["emergencies"]["north"]["heat"]["measured"])
            self.assertGreaterEqual(heat.mode_scores["heat"][0], 1)
        cfg, clock = configuration(), Clock()
        cool = self.decide(cfg, clock, {"south": 80})
        measured = cool.protection_context["emergencies"]["south"]["cool"]
        self.assertTrue(measured["measured"])
        self.assertEqual(measured["violation_f"], 0)
        self.assertGreaterEqual(cool.mode_scores["cool"][0], 1)

    def test_emergency_release_margin_does_not_keep_emergency_priority_until_comfort_target(self):
        for room, mode, sequence in (("north", "heat", (59.5, 60.5, 61)),
                                     ("south", "cool", (80.5, 79.5, 79))):
            with self.subTest(mode=mode):
                cfg, clock = configuration(), Clock()
                policy = Policy(cfg, clock=clock)
                first = self.decide(cfg, clock, {room: sequence[0]}, policy=policy)
                self.assertTrue(first.protection_context["emergencies"][room][mode]["measured"])
                clock.tick = 15
                recovering = self.decide(cfg, clock, {room: sequence[1]}, policy=policy)
                detail = recovering.protection_context["emergencies"][room][mode]
                self.assertTrue(detail["active"])
                self.assertFalse(detail["measured"])
                self.assertTrue(detail["recovering"])
                clock.tick = 30
                released = self.decide(cfg, clock, {room: sequence[2]}, policy=policy)
                self.assertNotIn(mode, released.protection_context["emergencies"].get(room, {}))
                self.assertEqual(released.mode_scores[mode][0], 0)
                self.assertTrue(released.room_targets[room][f"{mode}_requested"],
                                "Ordinary comfort can still request conditioning after emergency priority ends")
                self.assertEqual(released.room_targets[room]["target_f"], 72)

    def test_shared_head_with_opposing_actual_limits_has_no_safe_direction(self):
        cfg, clock = configuration(shared=True), Clock()
        plan = self.decide(cfg, clock, {"north": 59.5, "south": 80.5})
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_targets_f, {})
        self.assertEqual(plan.protection_context["preferred_emergency_mode"], "cool")
        self.assertEqual(plan.head_constraints["north_head"]["heat"]["kind"], "house_limit")
        self.assertEqual(plan.head_constraints["north_head"]["cool"]["kind"], "house_limit")
        self.assertTrue(any("not currently feasible" in warning for warning in plan.warnings))

    def test_shared_head_forecast_stop_remains_effective_before_raw_floor_is_crossed(self):
        cfg, clock = configuration(shared=True), Clock()
        policy = Policy(cfg, clock=clock)
        first = self.decide(cfg, clock, {"north": 61.25, "south": 80.5}, policy=policy)
        self.assertEqual(first.mode, "cool")
        clock.tick = 60
        stopped = self.decide(cfg, clock, {"north": 61.15, "south": 80.5}, policy=policy,
                              modes={"north_head": "cool"})
        self.assertEqual(stopped.mode, "off")
        detail = stopped.head_constraints["north_head"]["cool"]
        self.assertEqual(detail["kind"], "house_limit")
        self.assertGreater(detail["temperature_f"], detail["boundary_f"])
        self.assertLess(detail["projected_f"], detail["boundary_f"])

    def test_median_crossing_is_immediate_without_an_extra_confirmation_delay(self):
        cfg, clock = configuration(), Clock()
        plan = self.decide(cfg, clock, {"north": [59, 59, 71]})
        self.assertEqual(plan.mode, "heat")
        self.assertTrue(plan.protection_context["emergencies"]["north"]["heat"]["measured"])
        cfg, clock = configuration(), Clock()
        plan = self.decide(cfg, clock, {"south": [80, 80, 68]})
        self.assertEqual(plan.mode, "cool")
        self.assertTrue(plan.protection_context["emergencies"]["south"]["cool"]["measured"])

    def test_lone_outlier_waits_existing_confirmation_but_raw_opposing_guard_stops_immediately(self):
        for room, mode, values, opposing in (("north", "heat", [59, 73.5, 73.5], "cool"),
                                              ("south", "cool", [81, 70.5, 70.5], "heat")):
            with self.subTest(mode=mode):
                cfg, clock = configuration(), Clock()
                policy = Policy(cfg, clock=clock)
                head = cfg.zones[room].head
                initial = self.decide(cfg, clock, {room: values}, policy=policy)
                self.assertEqual(initial.decision_valid_until_monotonic, 60)
                self.assertFalse(initial.protection_context["emergencies"].get(room, {}).get(mode, {}).get("active", False))
                self.assertEqual(initial.head_constraints[head][opposing]["kind"], "house_limit")
                self.assertNotIn(head, initial.head_targets_f)
                clock.tick = 59
                waiting = self.decide(cfg, clock, {room: values}, policy=policy)
                self.assertEqual(waiting.decision_valid_until_monotonic, 60)
                self.assertFalse(waiting.protection_context["emergencies"].get(room, {}).get(mode, {}).get("active", False))
                clock.tick = 60
                confirmed = self.decide(cfg, clock, {room: values}, policy=policy)
                self.assertTrue(confirmed.protection_context["emergencies"][room][mode]["measured"])
                self.assertEqual(confirmed.mode, mode)
                self.assertIn(head, confirmed.head_targets_f)

    def test_maintenance_promotes_new_actual_emergency_inside_an_existing_reserve_without_waiting_90_seconds(self):
        cfg, clock = configuration(), Clock()
        cadence = DecisionCadence(cfg, clock=clock)
        first = cadence.decide(states(cfg, clock, {"north": 60.1, "south": 79.5}), clock.now,
                               Settings.read({}, cfg))
        self.assertEqual(first.mode, "heat")
        self.assertEqual(first.mode_scores["cool"][0], 0)
        clock.tick = 15
        actual = cadence.decide(states(cfg, clock, {"north": 60.1, "south": 80}), clock.now,
                                Settings.read({}, cfg))
        self.assertTrue(actual.protection_context["emergencies"]["south"]["cool"]["measured"])
        self.assertEqual(actual.protection_context["preferred_emergency_mode"], "cool")
        self.assertGreaterEqual(actual.mode_scores["cool"][0], 1,
                                "Fresh actual emergency must reach arbitration before the slow comfort deadline")

    def test_equal_warm_weather_switch_bypasses_comfort_dwell_after_direction_confirmation(self):
        cfg, clock = configuration(), Clock()
        policy = Policy(cfg, clock=clock)
        first = self.decide(cfg, clock, {"north": 59, "south": 80.5}, policy=policy)
        self.assertEqual(first.mode, "heat")
        clock.tick = 15
        candidate = self.decide(cfg, clock, {"north": 59.5, "south": 80.5}, policy=policy,
                                modes={"north_head": "heat"})
        self.assertEqual(candidate.protection_context["preferred_emergency_mode"], "cool")
        self.assertEqual(policy.candidate[0], "cool")
        self.assertEqual(candidate.mode, "heat")
        clock.tick = 134
        waiting = self.decide(cfg, clock, {"north": 59.5, "south": 80.5}, policy=policy,
                              modes={"north_head": "heat"})
        self.assertEqual(waiting.mode, "heat")
        clock.tick = 135
        switched = self.decide(cfg, clock, {"north": 59.5, "south": 80.5}, policy=policy,
                               modes={"north_head": "heat"})
        self.assertEqual(switched.mode, "cool")
        self.assertLess(clock.tick, cfg.mode_hold_seconds)


if __name__ == "__main__":
    unittest.main()
