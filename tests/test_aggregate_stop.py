"""Aggregate AC evidence corroborates native Off without attributing other loads."""
from dataclasses import replace
import math
import unittest

from house_climate.aggregate_stop import AggregateStopConfig, AggregateStopEvidence


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


if __name__ == "__main__":
    unittest.main()
