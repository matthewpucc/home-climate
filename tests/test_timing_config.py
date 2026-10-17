"""Stable comfort cadence is distinct from safety and recovery polling."""
from dataclasses import replace
from pathlib import Path
import tomllib
import unittest

from house_climate.config import Config
from tests.test_rooms import configuration

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = ("decision_interval_seconds", "head_minimum_run_seconds", "head_minimum_off_seconds")


class TimingConfigurationTests(unittest.TestCase):
    def test_existing_stored_configs_receive_stable_defaults(self):
        raw = configuration()
        self.assertTrue(all(name not in raw for name in TIMINGS))
        cfg = Config.from_dict(raw)
        self.assertEqual(cfg.poll_seconds, 15)
        self.assertEqual(cfg.decision_interval_seconds, 90)
        self.assertEqual(cfg.head_minimum_run_seconds, 300)
        self.assertEqual(cfg.head_minimum_off_seconds, 180)
        self.assertEqual(cfg.off_settle_seconds, 180)

    def test_timing_settings_round_trip_with_explicit_seconds(self):
        raw = configuration()
        raw.update(decision_interval_seconds=120, head_minimum_run_seconds=600,
                   head_minimum_off_seconds=240, poll_seconds=10)
        cfg = Config.from_dict(raw)
        saved = cfg.to_dict()
        self.assertEqual({name: saved[name] for name in TIMINGS},
                         {"decision_interval_seconds": 120, "head_minimum_run_seconds": 600,
                          "head_minimum_off_seconds": 240})
        restored = Config.from_dict(saved)
        self.assertEqual(restored.to_dict(), saved)
        self.assertEqual(restored.poll_seconds, 10)

    def test_legacy_import_preserves_explicit_new_timing_settings(self):
        with (ROOT / "tests/fixtures/legacy.toml").open("rb") as stream:
            raw = tomllib.load(stream)
        raw.update(decision_interval_seconds=60, head_minimum_run_seconds=420,
                   head_minimum_off_seconds=210)
        cfg = Config.from_dict(raw)
        self.assertTrue(cfg.legacy)
        self.assertEqual(tuple(getattr(cfg, name) for name in TIMINGS), (60, 420, 210))
        saved = cfg.to_dict()
        self.assertEqual(tuple(saved[name] for name in TIMINGS), (60, 420, 210))
        self.assertFalse(Config.from_dict(saved).legacy)

    def test_legacy_configs_without_timing_fields_receive_new_defaults(self):
        cfg = Config.load(ROOT / "tests/fixtures/legacy.toml")
        self.assertEqual(tuple(getattr(cfg, name) for name in TIMINGS), (90, 300, 180))

    def test_advanced_short_cadence_and_zero_head_timers_are_allowed(self):
        raw = configuration()
        raw.update(decision_interval_seconds=.03, head_minimum_run_seconds=0,
                   head_minimum_off_seconds=0)
        cfg = Config.from_dict(raw)
        self.assertEqual(tuple(getattr(cfg, name) for name in TIMINGS), (.03, 0, 0))

    def test_invalid_cadence_values_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, "90", None):
            raw = configuration()
            raw["decision_interval_seconds"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "decision_interval_seconds"):
                Config.from_dict(raw)

    def test_invalid_per_head_timers_are_rejected(self):
        for name in TIMINGS[1:]:
            for value in (-1, float("nan"), float("inf"), -float("inf"), True, "300", None):
                raw = configuration()
                raw[name] = value
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    Config.from_dict(raw)

    def test_long_comfort_interval_does_not_relax_live_watchdog_polling(self):
        cfg = replace(Config.from_dict(configuration()), equipment_timing_confirmed=True,
                      decision_interval_seconds=3600, poll_seconds=15)
        cfg.validate(live=True)
        with self.assertRaisesRegex(ValueError, "watchdog"):
            replace(cfg, poll_seconds=60).validate(live=True)

    def test_example_documents_independent_timing_defaults(self):
        cfg = Config.load(ROOT / "config.example.toml")
        self.assertEqual(tuple(getattr(cfg, name) for name in TIMINGS), (90, 300, 180))
        self.assertEqual(cfg.poll_seconds, 15)
