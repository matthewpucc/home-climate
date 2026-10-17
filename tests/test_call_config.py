"""Heat-pump call offsets are bounded temperature differences, not room targets."""
from pathlib import Path
import tomllib
import unittest

from house_climate.config import Config
from tests.test_rooms import configuration

ROOT = Path(__file__).resolve().parents[1]
OFFSETS = ("head_heat_call_offset_f", "head_cool_call_offset_f")


class CallOffsetConfigurationTests(unittest.TestCase):
    def test_old_stored_configuration_receives_five_degree_defaults(self):
        raw = configuration()
        self.assertTrue(all(name not in raw for name in OFFSETS))
        cfg = Config.from_dict(raw)
        self.assertEqual(tuple(getattr(cfg, name) for name in OFFSETS), (5, 5))
        self.assertEqual(cfg.zones["alpha"].profile.target_f, 72)
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))

    def test_independent_direction_offsets_round_trip_in_fahrenheit(self):
        raw = configuration()
        raw.update(head_heat_call_offset_f=7.5, head_cool_call_offset_f=3)
        cfg = Config.from_dict(raw)
        saved = cfg.to_dict()
        self.assertEqual(tuple(saved[name] for name in OFFSETS), (7.5, 3))
        restored = Config.from_dict(saved)
        self.assertEqual(restored.to_dict(), saved)
        self.assertEqual(restored.zones["alpha"].profile, cfg.zones["alpha"].profile)

    def test_zero_disables_either_direction_and_maximum_is_allowed(self):
        for heat, cool in ((0, 0), (0, 20), (20, 0), (20, 20)):
            raw = configuration()
            raw.update(head_heat_call_offset_f=heat, head_cool_call_offset_f=cool)
            with self.subTest(heat=heat, cool=cool):
                cfg = Config.from_dict(raw)
                self.assertEqual(tuple(getattr(cfg, name) for name in OFFSETS), (heat, cool))

    def test_invalid_offsets_are_rejected(self):
        for name in OFFSETS:
            for value in (-.1, 20.1, float("nan"), float("inf"), -float("inf"),
                          True, False, "5", None):
                raw = configuration()
                raw[name] = value
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    Config.from_dict(raw)

    def test_legacy_import_preserves_explicit_offset_values(self):
        with (ROOT / "tests/fixtures/legacy.toml").open("rb") as stream:
            raw = tomllib.load(stream)
        raw.update(head_heat_call_offset_f=8, head_cool_call_offset_f=0)
        cfg = Config.from_dict(raw)
        self.assertTrue(cfg.legacy)
        self.assertEqual(tuple(getattr(cfg, name) for name in OFFSETS), (8, 0))
        saved = cfg.to_dict()
        self.assertEqual(tuple(saved[name] for name in OFFSETS), (8, 0))
        self.assertFalse(Config.from_dict(saved).legacy)

    def test_legacy_file_without_offsets_uses_new_defaults(self):
        cfg = Config.load(ROOT / "tests/fixtures/legacy.toml")
        self.assertEqual(tuple(getattr(cfg, name) for name in OFFSETS), (5, 5))

    def test_example_enables_offsets_without_changing_room_desired_temperatures(self):
        cfg = Config.load(ROOT / "config.example.toml")
        self.assertEqual(tuple(getattr(cfg, name) for name in OFFSETS), (5, 5))
        self.assertEqual(cfg.zones["room_a"].profile.target_f, 72)
