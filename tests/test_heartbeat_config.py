"""Native heartbeat settings preserve the standalone watchdog contract."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tomllib
import unittest

from house_climate.config import Config, HeartbeatTimeoutError
from house_climate.ha_package import package, watchdog_signature
from tests.test_rooms import configuration

ROOT = Path(__file__).resolve().parents[1]


class HeartbeatConfigurationTests(unittest.TestCase):
    def test_existing_configs_receive_defaults_without_mutating_their_input(self):
        raw = configuration()
        original = deepcopy(raw)
        cfg = Config.from_dict(raw)
        self.assertEqual(cfg.heartbeat_timeout_seconds, 300)
        self.assertEqual(cfg.auxiliary_heartbeat_timeout_seconds, 120)
        self.assertEqual(raw, original)
        self.assertNotIn("heartbeat_timeout_seconds", raw)
        self.assertNotIn("auxiliary_heartbeat_timeout_seconds", raw)

    def test_explicit_seconds_survive_round_trip_and_legacy_import(self):
        for raw in (configuration(), tomllib.loads(
                (ROOT / "tests/fixtures/legacy.toml").read_text())):
            raw.update(heartbeat_timeout_seconds=480, auxiliary_heartbeat_timeout_seconds=90)
            cfg = Config.from_dict(raw)
            saved = cfg.to_dict()
            restored = Config.from_dict(saved)
            self.assertEqual(restored.heartbeat_timeout_seconds, 480)
            self.assertEqual(restored.auxiliary_heartbeat_timeout_seconds, 90)
            self.assertEqual(restored.to_dict(), saved)

    def test_valid_boundary_values_include_equal_watchdog_timeouts(self):
        for main, auxiliary in ((60, 30), (60, 60), (300, 120), (900, 900)):
            with self.subTest(main=main, auxiliary=auxiliary):
                raw = configuration()
                raw.update(heartbeat_timeout_seconds=main,
                           auxiliary_heartbeat_timeout_seconds=auxiliary)
                cfg = Config.from_dict(raw)
                self.assertEqual((cfg.heartbeat_timeout_seconds,
                                  cfg.auxiliary_heartbeat_timeout_seconds), (main, auxiliary))

    def test_main_timeout_rejects_invalid_types_nonfinite_and_unbounded_values(self):
        for value in (0, 59, 901, True, False, "300", None, float("nan"),
                      float("inf"), -float("inf")):
            with self.subTest(value=value):
                raw = configuration()
                raw["heartbeat_timeout_seconds"] = value
                with self.assertRaises(HeartbeatTimeoutError) as caught:
                    Config.from_dict(raw)
                self.assertEqual(caught.exception.field, "heartbeat_timeout_seconds")
                self.assertEqual(caught.exception.code, "invalid_heartbeat_timeout")

    def test_auxiliary_timeout_rejects_invalid_types_nonfinite_and_unbounded_values(self):
        for value in (0, 29, 901, True, False, "120", None, float("nan"),
                      float("inf"), -float("inf")):
            with self.subTest(value=value):
                raw = configuration()
                raw["auxiliary_heartbeat_timeout_seconds"] = value
                with self.assertRaises(HeartbeatTimeoutError) as caught:
                    Config.from_dict(raw)
                self.assertEqual(caught.exception.field, "auxiliary_heartbeat_timeout_seconds")
                self.assertEqual(caught.exception.code, "invalid_auxiliary_heartbeat_timeout")

    def test_auxiliary_timeout_cannot_outlast_the_controller(self):
        raw = configuration()
        raw.update(heartbeat_timeout_seconds=60, auxiliary_heartbeat_timeout_seconds=120)
        with self.assertRaises(HeartbeatTimeoutError) as caught:
            Config.from_dict(raw)
        self.assertEqual(caught.exception.field, "auxiliary_heartbeat_timeout_seconds")
        self.assertEqual(caught.exception.code, "auxiliary_heartbeat_exceeds_controller")

    def test_native_watchdog_settings_do_not_change_generated_packages_or_signatures(self):
        legacy = Config.load(ROOT / "tests/fixtures/legacy.toml")
        modern = Config.from_dict(legacy.to_dict())
        for cfg in (legacy, modern):
            adjusted = replace(cfg, heartbeat_timeout_seconds=900,
                               auxiliary_heartbeat_timeout_seconds=30)
            self.assertEqual(package(cfg), package(adjusted))
            self.assertEqual(watchdog_signature(cfg), watchdog_signature(adjusted))
            self.assertEqual(cfg.sensor_stale_seconds, adjusted.sensor_stale_seconds)
            self.assertEqual(cfg.heater_cutoff_f, adjusted.heater_cutoff_f)


if __name__ == "__main__":
    unittest.main()
