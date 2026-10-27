"""Approved draft thresholds are data, not implicit actuator instructions."""
from dataclasses import FrozenInstanceError
from math import inf, nan
import unittest

from house_climate.config import Config
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import configuration


class ProtectionTemperatureTests(unittest.TestCase):
    def test_approved_defaults_are_separate_values(self):
        settings = ProtectionTemperatures()
        self.assertEqual(settings.to_dict(), {
            "normal_heating_floor_f": 60,
            "freezing_heating_floor_f": 65,
            "freezing_outdoor_below_f": 32,
            "emergency_cooling_at_f": 80,
            "release_hysteresis_f": 1,
            "transition_deadband_f": 2,
            "qualification_seconds": 300,
            "boundary_tie_mode": "heat",
            "unknown_outdoor_tie_mode": "heat",
        })

    def test_freezing_boundary_is_strictly_below_32_fahrenheit(self):
        settings = ProtectionTemperatures()
        for outdoor in (-20, 0, 31.999):
            self.assertEqual(settings.candidate_heating_floor(outdoor), 65)
        for outdoor in (32, 32.001, 70):
            self.assertEqual(settings.candidate_heating_floor(outdoor), 60)
        self.assertEqual(settings.emergency_cooling_at_f, 80)

    def test_configured_thresholds_round_trip_without_shared_mutable_data(self):
        raw = {"normal_heating_floor_f": 58, "freezing_heating_floor_f": 66,
               "freezing_outdoor_below_f": 30, "emergency_cooling_at_f": 82}
        settings = ProtectionTemperatures.from_dict(raw)
        raw["normal_heating_floor_f"] = 50
        self.assertEqual(settings.candidate_heating_floor(29.9), 66)
        self.assertEqual(settings.candidate_heating_floor(30), 58)
        self.assertEqual(ProtectionTemperatures.from_dict(settings.to_dict()), settings)
        changed = settings.to_dict()
        changed["normal_heating_floor_f"] = 40
        self.assertEqual(settings.normal_heating_floor_f, 58)
        with self.assertRaises(FrozenInstanceError):
            settings.normal_heating_floor_f = 50

    def test_partial_configuration_preserves_other_approved_defaults(self):
        settings = ProtectionTemperatures.from_dict({"emergency_cooling_at_f": 82})
        self.assertEqual(settings.normal_heating_floor_f, 60)
        self.assertEqual(settings.freezing_heating_floor_f, 65)
        self.assertEqual(settings.freezing_outdoor_below_f, 32)

    def test_exposure_bounds_release_gap_and_regime_settings_are_validated(self):
        for raw in ({"normal_heating_floor_f": 39.9},
                    {"freezing_heating_floor_f": 70.1},
                    {"freezing_outdoor_below_f": -40.1},
                    {"emergency_cooling_at_f": 95.1},
                    {"release_hysteresis_f": 0},
                    {"transition_deadband_f": 10.1},
                    {"qualification_seconds": -1},
                    {"qualification_seconds": 1.5},
                    {"qualification_seconds": 3601},
                    {"normal_heating_floor_f": 70, "freezing_heating_floor_f": 70,
                     "emergency_cooling_at_f": 75, "release_hysteresis_f": 3},
                    {"boundary_tie_mode": "auto"},
                    {"unknown_outdoor_tie_mode": []}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ProtectionTemperatures.from_dict(raw)
        settings = ProtectionTemperatures(qualification_seconds=0, transition_deadband_f=0,
                                          boundary_tie_mode="cool", unknown_outdoor_tie_mode="cool")
        self.assertEqual(settings.qualification_seconds, 0)
        self.assertEqual(settings.boundary_tie_mode, "cool")

    def test_missing_or_untrusted_outdoor_does_not_silently_choose_a_floor(self):
        for outdoor in (None, True, False, "31", nan, inf, -inf, 10 ** 1000, [], {}):
            with self.subTest(outdoor=outdoor):
                with self.assertRaises(ValueError):
                    ProtectionTemperatures().candidate_heating_floor(outdoor)

    def test_invalid_configuration_is_rejected(self):
        for key in ProtectionTemperatures().to_dict():
            for value in (True, False, None, "60", nan, inf, -inf, 10 ** 1000):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        ProtectionTemperatures.from_dict({key: value})
        for value in (None, [], {"room_priority": 1}, {"outdoor_sensor": "sensor.example_weather"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProtectionTemperatures.from_dict(value)

    def test_heating_floors_must_not_invert_or_reach_the_cooling_threshold(self):
        for raw in ({"normal_heating_floor_f": 66},
                    {"freezing_heating_floor_f": 80},
                    {"emergency_cooling_at_f": 65}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    ProtectionTemperatures.from_dict(raw)
        # Equal floors deliberately allow an installation to disable the
        # outdoor-dependent difference without inventing a second threshold.
        self.assertEqual(ProtectionTemperatures(normal_heating_floor_f=65).candidate_heating_floor(70), 65)

    def test_existing_configuration_retains_66_to_75_until_operational_integration(self):
        cfg = Config.from_dict(configuration())
        before = cfg.to_dict()
        ProtectionTemperatures.from_dict({}).candidate_heating_floor(10)
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))
        self.assertEqual(cfg.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
