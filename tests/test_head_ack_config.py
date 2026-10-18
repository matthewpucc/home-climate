"""Head feedback waits do not relax safety, ownership, or Off timing."""
from dataclasses import replace
from pathlib import Path
import tomllib
import unittest

from house_climate.config import Config
from house_climate.ha_package import watchdog_signature
from tests.test_rooms import configuration

ROOT = Path(__file__).resolve().parents[1]


class HeadAcknowledgementConfigTests(unittest.TestCase):
    def test_existing_configs_default_to_longer_activation_only_wait(self):
        cfg = Config.from_dict(configuration())
        self.assertEqual(cfg.head_activation_timeout_seconds, 120)
        self.assertEqual(cfg.command_timeout_seconds, 45)
        self.assertEqual(cfg.poll_seconds, 15)

    def test_seconds_round_trip_and_legacy_import(self):
        raw = configuration()
        raw["head_activation_timeout_seconds"] = 150
        cfg = Config.from_dict(raw)
        self.assertEqual(Config.from_dict(cfg.to_dict()).head_activation_timeout_seconds, 150)
        with (ROOT / "tests/fixtures/legacy.toml").open("rb") as stream:
            legacy = tomllib.load(stream)
        legacy["head_activation_timeout_seconds"] = 150
        self.assertEqual(Config.from_dict(legacy).head_activation_timeout_seconds, 150)

    def test_invalid_or_unbounded_waits_are_rejected(self):
        for value in (0, -1, 301, True, "120", None, float("nan"), float("inf")):
            with self.subTest(value=value):
                raw = configuration()
                raw["head_activation_timeout_seconds"] = value
                with self.assertRaisesRegex(ValueError, "head_activation_timeout_seconds"):
                    Config.from_dict(raw)

    def test_long_wait_does_not_change_watchdog_contract(self):
        cfg = replace(Config.from_dict(configuration()), equipment_timing_confirmed=True,
                      head_activation_timeout_seconds=300)
        cfg.validate(live=True)
        self.assertEqual(watchdog_signature(cfg),
                         watchdog_signature(replace(cfg, head_activation_timeout_seconds=45)))
        with self.assertRaisesRegex(ValueError, "watchdog"):
            replace(cfg, command_timeout_seconds=61).validate(live=True)

    def test_example_documents_separate_acknowledgement_wait(self):
        cfg = Config.load(ROOT / "config.example.toml")
        self.assertEqual(cfg.head_activation_timeout_seconds, 120)
        self.assertEqual(cfg.command_timeout_seconds, 45)
