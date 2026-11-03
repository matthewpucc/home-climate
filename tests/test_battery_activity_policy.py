"""Unchanged SOC remains a value, not a fabricated fresh acquisition."""
from datetime import datetime, timezone
import unittest
from tests.test_power_guard import B, Clock, measurements, power


class BatteryActivityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.states = measurements(self.clock)
        self.original = datetime.fromtimestamp(self.clock.wall - 3600, timezone.utc).isoformat()
        self.states[B.battery].update(state="100", last_reported=self.original, last_updated=self.original)
        self.activity = {"entity_id": B.output_power, "reported_at": self.clock.wall}

    def reading(self, activity=None):
        return power.evaluate_power(self.states, B, self.clock.wall, battery_activity=activity)

    def test_old_last_updated_with_new_report_already_qualifies(self):
        self.states[B.battery]["last_reported"] = datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()
        r = self.reading()
        self.assertIsNone(r.issue)
        self.assertEqual(r.battery_evidence, "ha_report")
        self.assertIsNone(r.battery_activity_at)

    def test_old_available_state_alone_is_insufficient(self):
        self.assertIsNotNone(self.reading().issue)
        self.assertEqual(self.reading().battery_evidence, "unverified")

    def test_activity_keeps_original_battery_timestamp(self):
        r = self.reading(self.activity)
        self.assertIsNone(r.issue)
        self.assertTrue(r.safe_to_rearm)
        self.assertEqual(r.battery_percent, 100)
        self.assertEqual(r.battery_evidence, "same_device_activity")
        self.assertEqual(r.battery_reported_at, self.clock.wall - 3600)
        self.assertEqual(r.battery_activity_at, self.clock.wall)
        self.assertEqual(self.states[B.battery]["last_reported"], self.original)

    def test_activity_cannot_override_reserve_or_invalid_soc(self):
        for value in ("40", "39.9", "0", "101", "-1", "NaN", "inf", "unknown", "unavailable", True):
            with self.subTest(value=value):
                self.states[B.battery]["state"] = value
                r = self.reading(self.activity)
                self.assertIsNotNone(r.issue)
                self.assertFalse(r.safe_to_rearm)
        self.states[B.battery]["state"] = "40.01"
        self.assertIsNone(self.reading(self.activity).issue)

    def test_activity_cannot_override_untrusted_or_invalid_battery_time(self):
        for name in ("restored", "assumed_state"):
            self.states[B.battery]["attributes"][name] = True
            self.assertIsNotNone(self.reading(self.activity).issue)
            self.states[B.battery]["attributes"].pop(name)
        for stamp in (None, "invalid", datetime.fromtimestamp(self.clock.wall + 6, timezone.utc).isoformat()):
            self.states[B.battery]["last_reported"] = stamp
            self.states[B.battery]["last_updated"] = stamp
            self.assertIsNotNone(self.reading(self.activity).issue)

    def test_activity_boundary_and_invalid_witness(self):
        self.activity["reported_at"] = self.clock.wall - 180
        self.assertIsNone(self.reading(self.activity).issue)
        for stamp in (self.clock.wall - 180.001, self.clock.wall + .001, float("nan"), float("inf"), True, None):
            with self.subTest(stamp=stamp):
                self.activity["reported_at"] = stamp
                self.assertIsNotNone(self.reading(self.activity).issue)
        self.activity.update(reported_at=self.clock.wall, entity_id=B.circuit_power)
        self.assertIsNotNone(self.reading(self.activity).issue)

    def test_output_still_requires_current_valid_report(self):
        for state in ("unavailable", "NaN", "-1"):
            self.states[B.output_power]["state"] = state
            self.assertIsNotNone(self.reading(self.activity).issue)
        self.states[B.output_power]["state"] = "2200"
        self.states[B.output_power]["last_reported"] = self.original
        self.assertIsNotNone(self.reading(self.activity).issue)
        self.states[B.output_power]["last_reported"] = datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()
        self.assertIsNone(self.reading(self.activity).issue)

    def test_activity_does_not_replace_stop_or_ac_evidence(self):
        self.states[B.heater_current]["state"] = "10"
        self.assertFalse(self.reading(self.activity).safe_to_rearm)
        self.states[B.ac_output_switch]["state"] = "off"
        self.states[B.ac_output_switch]["attributes"].pop("device_report", None)
        self.assertIsNotNone(self.reading(self.activity).issue)
