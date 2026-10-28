"""Aggregate AC evidence corroborates native Off without attributing other loads."""
from dataclasses import replace
import math
import unittest

from house_climate.aggregate_stop import (
    MAX_BASELINE_SAMPLES, AggregateStopBaseline, AggregateStopConfig, AggregateStopEvidence,
)


class AggregateStopConfigTests(unittest.TestCase):
    def test_explicit_calibration_round_trip_and_defaults(self):
        raw = {"minimum_drop_w": 500, "residual_ceiling_w": 300, "noise_margin_w": 20}
        cfg = AggregateStopConfig.from_dict(raw)
        self.assertEqual(cfg.settle_seconds, 10)
        self.assertEqual(cfg.timeout_seconds, 45)
        self.assertEqual(cfg.max_age_seconds, 30)
        self.assertEqual(cfg.minimum_samples, 2)
        self.assertEqual(AggregateStopConfig.from_dict(cfg.to_dict()), cfg)

    def test_configuration_requires_calibration_not_device_defaults(self):
        for raw in (None, {}, {"minimum_drop_w": 500, "residual_ceiling_w": 300},
                    {"minimum_drop_w": 500, "residual_ceiling_w": 300,
                     "noise_margin_w": 20, "assume_heater_w": 1000}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                AggregateStopConfig.from_dict(raw)

    def test_invalid_calibration_and_timing_are_rejected(self):
        cfg = AggregateStopConfig(500, 300, 20)
        invalid = (("minimum_drop_w", 20), ("noise_margin_w", -1),
                   ("residual_ceiling_w", 0), ("settle_seconds", -1),
                   ("timeout_seconds", 10), ("max_age_seconds", 0),
                   ("minimum_samples", 1), ("minimum_samples", 2.0),
                   ("minimum_samples", True))
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                replace(cfg, **{key: value})
        for key in ("minimum_drop_w", "residual_ceiling_w", "noise_margin_w",
                    "settle_seconds", "timeout_seconds", "max_age_seconds"):
            for value in (True, "500", math.inf, math.nan, 10 ** 1000):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    replace(cfg, **{key: value})


class AggregateStopEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AggregateStopConfig(500, 300, 20)
        self.evidence = AggregateStopEvidence(self.cfg)
        self.evidence.begin(100, 1000, 99)

    def observe(self, now, watts=200, reported=None, off=101):
        return self.evidence.observe(now, watts, now if reported is None else reported, off)

    def test_large_drop_and_distinct_settled_reports_corroborate_off(self):
        first = self.observe(103, reported=102)
        self.assertEqual(first.status, "pending")
        result = self.observe(113, 205, reported=112)
        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.classification, "drop")
        self.assertEqual((result.baseline_w, result.latest_w), (1000, 205))
        self.assertEqual((result.sample_count, result.evidence_seconds), (2, 10))

    def test_low_residual_is_required_even_after_a_large_drop(self):
        self.evidence.begin(100, 2000, 99)
        self.assertEqual(self.observe(103, 1000, 102).status, "pending")
        result = self.observe(113, 1000, 112)
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.classification)
        self.assertIn("other loads", result.reason)

    def test_drop_must_exceed_calibrated_margin(self):
        self.evidence.begin(100, 800, 99)
        self.assertEqual(self.observe(103, 300, 102).status, "pending")
        self.assertEqual(self.observe(113, 300, 112).status, "pending")
        self.assertEqual(self.observe(114, 280, 113).status, "pending")
        self.assertEqual(self.observe(124, 280, 123).status, "corroborated")

    def test_already_low_output_handles_a_thermostatically_idle_heater(self):
        self.evidence.begin(100, 200, 99)
        self.observe(103, 195, 102)
        result = self.observe(113, 200, 112)
        self.assertEqual((result.status, result.classification), ("corroborated", "already_low"))

    def test_idle_or_other_load_drop_without_native_off_never_corroborates(self):
        for baseline in (200, 2000):
            with self.subTest(baseline=baseline):
                self.evidence.begin(100, baseline, 99)
                self.assertEqual(self.observe(103, 100, 102, off=None).status, "unknown")
                self.assertEqual(self.observe(113, 100, 112, off=None).status, "unknown")

    def test_native_off_must_follow_the_request_and_be_current(self):
        for off in (None, 99, 100, 104, math.nan, True, "101"):
            with self.subTest(off=off):
                self.evidence.begin(100, 1000, 99)
                self.assertEqual(self.observe(103, 200, 102, off=off).status, "unknown")
        self.assertEqual(self.observe(140, 200, 139, off=101).status, "unknown")

    def test_output_before_or_at_off_cannot_count(self):
        for stamp in (100, 101):
            with self.subTest(stamp=stamp):
                self.evidence.begin(100, 1000, 99)
                result = self.observe(103, 200, stamp)
                self.assertEqual((result.status, result.sample_count), ("pending", 0))

    def test_unchanged_off_refresh_does_not_erase_a_settling_window(self):
        self.observe(103, 200, 102)
        result = self.observe(114, 200, 113, off=112)
        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.evidence_seconds, 11)

    def test_repeated_scrapes_do_not_count_as_independent_reports(self):
        self.observe(103, 200, 102)
        result = self.observe(114, 200, 102)
        self.assertEqual((result.status, result.sample_count, result.evidence_seconds), ("pending", 1, 0))

    def test_longer_sample_requirement_uses_source_reports(self):
        self.evidence = AggregateStopEvidence(replace(self.cfg, minimum_samples=3))
        self.evidence.begin(100, 1000, 99)
        self.observe(103, 200, 102)
        self.assertEqual(self.observe(113, 200, 112).status, "pending")
        self.assertEqual(self.observe(114, 200, 113).status, "corroborated")

    def test_changing_value_with_same_timestamp_invalidates_evidence(self):
        self.observe(103, 200, 102)
        result = self.observe(104, 205, 102)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.sample_count, 0)

    def test_noisy_candidate_restarts_the_settle_interval(self):
        self.observe(103, 200, 102)
        result = self.observe(113, 225, 112)
        self.assertEqual((result.status, result.sample_count), ("pending", 1))
        self.assertEqual(self.observe(123, 220, 122).status, "corroborated")

    def test_fresh_large_load_cannot_be_interpreted_as_heater_still_on(self):
        self.observe(103, 200, 102)
        result = self.observe(113, 1500, 112)
        self.assertEqual((result.status, result.sample_count), ("pending", 0))
        self.assertIn("other loads", result.reason)
        self.assertNotIn("heater remains", result.reason)

    def test_timeout_is_unknown_not_an_instruction_to_cut_power(self):
        result = self.observe(145, 200, 144, off=144)
        self.assertEqual(result.status, "unknown")
        self.assertIn("timed out", result.reason)
        self.assertEqual(self.observe(146, 200, 145, off=145), result)
        self.assertNotIn("action", result.as_dict())

    def test_invalid_baselines_need_an_explicit_new_attempt(self):
        for power, stamp in ((None, 99), (-1, 99), (math.inf, 99), (10 ** 1000, 99),
                             (1000, 69), (1000, 101)):
            with self.subTest(power=power, stamp=stamp):
                result = self.evidence.begin(100, power, stamp)
                self.assertEqual(result.status, "unknown")
                self.assertEqual(self.observe(113, 200, 112), result)
        self.evidence.begin(100, 1000, 99)
        self.assertEqual(self.observe(103, 200, 102).status, "pending")

    def test_age_boundary_is_inclusive_but_future_reports_are_not(self):
        self.assertEqual(self.evidence.begin(100, 1000, 70).status, "pending")
        result = self.observe(103, 200, 104)
        self.assertEqual(result.status, "unknown")
        self.assertIn("future", result.reason)

    def test_invalid_post_data_drops_proof_and_requires_new_reports(self):
        self.observe(103, 200, 102)
        self.assertEqual(self.observe(113, 200, 112).status, "corroborated")
        self.assertEqual(self.observe(114, None, 113).status, "unknown")
        # A cached report preceding the invalid observation cannot bridge it.
        self.assertEqual(self.observe(115, 200, 112).sample_count, 0)
        self.assertEqual(self.observe(116, 200, 115).status, "pending")
        self.assertEqual(self.observe(126, 200, 125, off=120).status, "corroborated")

    def test_stale_post_data_and_out_of_order_reports_reset_the_window(self):
        self.observe(103, 200, 102)
        self.assertEqual(self.observe(114, 200, 101).status, "unknown")
        self.assertEqual(self.observe(115, 200, 105).sample_count, 0)
        self.observe(116, 200, 115)
        self.assertEqual(self.observe(140, 200, 109, off=139).status, "unknown")

    def test_clock_regression_is_terminal_for_this_attempt(self):
        self.observe(103, 200, 102)
        result = self.observe(102, 200, 102)
        self.assertEqual(result.status, "unknown")
        self.assertIn("backwards", result.reason)
        self.assertEqual(self.observe(114, 200, 113), result)

    def test_hidden_validity_gap_cannot_bridge_the_settling_interval(self):
        self.observe(103, 200, 102)
        result = self.observe(143, 200, 142, off=141)
        self.assertEqual((result.status, result.sample_count, result.evidence_seconds), ("pending", 1, 0))

    def test_exactly_touching_evidence_validity_preserves_continuity(self):
        self.observe(103, 200, 102)
        result = self.observe(133, 200, 132, off=131)
        self.assertEqual(result.status, "corroborated")
        self.assertEqual((result.sample_count, result.evidence_seconds), (2, 30))

    def test_lost_corroboration_cannot_start_a_new_window_after_timeout(self):
        self.observe(103, 200, 102)
        self.assertEqual(self.observe(113, 200, 112).status, "corroborated")
        result = self.observe(146, 200, 145, off=145)
        self.assertEqual(result.status, "unknown")
        self.assertIn("timed out", result.reason)

    def test_native_off_loss_revokes_corroboration(self):
        self.observe(103, 200, 102)
        self.assertEqual(self.observe(113, 200, 112).status, "corroborated")
        self.assertEqual(self.observe(114, 200, 113, off=None).status, "unknown")
        self.assertEqual(self.observe(116, 200, 115, off=115).sample_count, 0)
        self.observe(117, 200, 116, off=115)
        self.assertEqual(self.observe(127, 200, 126, off=125).status, "corroborated")

    def test_evidence_is_constant_size_and_contains_no_physical_claim(self):
        self.observe(103, 200, 102)
        result = self.observe(113, 200, 112)
        for now in range(114, 300):
            result = self.observe(now, 200, now, off=now - .1)
        self.assertEqual(result.status, "corroborated")
        self.assertFalse(any(isinstance(value, (list, dict, set))
                             for value in vars(self.evidence).values()))
        self.assertNotIn("isolated", result.as_dict())


