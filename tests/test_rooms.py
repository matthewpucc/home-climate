"""Configuration-driven behavior: no predefined names, room count, or device brands."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.ha_package import package, watchdog_signature
from house_climate.policy import Policy
from tests.test_controller import FakeHA, config as legacy_config, fixture, NIGHT

NOW = datetime(2026, 10, 16, 23, tzinfo=timezone.utc)


def configuration():
    return {
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.test_shared", "second": "climate.test_second"},
        "rooms": {
            "alpha": {"name": "North alcove", "head": "shared",
                      "sensors": [{"entity_id": "sensor.test_a"}, {"entity_id": "sensor.test_b"}],
                      "profile": {"target_f": 72, "priority": 1},
                      "fans": [{"entity_id": "fan.test_airflow", "strategy": "transfer", "source_room": "beta", "percentage": 60}]},
            "beta": {"name": "South nook", "head": "shared",
                     "sensors": [{"entity_id": "sensor.test_c"}, {"entity_id": "sensor.test_d"}],
                     "profile": {"comfort_enabled": False}},
            "gamma": {"name": "Quiet space", "head": "second",
                      "sensors": [{"entity_id": "sensor.test_e"}, {"entity_id": "sensor.test_f"}],
                      "profile": {"comfort_enabled": False, "fan_percentage": 0},
                      "schedules": [{"name": "Rest", "start": "22:00", "end": "05:00",
                                     "target_f": 68, "half_band_f": 1, "priority": 8, "fan_percentage": 60}],
                      "fans": [{"entity_id": "fan.test_rest", "strategy": "schedule"}]},
        },
    }


def states(cfg, temperatures=None, now=NOW):
    temperatures = temperatures or {}
    result = {}
    for key, room in cfg.zones.items():
        for p in room.probes:
            result[p.entity_id] = {"state": str(temperatures.get(key, 71)), "last_reported": now.isoformat(),
                                   "attributes": {"unit_of_measurement": "°F"}}
    for entity in cfg.climate_entities:
        result[entity] = {"state": "off", "attributes": {"hvac_modes": ["off", "heat", "cool", "fan_only"],
                           "min_temp": 60, "max_temp": 90, "temperature": 70}}
    for entity in cfg.fans.values():
        result[entity] = {"state": "off", "attributes": {"percentage": 0, "supported_features": 1}}
    return result


def plan(cfg, temperatures=None, now=NOW):
    return Policy(cfg).decide(states(cfg, temperatures, now), now, Settings.read({}, cfg))


class RoomConfigurationTests(unittest.TestCase):
    def test_arbitrary_names_shared_heads_and_priorities(self):
        cfg = Config.from_dict(configuration())
        decision = plan(cfg, {"alpha": 73, "beta": 71, "gamma": 66.9})
        self.assertEqual(decision.mode, "heat")
        self.assertEqual(set(decision.head_targets_f), {"second"})
        self.assertEqual(decision.fans["fan.test_airflow"], 60)
        renamed = replace(cfg, zones={k: replace(v, name="New label") for k, v in cfg.zones.items()})
        self.assertEqual(decision.head_targets_f, plan(renamed, {"alpha": 73, "beta": 71, "gamma": 66.9}).head_targets_f)

    def test_room_count_never_outvotes_an_actual_limit(self):
        raw = configuration()
        for i in range(12):
            key = f"extra_{i}"
            raw["heads"][key] = f"climate.{key}"
            raw["rooms"][key] = {"name": key, "head": key, "sensors": [{"entity_id": f"sensor.{key}"}],
                                 "profile": {"target_f": 68, "priority": 10}}
        cfg = Config.from_dict(raw)
        temps = {k: 69.5 for k in cfg.zones}
        temps["alpha"], temps["beta"] = 65, 71
        self.assertEqual(plan(cfg, temps).mode, "heat")

    def test_minimal_config_without_auxiliary_or_fans_executes(self):
        raw = configuration()
        raw["rooms"] = {"alpha": raw["rooms"]["alpha"]}
        raw["rooms"]["alpha"]["fans"] = []
        raw["heads"].pop("second")
        cfg = Config.from_dict(raw)
        ha = FakeHA(states(cfg, {"alpha": 73}))
        executor = Executor(cfg, ha, "°F")
        decision = plan(cfg, {"alpha": 73})
        for now in (0, 15, 195, 210):
            executor.step(decision, ha.states(), now)
        self.assertEqual(ha.data["climate.test_shared"]["state"], "cool")
        self.assertEqual(executor.shutdown(), [])
        self.assertNotIn("automation", package(cfg))

    def test_missing_shared_room_probe_blocks_the_head(self):
        cfg = Config.from_dict(configuration())
        data = states(cfg, {"alpha": 78, "gamma": 68})
        for p in cfg.zones["beta"].probes:
            data.pop(p.entity_id)
        decision = Policy(cfg).decide(data, NOW, Settings.read({}, cfg))
        self.assertNotIn("shared", decision.head_targets_f)

    def test_daily_schedule_boundaries_and_base_profile(self):
        cfg = Config.from_dict(configuration())
        for hour, percentage in ((21, 0), (22, 60), (4, 60), (5, 0)):
            now = NOW.replace(hour=hour)
            self.assertEqual(plan(cfg, now=now).fans["fan.test_rest"], percentage)

    def test_overlapping_and_empty_schedules_rejected(self):
        for period in ({"start": "04:00", "end": "06:00"}, {"start": "12:00", "end": "12:00"}):
            raw = configuration()
            raw["rooms"]["gamma"]["schedules"].append(period)
            with self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_duplicate_fans_and_dangling_airflow_rejected(self):
        raw = configuration()
        raw["rooms"]["gamma"]["fans"] = raw["rooms"]["alpha"]["fans"]
        with self.assertRaises(ValueError):
            Config.from_dict(raw)
        raw = configuration()
        del raw["rooms"]["beta"]
        with self.assertRaises(ValueError):
            Config.from_dict(raw)

    def test_configured_system_limits_drive_protection(self):
        raw = configuration()
        raw.update(low_f=64, high_f=77)
        cfg = Config.from_dict(raw)
        # Limits-only area follows configured bounds, not the former 66–75 constants.
        self.assertEqual(Policy(cfg).band("beta", Settings.read({}, cfg), NOW)[:2], (64, 77))

    def test_auxiliary_can_belong_to_any_room(self):
        raw = configuration()
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha", heater_cutoff_f=70)
        raw["rooms"]["alpha"]["profile"] = {"target_f": 68, "half_band_f": .5}
        cfg = Config.from_dict(raw)
        self.assertTrue(plan(cfg, {"alpha": 67, "gamma": 68}).heater_on)
        data = states(cfg, {"alpha": 67, "gamma": 68})
        data["sensor.test_a"]["state"] = "71"
        self.assertFalse(Policy(cfg).decide(data, NOW, Settings.read({}, cfg)).heater_on)
        generated = package(cfg)
        self.assertIn("sensor.test_a", generated["template"][0]["binary_sensor"][0]["state"])
        self.assertNotIn("sensor.test_e", generated["template"][0]["binary_sensor"][0]["state"])
        self.assertNotEqual(watchdog_signature(cfg), watchdog_signature(replace(cfg, heater_cutoff_f=69.5)))

    def test_roundtrip_and_legacy_helper_snapshot_preserve_behavior(self):
        old = legacy_config()
        helper_states = {"input_number.hp_office_target": {"state": "73"},
                         "input_number.hp_primary_night_fan_percentage": {"state": "70"}}
        settings = Settings.read(helper_states, old)
        new = Config.from_dict(old.to_dict(settings))
        self.assertFalse(new.legacy)
        self.assertEqual(new.to_dict(), Config.from_dict(new.to_dict()).to_dict())
        for offset in (0, 7, 12):
            now = NIGHT + timedelta(hours=offset)
            data = fixture(old, now, office=74, living=67)
            a = Policy(old).decide(data, now, settings)
            b = Policy(new).decide(data, now, Settings.read({}, new))
            self.assertEqual((a.mode, a.head_targets_f, a.heater_on, a.fans),
                             (b.mode, b.head_targets_f, b.heater_on, b.fans))

    def test_invalid_auxiliary_and_live_configuration_rejected(self):
        raw = configuration()
        raw["heater_entity"] = "climate.test_auxiliary"
        with self.assertRaises(ValueError):
            Config.from_dict(raw)
        cfg = Config.from_dict(configuration())
        with self.assertRaises(ValueError):
            cfg.validate(live=True)
        replace(cfg, equipment_timing_confirmed=True).validate(live=True)
