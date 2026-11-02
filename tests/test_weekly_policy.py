"""Reviewed weekly transitions, seasonal bands and explicit policy edits."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from house_climate.bindings import example_bindings
from house_climate.config import Config, Settings
from house_climate.comfort import next_schedule_change
from house_climate.house_policy import house_configuration, editable_policy_settings


LOCAL = ZoneInfo("America/Los_Angeles")


class WeeklyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.bindings = example_bindings()
        self.config = house_configuration(bindings=self.bindings)
        self.settings = Settings.read({}, self.config)

    def selected(self, room, stamp, season="cool"):
        self.settings.seasonal_profile = season
        selected = self.settings.profile(self.config.zones[room], datetime.fromisoformat(stamp).replace(tzinfo=LOCAL),
                                         self.config.timezone)
        return selected.target_f, selected.low_f, selected.high_f, selected.comfort_enabled, selected.priority

    def test_office_weekday_and_weekend_boundaries(self):
        for stamp, expected in (
            ("2026-11-02T07:59:59", (70, 67, 73, False, 1)),
            ("2026-11-02T08:00:00", (71, 70, 73, True, 2)),
            ("2026-11-02T18:00:00", (70, 67, 73, False, 1)),
            ("2026-11-07T07:00:00", (70, 67, 73, False, 1)),
            ("2026-11-07T12:00:00", (70, 67, 73, False, 1)),
            ("2026-11-08T23:59:59", (70, 67, 73, False, 1)),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.selected("office", stamp), expected)

    def test_entertainment_start_day_ownership_and_approved_exceptions(self):
        for stamp, expected in (
            ("2026-11-06T16:29:59", (70, 68, 73, False, 1)),
            ("2026-11-06T16:30:00", (70, 69, 72, True, 2)),
            ("2026-11-07T01:59:59", (70, 69, 72, True, 2)),
            ("2026-11-07T02:00:00", (70, 67, 74, False, 1)),
            ("2026-11-07T09:00:00", (70, 68, 73, True, 1)),
            ("2026-11-08T02:59:59", (70, 68, 73, True, 1)),
            ("2026-11-08T03:00:00", (70, 67, 74, False, 1)),
            ("2026-11-09T02:59:59", (70, 68, 73, True, 1)),
            ("2026-11-09T03:00:00", (70, 68, 73, False, 1)),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.selected("entertainment", stamp), expected)

    def test_guest_friday_saturday_and_sunday_monday_transitions(self):
        for stamp, expected in (
            ("2026-11-07T00:59:59", (70, 69, 73, True, 1)),
            ("2026-11-07T01:00:00", (70, 68, 74, False, 1)),
            ("2026-11-07T09:59:59", (70, 68, 74, False, 1)),
            ("2026-11-07T10:00:00", (70, 69, 73, True, 1)),
            ("2026-11-08T22:00:00", (70, 68, 74, False, 1)),
            ("2026-11-09T09:59:59", (70, 68, 74, False, 1)),
            ("2026-11-09T10:00:00", (70, 68, 73, False, 1)),
            ("2026-11-09T17:00:00", (70, 69, 73, True, 1)),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.selected("guest", stamp), expected)

    def test_living_seasonal_bands_are_not_capped_by_auxiliary_cutoff(self):
        self.assertEqual(self.selected("living", "2026-11-02T07:00:00"), (69, 68, 72, True, 1))
        self.assertEqual(self.selected("living", "2026-11-02T22:00:00"), (69, 66, 75, False, 1))
        self.assertEqual(self.selected("living", "2026-11-02T07:00:00", "warm"), (71, 69, 73, True, 1))
        self.assertEqual(self.selected("living", "2026-11-02T22:00:00", "warm"), (71, 68, 75, False, 1))
        self.assertEqual(self.config.heater_high_setpoint_f, 80)
        self.assertTrue(self.config.auxiliary_dynamic_stop)
        self.assertTrue(Config.from_dict(self.config.to_dict()).auxiliary_dynamic_stop)
        self.assertEqual((self.config.fallback.heat_low_f, self.config.fallback.cool_high_f,
                          self.config.fallback.fan_percentage), (68, 73, 40))

    def test_weekly_roundtrip_preserves_selection_across_entire_week(self):
        rebuilt = Config.from_dict(self.config.to_dict())
        settings = Settings.read({}, rebuilt)
        for day in range(2, 9):
            for hour in range(24):
                now = datetime(2026, 11, day, hour, 30, tzinfo=LOCAL)
                for room in self.config.zones:
                    self.assertEqual(settings.profile(rebuilt.zones[room], now, rebuilt.timezone),
                                     self.settings.profile(self.config.zones[room], now, self.config.timezone))

    def test_dynamic_stop_is_explicit_and_requires_a_boolean(self):
        raw = self.config.to_dict()
        raw.pop("auxiliary_dynamic_stop")
        self.assertFalse(Config.from_dict(raw).auxiliary_dynamic_stop)
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "dynamic stop"):
                Config.from_dict({**raw, "auxiliary_dynamic_stop": value})

    def test_cross_week_overlap_is_rejected_but_identical_limits_can_merge(self):
        raw = self.config.to_dict()
        room = raw["rooms"]["office"]
        room["schedules"] = [
            {"start": "22:00", "end": "08:00", "weekdays": [6], "target_f": 70},
            {"start": "07:00", "end": "09:00", "weekdays": [0], "target_f": 71},
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            Config.from_dict(raw)
        room["schedules"][1]["target_f"] = 70
        Config.from_dict(raw)

    def test_invalid_weekday_and_all_day_definitions_are_rejected(self):
        for extra in ({"weekdays": []}, {"weekdays": [True]}, {"weekdays": [7]},
                      {"weekdays": [0, 0]}, {"all_day": True}, {"all_day": "yes"}):
            raw = self.config.to_dict()
            raw["rooms"]["office"]["schedules"] = [{"start": "08:00", "end": "18:00", **extra}]
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_weekly_hold_boundary_can_be_more_than_three_days_away(self):
        raw = self.config.to_dict()
        raw["rooms"]["office"]["schedules"] = [{"start": "08:00", "end": "18:00", "weekdays": [4]}]
        config = Config.from_dict(raw)
        now = datetime(2026, 11, 6, 19, tzinfo=LOCAL)
        self.assertEqual(next_schedule_change(config.zones["office"], Settings.read({}, config), now, config.timezone),
                         datetime(2026, 11, 13, 8, tzinfo=LOCAL).astimezone(timezone.utc))

    def test_overnight_start_day_survives_fall_dst_repetition(self):
        for stamp in ("2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"):
            p = self.settings.profile(self.config.zones["entertainment"], datetime.fromisoformat(stamp), self.config.timezone)
            self.assertTrue(p.comfort_enabled)
            self.assertEqual((p.low_f, p.high_f), (68, 73))

    def test_explicit_edits_are_independent_of_migration_and_cannot_replace_wiring(self):
        edits = editable_policy_settings(self.config, self.bindings)
        edits["rooms"]["office"]["profile"]["target_f"] = 72
        edits["auxiliary_policy"]["peak_start"] = "16:00"
        edits["fallback"]["fan_percentage"] = 50
        before = deepcopy(edits)
        changed = house_configuration(self.config.to_dict(), self.bindings, policy_settings=edits)
        self.assertEqual(changed.zones["office"].profile.target_f, 72)
        self.assertEqual(changed.auxiliary_policy.peak_start, "16:00")
        self.assertEqual(changed.fallback.fan_percentage, 50)
        self.assertEqual(changed.heads, self.bindings.heads)
        self.assertEqual(edits, before)
        for forbidden in ({"heads": {}}, {"rooms": {"office": {"head": "living"}}}, {"fallback": None}):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                house_configuration(bindings=self.bindings, policy_settings=forbidden)


if __name__ == "__main__":
    unittest.main()