class AggregateRelativeConfigTests(unittest.TestCase):
    def test_opt_in_is_a_strict_serialized_boolean(self):
        legacy = AggregateStopConfig(1000, 160, 20)
        self.assertFalse(legacy.relative_drop_enabled)
        enabled = replace(legacy, relative_drop_enabled=True, minimum_samples=3)
        self.assertEqual(AggregateStopConfig.from_dict(enabled.to_dict()), enabled)
        self.assertTrue(enabled.to_dict()["relative_drop_enabled"])
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(enabled, relative_drop_enabled=value)

    def test_relative_size_and_fresh_window_limits_do_not_change_legacy(self):
        legacy = AggregateStopConfig(1000, 160, 20)
        replace(legacy, minimum_samples=MAX_BASELINE_SAMPLES + 1)
        replace(legacy, settle_seconds=31)
        relative = replace(legacy, relative_drop_enabled=True, minimum_samples=3)
        for changes in ({"minimum_samples": 2},
                        {"minimum_samples": MAX_BASELINE_SAMPLES + 1}, {"settle_seconds": 31}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(relative, **changes)
        replace(legacy, relative_drop_enabled=True, minimum_samples=MAX_BASELINE_SAMPLES,
                settle_seconds=30)


class AggregateStopBaselineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AggregateStopConfig(1000, 160, 20, minimum_samples=3,
                                       relative_drop_enabled=True)
        self.baseline = self.cfg.create_baseline()

    def qualify(self):
        for now, watts in ((100, 2000), (105, 1995), (110, 2005)):
            self.baseline.observe(now, watts, now)
        return self.baseline.snapshot(110)

    def test_factory_qualifies_the_conservative_low(self):
        self.assertIsInstance(self.baseline, AggregateStopBaseline)
        self.assertEqual(self.qualify(), 1995)
        self.assertEqual(self.baseline.snapshot(111), 1995)
        with self.assertRaises(TypeError):
            AggregateStopBaseline({})

    def test_distinct_unchanged_reports_count_but_cached_reads_do_not(self):
        self.baseline.observe(100, 2000, 100)
        self.baseline.observe(105, 2000, 100)
        self.assertIsNone(self.baseline.snapshot(110))
        self.baseline.observe(111, 2000, 105)
        self.assertEqual(self.baseline.observe(112, 2000, 110), 2000)

    def test_report_count_does_not_replace_source_settling_time(self):
        for now in (100, 101, 102):
            self.baseline.observe(now, 2000, now)
        self.assertIsNone(self.baseline.snapshot(112))
        self.assertEqual(self.baseline.observe(113, 2000, 113), 2000)

    def test_known_spike_discards_the_old_stable_window(self):
        self.qualify()
        self.assertIsNone(self.baseline.observe(111, 3000, 111))
        self.assertIsNone(self.baseline.observe(112, 2000, 112))
        self.assertIsNone(self.baseline.observe(117, 2005, 117))
        self.assertEqual(self.baseline.observe(122, 2000, 122), 2000)

    def test_invalid_observation_bars_cached_reports_from_before_it(self):
        self.qualify()
        self.baseline.observe(111, None, None)
        self.assertIsNone(self.baseline.observe(112, 2005, 110))
        self.assertIsNone(self.baseline.observe(113, 2005, 111))
        self.assertEqual(len(self.baseline._samples), 0)
        self.baseline.observe(114, 2000, 112)
        self.baseline.observe(119, 2000, 117)
        self.assertEqual(self.baseline.observe(124, 2000, 122), 2000)

    def test_invalid_source_data_resets_qualification(self):
        invalid = ((None, 111), (-1, 111), (math.inf, 111), (True, 111),
                   ("2000", 111), (2000, 112), (2000, 80),
                   (2000, 109), (2001, 110), (2000, math.nan))
        for watts, stamp in invalid:
            with self.subTest(watts=watts, stamp=stamp):
                self.baseline = self.cfg.create_baseline()
                self.qualify()
                self.assertIsNone(self.baseline.observe(111, watts, stamp))
                self.assertIsNone(self.baseline.snapshot(112))
                self.assertIsNone(self.baseline.observe(113, 2005, 110))

    def test_hidden_gap_starts_a_fresh_window(self):
        self.qualify()
        self.assertIsNone(self.baseline.observe(141, 2000, 141))
        self.assertEqual(len(self.baseline._samples), 1)
        self.baseline.observe(146, 2000, 146)
        self.assertEqual(self.baseline.observe(151, 2000, 151), 2000)

    def test_stale_snapshot_blocks_a_late_read_of_old_reports(self):
        self.qualify()
        self.assertIsNone(self.baseline.snapshot(141))
        self.assertIsNone(self.baseline.observe(142, 2000, 140))
        self.baseline.observe(143, 2000, 142)
        self.baseline.observe(148, 2000, 147)
        self.assertEqual(self.baseline.observe(153, 2000, 152), 2000)

    def test_all_retained_reports_must_be_fresh_with_inclusive_boundary(self):
        self.baseline = replace(self.cfg, max_age_seconds=10).create_baseline()
        self.assertEqual(self.qualify(), 1995)
        self.assertIsNone(self.baseline.snapshot(110.001))

    def test_clock_regression_or_invalid_snapshot_clock_revokes_history(self):
        for now in (109, None, math.inf, math.nan, True):
            with self.subTest(now=now):
                self.baseline = self.cfg.create_baseline()
                self.qualify()
                self.assertIsNone(self.baseline.snapshot(now))
                self.assertIsNone(self.baseline.snapshot(111))
                self.baseline.observe(112, 2000, 111)
                self.baseline.observe(117, 2000, 116)
                self.assertEqual(self.baseline.observe(122, 2000, 121), 2000)

    def test_dense_history_is_bounded_and_overflow_fails_conservative(self):
        for index in range(MAX_BASELINE_SAMPLES):
            now = 100 + index / 1000
            self.assertIsNone(self.baseline.observe(now, 2000, now))
        self.assertEqual(len(self.baseline._samples), MAX_BASELINE_SAMPLES)
        self.assertIsNone(self.baseline.observe(111, 2000, 111))
        self.assertEqual(len(self.baseline._samples), 1)
        self.baseline.observe(116, 2000, 116)
        self.assertEqual(self.baseline.observe(121, 2000, 121), 2000)


class AggregateRelativeStopTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AggregateStopConfig(1000, 160, 20, minimum_samples=3,
                                       relative_drop_enabled=True)
        self.evidence = self.cfg.create_evidence()

    def posts(self, watts=(800, 805, 800)):
        for now, value in zip((103, 108, 113), watts):
            result = self.evidence.observe(now, value, now, native_off_after=101)
        return result

    def test_high_background_uses_opt_in_qualified_relative_drop(self):
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
        result = self.posts()
        self.assertEqual((result.status, result.classification), ("corroborated", "relative_drop"))
        self.assertEqual((result.baseline_w, result.qualified_baseline_w), (2250, 2240))
        self.assertEqual((result.sample_count, result.evidence_seconds), (3, 10))
        self.assertNotIn("action", result.as_dict())
        self.evidence = replace(self.cfg, relative_drop_enabled=False).create_evidence()
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
        self.assertEqual(self.posts().status, "pending")

    def test_missing_relative_baseline_cannot_reuse_one_high_begin_sample(self):
        result = self.evidence.begin(100, 2250, 99)
        self.assertEqual(result.status, "unknown")
        self.assertIn("baseline", result.reason)
        self.assertEqual(self.posts((100, 105, 100)), result)

    def test_already_low_path_needs_no_heating_baseline(self):
        self.evidence.begin(100, 100, 99)
        result = self.posts((95, 100, 95))
        self.assertEqual((result.status, result.classification), ("corroborated", "already_low"))
        self.assertIsNone(result.qualified_baseline_w)

    def test_high_unchanged_or_partial_drop_is_not_an_already_low_bypass(self):
        for values in ((2250, 2250, 2250), (1500, 1500, 1500)):
            with self.subTest(values=values):
                self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
                result = self.posts(values)
                self.assertEqual(result.status, "pending")
                self.assertIsNone(result.classification)

    def test_qualified_low_cannot_inflate_fresh_begin_output(self):
        for qualified in (2251, -1, math.inf, math.nan, True, "2200", 10 ** 1000):
            with self.subTest(qualified=qualified):
                result = self.evidence.begin(100, 2250, 99, qualified_baseline_w=qualified)
                self.assertEqual(result.status, "unknown")
                self.assertEqual(self.posts(), result)

    def test_margin_uses_qualified_low_and_maximum_stable_post(self):
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2200)
        self.assertEqual(self.posts((1200, 1200, 1200)).status, "pending")
        for now, watts in ((114, 1170), (119, 1175), (124, 1170)):
            result = self.evidence.observe(now, watts, now, native_off_after=120 if now > 120 else 101)
        self.assertEqual(result.classification, "relative_drop")
        self.evidence.begin(100, 2000, 99, qualified_baseline_w=2000)
        self.evidence.observe(103, 980, 103, native_off_after=101)
        result = self.evidence.observe(108, 985, 108, native_off_after=101)
        self.assertEqual((result.status, result.sample_count), ("pending", 0))
        for now, watts in ((113, 970), (118, 975), (123, 980)):
            result = self.evidence.observe(now, watts, now, native_off_after=101)
        self.assertEqual(result.classification, "relative_drop")

    def test_baseline_is_frozen_for_the_stop_attempt(self):
        baseline = self.cfg.create_baseline()
        for now, watts in ((85, 2240), (90, 2250), (95, 2245)):
            baseline.observe(now, watts, now)
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=baseline.snapshot(100))
        for now in (101, 106, 111):
            baseline.observe(now, 4000, now)
        self.assertEqual(baseline.snapshot(111), 4000)
        result = self.posts()
        self.assertEqual((result.classification, result.qualified_baseline_w), ("relative_drop", 2240))

    def test_other_load_drop_without_native_off_cannot_corroborate(self):
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
        for now in (103, 108, 113):
            result = self.evidence.observe(now, 800, now, native_off_after=None)
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.classification)

    def test_relative_noise_duplicates_and_native_off_loss(self):
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
        self.evidence.observe(103, 800, 103, native_off_after=101)
        self.assertEqual(self.evidence.observe(113, 800, 103, native_off_after=101).sample_count, 1)
        self.evidence.observe(114, 830, 114, native_off_after=101)
        self.assertEqual(self.evidence.observe(119, 810, 119, native_off_after=101).status, "pending")
        result = self.evidence.observe(124, 815, 124, native_off_after=101)
        self.assertEqual(result.classification, "relative_drop")
        self.assertEqual(self.evidence.observe(125, 810, 125, native_off_after=None).status, "unknown")

    def test_relative_settling_cannot_bridge_hidden_source_gaps(self):
        self.evidence.begin(100, 2250, 99, qualified_baseline_w=2240)
        self.evidence.observe(103, 800, 103, native_off_after=101)
        result = self.evidence.observe(144, 800, 144, native_off_after=143)
        self.assertEqual((result.status, result.sample_count), ("pending", 1))


if __name__ == "__main__":
    unittest.main()
