"""Outdoor evidence wiring survives private and generic configuration paths."""
from dataclasses import replace
import unittest

from house_climate.bindings import BindingsError, HouseBindings, example_bindings
from house_climate.config import Config, OutdoorProbe, Probe
from house_climate.house_policy import house_configuration
from tests.test_rooms import configuration


class OutdoorWiringTests(unittest.TestCase):
    def descriptors(self):
        return [
            {"entity_id": "sensor.fixture_local_outdoor", "source": "local", "role": "primary"},
            {"entity_id": "weather.fixture_station", "source": "station",
             "observation_time_entity": "sensor.fixture_observation_time"},
        ]

    def test_native_house_policy_keeps_priority_and_observation_binding(self):
        raw = example_bindings().to_dict()
        raw["outdoor"] = self.descriptors()
        bindings = HouseBindings.from_dict(raw)
        self.assertEqual(bindings.to_dict(), raw)
        cfg = house_configuration(bindings=bindings)
        self.assertEqual(cfg.to_dict()["outdoor_probes"], raw["outdoor"])
        self.assertEqual(cfg.outdoor_probes[0].role, "primary")
        self.assertEqual(cfg.outdoor_probes[1].observation_time_entity,
                         "sensor.fixture_observation_time")
        self.assertEqual(Config.from_dict(cfg.to_dict()).to_dict(), cfg.to_dict())

    def test_generic_configuration_preserves_new_wiring(self):
        raw = configuration()
        raw["outdoor_probes"] = self.descriptors()
        cfg = Config.from_dict(raw)
        self.assertTrue(all(isinstance(probe, OutdoorProbe) for probe in cfg.outdoor_probes))
        self.assertEqual(cfg.to_dict()["outdoor_probes"], self.descriptors())
        self.assertEqual(Config.from_dict(cfg.to_dict()).outdoor_probes, cfg.outdoor_probes)

    def test_existing_probe_and_untagged_bindings_do_not_gain_required_keys(self):
        cfg = replace(Config.from_dict(configuration()), outdoor_probes=(
            Probe("sensor.fixture_outdoor", " station "),))
        self.assertEqual(cfg.to_dict()["outdoor_probes"], [
            {"entity_id": "sensor.fixture_outdoor", "source": "station"}])
        self.assertEqual(cfg.outdoor_probes[0].role, "fallback")
        self.assertIsNone(cfg.outdoor_probes[0].observation_time_entity)
        original = example_bindings().to_dict()
        self.assertEqual(HouseBindings.from_dict(original).to_dict(), original)

    def test_invalid_roles_timestamp_bindings_and_hidden_rules_are_rejected(self):
        for extra in ({"role": "preferred"}, {"role": True}, {"role": []},
                      {"observation_time_entity": "weather.fixture_station"},
                      {"observation_time_entity": "sensor.bad.id"},
                      {"observation_time_entity": False}, {"stale_seconds": 99999}):
            with self.subTest(extra=extra):
                raw = example_bindings().to_dict()
                raw["outdoor"] = [{"entity_id": "sensor.fixture_outdoor", "source": "station", **extra}]
                with self.assertRaises(BindingsError):
                    HouseBindings.from_dict(raw)

    def test_one_physical_source_cannot_have_conflicting_priority_roles(self):
        raw = configuration()
        raw["outdoor_probes"] = [
            {"entity_id": "sensor.fixture_one", "source": "station", "role": "primary"},
            {"entity_id": "sensor.fixture_alias", "source": " station "},
        ]
        with self.assertRaisesRegex(ValueError, "same role"):
            Config.from_dict(raw)

    def test_outdoor_normalization_does_not_relax_indoor_probe_freshness(self):
        with self.assertRaisesRegex(ValueError, "freshness"):
            replace(Config.from_dict(configuration()), outdoor_probes=(
                Probe("sensor.fixture_outdoor", "station", report_on_change=True),))
        cfg = Config.from_dict(configuration())
        self.assertEqual(cfg.sensor_stale_seconds, 900)
        self.assertTrue(all(type(probe) is Probe for room in cfg.zones.values() for probe in room.probes))


if __name__ == "__main__":
    unittest.main()
