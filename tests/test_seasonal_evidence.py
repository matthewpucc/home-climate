"""Seasonal comfort uses continuous, source-separated bounded history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from house_climate.config import SeasonalPolicy
from house_climate.outdoor import OutdoorReading, OutdoorSourceSample
from house_climate.seasonal import SeasonalEvaluator

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def source(value, second, *, name="local", role="primary", weather=False, observed=True, valid_for=1800):
    stamp = NOW.timestamp() + second
    return OutdoorSourceSample(name, role, value, stamp,
        stamp if weather and observed else None, stamp + valid_for,
        ("weather." if weather else "sensor.") + name)


def reading(*samples, role="primary"):
    selected = [s for s in samples if s.role == role]
    return OutdoorReading(min((s.temperature_f for s in selected), default=None),
        len(selected), "known" if selected else "unknown", selected_role=role if selected else None,
        source_samples=samples, primary_configured=True)


class SeasonalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.evaluator = SeasonalEvaluator(SeasonalPolicy(enabled=True))

    def feed(self, value, *, end=1800, role="primary", weather=False):
        for second in range(0, end + 1, 300):
            result = self.evaluator.update(reading(source(value, second, role=role, weather=weather), role=role),
                                           NOW + timedelta(seconds=second))
        return result

    def test_full_window_is_required_and_cache_age_is_not_backfilled(self):
        first = self.evaluator.update(reading(source(65, -600)), NOW)
        self.assertEqual((first.profile, first.coverage_seconds), ("cool", 0))
        self.assertEqual(first.evidence_status, "warming")
        self.assertFalse(first.degraded)
        result = self.feed(65, end=1500)
        self.assertEqual((result.profile, result.coverage_seconds), ("cool", 1500))
        result = self.evaluator.update(reading(source(65, 1800)), NOW + timedelta(seconds=1800))
        self.assertEqual((result.profile, result.coverage_seconds, result.average_f), ("warm", 1800, 65))
        self.assertEqual(result.evidence_status, "qualified")

    def test_exact_boundaries_retain_previous_profile(self):
        for previous, temperature in (("cool", 61), ("warm", 59), ("warm", 60), ("cool", 60)):
            with self.subTest(previous=previous, temperature=temperature):
                self.evaluator = SeasonalEvaluator(SeasonalPolicy(enabled=True))
                self.evaluator.restore({"profile": previous})
                self.assertEqual(self.feed(temperature).profile, previous)

    def test_cold_average_switches_back_to_cool(self):
        self.evaluator.restore({"profile": "warm"})
        self.assertEqual(self.feed(58).profile, "cool")

    def test_repeated_cache_cannot_extend_expiry(self):
        cached = reading(source(65, 0))
        self.evaluator.update(cached, NOW)
        at_expiry = self.evaluator.update(cached, NOW + timedelta(seconds=1800))
        self.assertEqual(at_expiry.profile, "cool")
        self.assertTrue(at_expiry.degraded)
        self.assertEqual(at_expiry.evidence_status, "unavailable")
        self.assertEqual(at_expiry.coverage_seconds, 0)

    def test_disappearance_breaks_coverage_even_if_cache_timestamp_still_valid(self):
        self.feed(65, end=600)
        self.evaluator.update(OutdoorReading(), NOW + timedelta(seconds=900))
        result = None
        for second in (1200, 1500, 1800):
            result = self.evaluator.update(reading(source(65, second)), NOW + timedelta(seconds=second))
        self.assertEqual((result.profile, result.coverage_seconds), ("cool", 600))

    def test_fallback_history_is_separate_and_can_be_prequalified(self):
        for second in range(0, 1801, 300):
            samples = (source(55, second), source(65, second, name="airport", role="fallback", weather=True))
            local = self.evaluator.update(reading(*samples), NOW + timedelta(seconds=second))
        self.assertEqual(local.profile, "cool")
        fallback = self.evaluator.update(reading(source(65, 1801, name="airport", role="fallback", weather=True),
                                                 role="fallback"), NOW + timedelta(seconds=1801))
        self.assertEqual((fallback.profile, fallback.average_f), ("warm", 65))
        self.assertTrue(fallback.degraded)
        self.assertEqual(fallback.evidence_status, "qualified")
        self.assertEqual(fallback.source_ids, ("airport",))

    def test_weather_without_acquisition_timestamp_does_not_masquerade_as_observation(self):
        self.evaluator.restore({"profile": "warm"})
        for second in range(0, 2101, 300):
            result = self.evaluator.update(reading(source(45, second, role="fallback", weather=True, observed=False),
                                                   role="fallback"), NOW + timedelta(seconds=second))
        self.assertEqual(result.profile, "warm")
        self.assertIn("physical observation", result.reason)
        self.assertEqual(result.evidence_status, "unverified")
        self.assertEqual(result.coverage_seconds, 0)

    def test_source_replacement_does_not_inherit_previous_temperature_history(self):
        self.feed(58)
        result = self.evaluator.update(reading(source(65, 1801, name="replacement")),
                                       NOW + timedelta(seconds=1801))
        self.assertEqual((result.profile, result.coverage_seconds), ("cool", 0))

    def test_restart_restores_only_profile_not_measurement_history(self):
        self.feed(65)
        saved = self.evaluator.dump()
        self.assertEqual(saved, {"profile": "warm"})
        restored = SeasonalEvaluator(SeasonalPolicy(enabled=True))
        self.assertTrue(restored.restore(saved))
        result = restored.update(reading(source(45, 2100)), NOW + timedelta(seconds=2100))
        self.assertEqual((result.profile, result.coverage_seconds), ("warm", 0))

    def test_unknown_and_clock_reversal_retain_profile_without_fabricating_average(self):
        self.feed(65)
        missing = self.evaluator.update(OutdoorReading(), NOW + timedelta(seconds=1801))
        self.assertEqual(missing.profile, "warm")
        self.assertIsNone(missing.average_f)
        backwards = self.evaluator.update(reading(source(45, 1000)), NOW + timedelta(seconds=1000))
        self.assertEqual((backwards.profile, backwards.coverage_seconds), ("warm", 0))

    def test_invalid_persisted_profile_is_ignored(self):
        for value in ({"profile": "automatic"}, {"profile": True}, None, "warm"):
            self.assertFalse(self.evaluator.restore(value))
            self.assertEqual(self.evaluator.profile, "cool")

    def test_window_is_time_weighted_not_poll_count_weighted(self):
        for second in (0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300):
            self.evaluator.update(reading(source(50, second)), NOW + timedelta(seconds=second))
        self.evaluator.update(reading(source(65, 900)), NOW + timedelta(seconds=900))
        result = self.evaluator.update(reading(source(65, 1800)), NOW + timedelta(seconds=1800))
        self.assertAlmostEqual(result.average_f, 57.5)
        self.assertEqual(result.profile, "cool")
