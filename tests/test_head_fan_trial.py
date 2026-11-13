from dataclasses import asdict, replace
import unittest

from house_climate.head_fan_trial import FanTrial, MAX_TRIAL_SECONDS


def trial(**changes):
    row = FanTrial(
        trial_id="trial-one", entity_id="climate.test_head", mode="low",
        baseline="auto", starts_at=1800000000, ends_at=1800003600,
        binding_fingerprint="head:test|rooms:study", ownership_generation=12,
    )
    return replace(row, **changes)


class FanTrialTests(unittest.TestCase):
    def test_round_trip_is_exact_and_does_not_resume(self):
        original = trial()
        self.assertEqual(FanTrial.from_dict(asdict(original)), original)
        self.assertEqual(original.after_restart().stage, "cancelled")
        self.assertEqual(original.stage, "enrolled")

    def test_defaults_protect_sleep_and_do_not_authorize_overnight(self):
        self.assertTrue(trial().sleep_sensitive)
        self.assertFalse(trial().overnight_authorized)
        with self.assertRaises(ValueError):
            trial(overnight_authorized=True).validate(1800000000)
        trial(sleep_sensitive=False, overnight_authorized=True).validate(1800000000)

    def test_expiration_is_exact_and_rows_remain_inspectable(self):
        row = trial()
        self.assertFalse(row.expired(row.ends_at - 0.1))
        self.assertTrue(row.expired(row.ends_at))
        self.assertTrue(row.expired(row.ends_at + 3600))
        self.assertEqual(FanTrial.from_dict(asdict(row)), row)

    def test_finite_positive_bounded_authorization_only(self):
        for field in ("starts_at", "ends_at"):
            for value in (True, False, None, "1800000000", float("inf"), float("nan"), -1, 10 ** 1000):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    trial(**{field: value}).validate(1800000000)
        for duration in (0, -1, MAX_TRIAL_SECONDS + 0.1):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                trial(ends_at=1800000000 + duration).validate(1800000000)
        trial(ends_at=1800000000 + MAX_TRIAL_SECONDS).validate(1800000000)

    def test_invalid_clock_never_means_unexpired(self):
        for now in (True, None, float("nan"), float("inf"), "now", 10 ** 1000):
            with self.subTest(now=now), self.assertRaises(ValueError):
                trial().expired(now)

    def test_storage_missing_or_unknown_fields_fail_closed(self):
        for field in asdict(trial()):
            row = asdict(trial())
            del row[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                FanTrial.from_dict(row)
        for row in (None, [], "trial", {**asdict(trial()), "future_authority": True}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                FanTrial.from_dict(row)

    def test_malformed_id_modes_binding_stage_flags_fail_closed(self):
        for changes in (
            {"entity_id": "fan.test"}, {"entity_id": "climate.one,climate.two"},
            {"mode": "auto"}, {"mode": ""}, {"baseline": None},
            {"binding_fingerprint": ""}, {"trial_id": " x "},
            {"reason": "bad\nreason"}, {"stage": "resume"}, {"stage": []},
            {"sleep_sensitive": 0}, {"overnight_authorized": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                FanTrial.from_dict(asdict(trial(**changes)))

    def test_restart_never_replays_or_blindly_restores_interrupted_requests(self):
        for stage in ("request_reserved", "active", "restore_reserved"):
            with self.subTest(stage=stage):
                row = trial(stage=stage).after_restart()
                self.assertEqual(row.stage, "needs_attention")
                self.assertEqual(row.reason, "restart_requires_restoration_review")
                self.assertEqual(row.after_restart(), row)
        for stage in ("complete", "cancelled", "needs_attention"):
            row = trial(stage=stage)
            self.assertEqual(row.after_restart(), row)

    def test_generation_is_bounded_json_without_nonfinite_values(self):
        value = {"lease": [1, "generation", None, True]}
        self.assertEqual(FanTrial.from_dict(asdict(trial(ownership_generation=value))).ownership_generation, value)
        nested = None
        for _ in range(10):
            nested = [nested]
        for value in (float("nan"), {"x": float("inf")}, (1, 2), object(), nested, list(range(33)),
                      10 ** 1000, {"lease": -(10 ** 1000)}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                trial(ownership_generation=value).validate(1800000000)


if __name__ == "__main__":
    unittest.main()
