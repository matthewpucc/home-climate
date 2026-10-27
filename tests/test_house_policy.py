"""The house's fixed operating contract and independently measured rooms."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from house_climate.config import Settings
from house_climate.house_policy import house_configuration
from house_climate.bindings import example_bindings
from house_climate.policy import Policy
from tests.test_rooms import states


LOCAL = ZoneInfo("America/Los_Angeles")


class HousePolicyTests(unittest.TestCase):
    def setUp(self):
        self.bindings = example_bindings()
        self.cfg = house_configuration(bindings=self.bindings)
        self.settings = Settings.read({}, self.cfg)

    def profile(self, hour, minute=0, second=0):
        now = datetime(2026, 10, 25, hour, minute, second, tzinfo=LOCAL)
        return self.settings.profile(self.cfg.zones["primary"], now, self.cfg.timezone)

    def test_four_physical_heads_and_staggered_bonus_room_share_one_head(self):
        self.cfg.validate(live=True)
        self.assertEqual(self.cfg.heads, self.bindings.heads)
        self.assertEqual(set(self.cfg.zones), {"office", "entertainment", "guest", "primary", "living"})
        self.assertEqual(self.cfg.zones["office"].head, "bonus")
        self.assertEqual(self.cfg.zones["entertainment"].head, "bonus")
        self.assertEqual(self.cfg.auxiliary_room, "living")
        self.assertEqual(self.cfg.low_f, 66)
        self.assertEqual(self.cfg.high_f, 75)

    def test_room_measurements_exclude_equipment_thermostats(self):
        selected = {p.entity_id for room in self.cfg.zones.values() for p in room.probes}
        self.assertFalse(any("heat_pump" in entity for entity in selected))
        self.assertNotIn("sensor.auxiliary_equipment_temperature", selected)
        self.assertEqual({p.entity_id for p in self.cfg.zones["living"].probes},
                         set(self.bindings.rooms["living"]))
        for key in ("office", "guest", "primary", "living"):
            sources = {p.source for p in self.cfg.zones[key].probes}
            self.assertGreaterEqual(len(sources), 2)
        self.assertEqual([p.entity_id for p in self.cfg.zones["entertainment"].probes],
                         list(self.bindings.rooms["entertainment"]))

    def test_sleep_and_morning_schedule_boundaries_and_fan_speed(self):
        for clock in ((22, 0, 0), (23, 59, 59), (0, 0, 0), (4, 59, 59)):
            with self.subTest(clock=clock):
                p = self.profile(*clock)
                self.assertTrue(p.comfort_enabled)
                self.assertEqual((p.low_f, p.high_f, p.target_f, p.priority, p.fan_percentage),
                                 (67, 69, 68, 10, 60))
        for clock in ((5, 0, 0), (8, 59, 59)):
            with self.subTest(clock=clock):
                p = self.profile(*clock)
                self.assertTrue(p.comfort_enabled)
                self.assertEqual((p.high_f, p.target_f, p.fan_percentage), (70, 69, 0))
        for clock in ((9, 0, 0), (12, 0, 0), (21, 59, 59)):
            with self.subTest(clock=clock):
                p = self.profile(*clock)
                self.assertFalse(p.comfort_enabled)
                self.assertEqual(p.fan_percentage, 0)

    def test_everyday_targets_priorities_and_cooling_floor(self):
        for key in ("office", "entertainment", "guest"):
            p = self.cfg.zones[key].profile
            self.assertEqual((p.target_f, p.low_f, p.high_f, p.priority), (72, 71, 73, 1))
        p = self.cfg.zones["living"].profile
        self.assertEqual((p.target_f, p.priority), (68, .1))
        self.assertEqual(self.cfg.head_cool_min_f, {entity: 65 for entity in self.bindings.heads.values()})
        self.assertEqual((self.cfg.poll_seconds, self.cfg.decision_interval_seconds), (15, 90))

    def test_two_office_fans_distribute_shared_entertainment_head_air(self):
        fans = self.cfg.fan_controls
        for entity in (self.bindings.fans["office"], self.bindings.fans["entertainment"]):
            self.assertEqual((fans[entity].room, fans[entity].source_room, fans[entity].strategy,
                              fans[entity].percentage), ("office", "entertainment", "transfer", 60))
        primary = fans[self.bindings.fans["primary"]]
        self.assertEqual(primary.strategy, "schedule")

    def test_cold_internal_equipment_readings_do_not_create_false_room_heat(self):
        now = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)  # 10pm Pacific
        data = states(self.cfg, {"primary": 70, "office": 72, "entertainment": 72.9,
                                "guest": 72, "living": 68}, now)
        baseline = Policy(self.cfg).decide(data, now, self.settings)
        data["sensor.shared_equipment_temperature"] = {
            "state": "62", "attributes": {"unit_of_measurement": "°F"},
            "last_reported": now.isoformat(),
        }
        data["sensor.sleep_equipment_temperature"] = {
            "state": "62", "attributes": {"unit_of_measurement": "°F"},
            "last_reported": now.isoformat(),
        }
        data[self.cfg.heads["bonus"]]["attributes"]["current_temperature"] = 62
        decision = Policy(self.cfg).decide(data, now, self.settings)
        self.assertEqual(decision.rooms["entertainment"]["temperature"], 72.9)
        self.assertEqual(decision.mode, "cool")
        self.assertEqual(decision.head_targets_f, baseline.head_targets_f)

    def test_lost_entertainment_probe_blocks_only_shared_bonus_head(self):
        now = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
        data = states(self.cfg, {"primary": 70, "office": 74, "entertainment": 74,
                                "guest": 72, "living": 68}, now)
        for probe in self.cfg.zones["entertainment"].probes:
            data[probe.entity_id]["state"] = "unavailable"
        decision = Policy(self.cfg).decide(data, now, self.settings)
        self.assertNotIn("bonus", decision.head_targets_f)
        self.assertIn("primary", decision.head_targets_f)
        self.assertEqual(decision.fans[self.bindings.fans["office"]], 0)
        self.assertEqual(decision.fans[self.bindings.fans["entertainment"]], 0)

    def test_old_tuning_cannot_override_rules_but_room_identity_is_preserved(self):
        old = self.cfg.to_dict()
        aliases = {key: f"saved_{i}" for i, key in enumerate(old["rooms"])}
        old["rooms"] = {aliases[key]: value for key, value in old["rooms"].items()}
        old["rooms"][aliases["office"]]["area_id"] = "bonus_floor"
        old["rooms"][aliases["office"]]["profile"] = {"target_f": 74, "priority": 10}
        old["low_f"], old["high_f"] = 55, 85
        old["head_cool_min_f"] = {}
        fixed = house_configuration(old, self.bindings)
        self.assertEqual(set(fixed.zones), set(aliases.values()))
        self.assertEqual(fixed.zones[aliases["office"]].profile.target_f, 72)
        self.assertEqual(fixed.zones[aliases["office"]].area_id, "bonus_floor")
        self.assertEqual(fixed.auxiliary_room, aliases["living"])
        self.assertEqual(fixed.fan_controls[self.bindings.fans["office"]].source_room,
                         aliases["entertainment"])
        self.assertEqual((fixed.low_f, fixed.high_f), (66, 75))
        self.assertEqual(fixed.head_cool_min_f, self.cfg.head_cool_min_f)

    def test_build_returns_fresh_wiring_without_mutating_saved_configuration(self):
        old = self.cfg.to_dict()
        original = deepcopy(old)
        current = house_configuration(old, self.bindings)
        current.heads["bonus"] = "climate.changed"
        self.assertEqual(old, original)
        self.assertEqual(house_configuration(bindings=self.bindings).heads, self.bindings.heads)


if __name__ == "__main__":
    unittest.main()
