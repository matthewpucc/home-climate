"""Temporary comfort profiles must keep the existing shared-system policy."""
import unittest
from dataclasses import replace
from datetime import datetime, time, timezone

from house_climate.comfort import next_schedule_change
from house_climate.config import Config, Period, Profile, Settings, Zone
from house_climate.policy import Policy
from tests.test_rooms import NOW, configuration, states


def instant(value):
    return datetime.fromisoformat(value)


class ComfortProfileTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_dict(configuration())
        self.settings = Settings.read({}, self.config)

    def test_override_changes_reported_band_and_actual_head_target(self):
        self.settings.room_profiles = {
            "alpha": replace(self.config.zones["alpha"].profile,
                             target_f=70, low_f=69, high_f=71)
        }
        plan = Policy(self.config).decide(
            states(self.config, {"alpha": 72, "beta": 71, "gamma": 68}),
            NOW, self.settings,
        )
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.head_targets_f["shared"], 70)
        self.assertEqual(
            tuple(plan.room_targets["alpha"][key] for key in ("low_f", "high_f", "target_f")),
            (69, 71, 70),
        )
        self.assertEqual(self.config.zones["alpha"].profile.target_f, 72)

    def test_override_preserves_scheduled_priority_and_fan_profile(self):
        room = self.config.zones["gamma"]
        base = self.settings.profile(room, NOW, self.config.timezone)
        self.settings.room_profiles = {
            "gamma": replace(base, target_f=72, low_f=71, high_f=73,
                             comfort_enabled=True, target_offset_f=0)
        }
        plan = Policy(self.config).decide(
            states(self.config, {"alpha": 73, "beta": 71, "gamma": 69}),
            NOW, self.settings,
        )
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(plan.head_targets_f["second"], 72)
        self.assertEqual(plan.room_targets["gamma"]["priority"], 8)
        self.assertEqual(plan.fans["fan.test_rest"], 60)
        self.assertEqual(self.settings.profile(room, NOW, self.config.timezone).target_f, 68)

    def test_manual_comfort_can_override_a_limits_only_day_profile(self):
        now = NOW.replace(hour=12)
        room = self.config.zones["gamma"]
        base = self.settings.profile(room, now, self.config.timezone)
        self.assertFalse(base.comfort_enabled)
        self.settings.room_profiles = {
            "gamma": replace(base, target_f=70, low_f=69, high_f=71,
                             comfort_enabled=True, target_offset_f=0)
        }
        plan = Policy(self.config).decide(
            states(self.config, {"alpha": 72, "beta": 71, "gamma": 72}, now),
            now, self.settings,
        )
        self.assertIn("second", plan.head_targets_f)
        self.assertEqual(plan.room_targets["gamma"]["target_f"], 70)
        self.assertTrue(plan.room_targets["gamma"]["comfort_enabled"])
        self.assertEqual(plan.fans["fan.test_rest"], 0)

    def test_incompatible_shared_room_comfort_blocks_both_directions_despite_priority(self):
        self.settings.room_profiles = {
            "alpha": Profile(target_f=74, low_f=73, high_f=75, priority=1),
            "beta": Profile(target_f=68, low_f=67, high_f=69, priority=5),
        }
        plan = Policy(self.config).decide(
            states(self.config, {"alpha": 72, "beta": 71, "gamma": 68}),
            NOW, self.settings,
        )
        self.assertTrue(plan.room_targets["alpha"]["heat_requested"])
        self.assertTrue(plan.room_targets["beta"]["cool_requested"])
        # Priority cannot start a biased call already past another attached
        # area's opposite comfort edge. Neither request concerns a house limit.
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_targets_f, {})
        blocked = plan.head_constraints["shared"]
        self.assertEqual(blocked["cool"]["room"], "alpha")
        self.assertEqual(blocked["heat"]["room"], "beta")
        self.assertEqual({detail["kind"] for detail in blocked.values()}, {"comfort_limit"})

    def test_removing_override_resumes_schedule_without_mutating_config(self):
        room = self.config.zones["gamma"]
        base = self.settings.profile(room, NOW, self.config.timezone)
        self.settings.room_profiles = {"gamma": replace(base, target_f=70, low_f=69, high_f=71)}
        policy = Policy(self.config)
        self.assertEqual(policy.band("gamma", self.settings, NOW), (69, 71, 70))
        self.settings.room_profiles = {}
        self.assertEqual(policy.band("gamma", self.settings, NOW), (67, 69, 68))
        self.assertEqual(room.schedules[0].profile.target_f, 68)


class ScheduleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings({}, {}, {})

    def room(self, *periods):
        return Zone("head", (), schedules=tuple(
            Period(start, end, Profile(target_f=68)) for start, end in periods))

    def boundary(self, room, now, zone="Etc/UTC"):
        return next_schedule_change(room, self.settings, instant(now), zone)

    def test_no_schedules_has_no_expiry(self):
        self.assertIsNone(self.boundary(self.room(), "2026-10-16T23:00:00+00:00"))

    def test_overnight_and_exact_boundaries(self):
        room = self.room(("22:00", "05:00"))
        for now, expected in (
            ("2026-10-16T21:00:00+00:00", "2026-10-16T22:00:00+00:00"),
            ("2026-10-16T22:00:00+00:00", "2026-10-17T05:00:00+00:00"),
            ("2026-10-17T04:59:59+00:00", "2026-10-17T05:00:00+00:00"),
            ("2026-10-17T05:00:00+00:00", "2026-10-17T22:00:00+00:00"),
        ):
            with self.subTest(now=now):
                self.assertEqual(self.boundary(room, now), instant(expected))

    def test_adjacent_equal_profiles_still_end_hold_at_period_change(self):
        room = self.room(("08:00", "12:00"), ("12:00", "16:00"))
        self.assertEqual(self.boundary(room, "2026-10-16T10:00:00+00:00"),
                         instant("2026-10-16T12:00:00+00:00"))

    def test_helper_backed_schedule_clocks_remain_supported(self):
        self.settings.times.update(start=time(22), finish=time(5))
        room = self.room(("@start", "@finish"))
        self.assertEqual(self.boundary(room, "2026-10-16T23:00:00+00:00"),
                         instant("2026-10-17T05:00:00+00:00"))

    def test_spring_clock_jump_enters_and_leaves_missing_boundary_periods(self):
        for room in (self.room(("02:30", "04:00")), self.room(("01:00", "02:30"))):
            with self.subTest(room=room):
                self.assertEqual(
                    self.boundary(room, "2026-03-08T09:50:00+00:00", "America/Los_Angeles"),
                    instant("2026-03-08T10:00:00+00:00"),
                )

    def test_entirely_missing_spring_period_does_not_create_false_expiry(self):
        room = self.room(("02:10", "02:50"))
        self.assertEqual(
            self.boundary(room, "2026-03-08T09:30:00+00:00", "America/Los_Angeles"),
            instant("2026-03-09T09:10:00+00:00"),
        )

    def test_both_fall_occurrences_of_a_boundary_are_used(self):
        room = self.room(("01:15", "01:45"))
        for now, expected in (
            ("2026-11-01T08:20:00+00:00", "2026-11-01T08:45:00+00:00"),
            ("2026-11-01T08:50:00+00:00", "2026-11-01T09:15:00+00:00"),
            ("2026-11-01T09:20:00+00:00", "2026-11-01T09:45:00+00:00"),
        ):
            with self.subTest(now=now):
                self.assertEqual(self.boundary(room, now, "America/Los_Angeles"), instant(expected))

    def test_fall_clock_jump_can_leave_a_period_before_nominal_end(self):
        room = self.room(("01:30", "02:30"))
        self.assertEqual(
            self.boundary(room, "2026-11-01T08:50:00+00:00", "America/Los_Angeles"),
            instant("2026-11-01T09:00:00+00:00"),
        )
        self.assertEqual(
            self.boundary(room, "2026-11-01T09:40:00+00:00", "America/Los_Angeles"),
            instant("2026-11-01T10:30:00+00:00"),
        )

    def test_overnight_period_does_not_expire_at_an_irrelevant_dst_jump(self):
        room = self.room(("22:00", "05:00"))
        self.assertEqual(
            self.boundary(room, "2026-11-01T08:30:00+00:00", "America/Los_Angeles"),
            instant("2026-11-01T13:00:00+00:00"),
        )

    def test_timezone_aware_input_is_required(self):
        with self.assertRaises(ValueError):
            next_schedule_change(self.room(("22:00", "05:00")), self.settings,
                                 datetime(2026, 10, 16, 23), "Etc/UTC")
