"""Opt-in age handling for probes that report only when temperature changes."""
import hashlib
import json
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

from house_climate.config import Config, Probe
from house_climate.ha_package import package, watchdog_signature
from house_climate.sensors import Sensors, probe_value
from tests.test_controller import NIGHT, config as legacy_configuration, fixture
from tests import test_watchdog as watchdog_tests


def configuration(report_on_change=False):
    return Config.from_dict({
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.synthetic_head"},
        "heater_entity": "climate.synthetic_heater", "auxiliary_room": "zone",
        "rooms": {"zone": {"head": "shared", "name": "Synthetic room",
            "sensors": [{"entity_id": "sensor.synthetic_temperature", "source": "physical_sensor",
                         "report_on_change": report_on_change}],
            "profile": {"target_f": 68}}},
    })


def sample(value="68", stamp=None):
    return {"sensor.synthetic_temperature": {
        "state": value, "last_reported": stamp or (NIGHT - timedelta(hours=3)).isoformat(),
        "attributes": {"unit_of_measurement": "°F"}}}


def old_signature(cfg):
    probes = cfg.zones[cfg.auxiliary_room].probes
    mapping = {"watchdog_version": 2 if cfg.legacy else 3,
               "probes": [{key: value for key, value in asdict(probe).items()
                           if key != "report_on_change"} for probe in probes],
               "stale": cfg.sensor_stale_seconds, "cutoff": cfg.heater_cutoff_f,
               "heater": cfg.heater_entity}
    if not cfg.legacy:
        mapping["prefix"] = cfg.helper_prefix
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:16]


class ProbeReportingTests(unittest.TestCase):
    def test_three_hour_unchanged_value_requires_explicit_opt_in(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor")
        self.assertFalse(probe.report_on_change)
        with self.assertRaisesRegex(ValueError, "stale"):
            probe_value(probe, sample(), NIGHT, 900)
        self.assertEqual(probe_value(replace(probe, report_on_change=True), sample(), NIGHT, 900), 68)

    def test_opt_in_does_not_accept_unavailable_nonfinite_or_implausible_values(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor", report_on_change=True)
        for value in (None, "unavailable", "unknown", "nan", "inf", "141", "-41"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    probe_value(probe, sample(value), NIGHT, 900)
        with self.assertRaises(ValueError):
            probe_value(probe, {}, NIGHT, 900)

    def test_opt_in_still_rejects_missing_naive_and_future_timestamps(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor", report_on_change=True)
        for stamp in (None, "invalid", "2026-10-16T05:30:00",
                      (NIGHT + timedelta(seconds=61)).isoformat()):
            with self.subTest(stamp=stamp):
                data = sample()
                data[probe.entity_id]["last_reported"] = stamp
                with self.assertRaises(ValueError):
                    probe_value(probe, data, NIGHT, 900)

    def test_report_policy_is_strictly_boolean_in_configuration(self):
        for value in (0, 1, "false", "true", None, []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "report_on_change must be a boolean"):
                    configuration(value)
        self.assertFalse(configuration().zones["zone"].probes[0].report_on_change)
        self.assertTrue(configuration(True).zones["zone"].probes[0].report_on_change)

    def test_legacy_migration_and_configuration_roundtrip_preserve_report_policy(self):
        legacy = legacy_configuration()
        self.assertTrue(all(not probe.report_on_change
                            for room in legacy.zones.values() for probe in room.probes))
        room = legacy.zones["entertainment"]
        updated = replace(legacy, zones={**legacy.zones, "entertainment": replace(
            room, probes=tuple(replace(probe, report_on_change=True) for probe in room.probes))})
        migrated = Config.from_dict(updated.to_dict())
        self.assertFalse(migrated.legacy)
        self.assertTrue(migrated.zones["entertainment"].probes[0].report_on_change)
        self.assertEqual(migrated.to_dict(), Config.from_dict(migrated.to_dict()).to_dict())

    def test_aliases_remain_one_physical_temperature_source(self):
        cfg = configuration(True)
        room = cfg.zones["zone"]
        alias = replace(room.probes[0], entity_id="sensor.synthetic_alias")
        cfg = replace(cfg, zones={"zone": replace(room, probes=(*room.probes, alias))})
        data = sample()
        data[alias.entity_id] = deepcopy(data[room.probes[0].entity_id])
        readings, warnings = Sensors(cfg).read(data, NIGHT)
        self.assertEqual(readings["zone"].sources, 1)
        self.assertTrue(any("one independent" in warning for warning in warnings))

    def test_default_false_preserves_existing_generic_and_legacy_watchdog_signatures(self):
        for cfg in (configuration(), legacy_configuration()):
            with self.subTest(legacy=cfg.legacy):
                self.assertEqual(watchdog_signature(cfg), old_signature(cfg))
                name = cfg.auxiliary_room
                room = cfg.zones[name]
                changed = replace(cfg, zones={**cfg.zones, name: replace(room, probes=tuple(
                    replace(probe, report_on_change=True) for probe in room.probes))})
                self.assertNotEqual(watchdog_signature(changed), watchdog_signature(cfg))
                generated = package(changed)["template"][0]["binary_sensor"][0]
                self.assertEqual(generated["attributes"]["configuration_signature"], watchdog_signature(changed))


@unittest.skipIf(watchdog_tests.Environment is None, "Install Jinja2 to render HA templates")
class ProbeReportingWatchdogTests(unittest.TestCase):
    def render(self, cfg, data):
        self.data = data
        env = watchdog_tests.WatchdogTemplateTests.environment(self)
        template = package(cfg)["template"][0]["binary_sensor"][0]["state"]
        return env.from_string(template).render().strip() == "True"

    def python_safe(self, cfg, data):
        values = []
        for probe in cfg.zones[cfg.auxiliary_room].probes:
            try:
                values.append(probe_value(probe, data, NIGHT, cfg.sensor_stale_seconds))
            except (ValueError, TypeError, OverflowError):
                pass
        return bool(values) and max(values) < cfg.heater_cutoff_f

    def test_generated_watchdog_matches_python_age_policy_and_independent_hot_cutoff(self):
        for flag in (False, True):
            cfg = configuration(flag)
            for value in ("68", "70", "unavailable", "unknown", "nan", "inf", "141"):
                with self.subTest(flag=flag, value=value):
                    data = sample(value)
                    self.assertEqual(self.render(cfg, data), self.python_safe(cfg, data))
                    self.assertEqual(self.render(cfg, data), flag and value == "68")

    def test_generated_watchdog_rejects_missing_naive_and_future_reports_with_opt_in(self):
        cfg = configuration(True)
        for stamp in (None, "invalid", "2026-10-16T05:30:00",
                      (NIGHT + timedelta(seconds=61)).isoformat()):
            with self.subTest(stamp=stamp):
                data = sample()
                data["sensor.synthetic_temperature"]["last_reported"] = stamp
                self.assertFalse(self.render(cfg, data))
                self.assertFalse(self.python_safe(cfg, data))

    def test_real_ha_aware_datetime_report_is_accepted_and_naive_datetime_is_rejected(self):
        cfg = configuration(True)
        for stamp, expected in ((NIGHT - timedelta(hours=3), True),
                                ((NIGHT - timedelta(hours=3)).replace(tzinfo=None), False)):
            data = sample()
            data["sensor.synthetic_temperature"]["last_reported"] = stamp
            self.assertEqual(self.render(cfg, data), expected)

    def test_legacy_watchdog_honors_opt_in_without_weakening_hot_cutoff(self):
        cfg = legacy_configuration()
        room = cfg.zones[cfg.auxiliary_room]
        cfg = replace(cfg, zones={**cfg.zones, cfg.auxiliary_room: replace(room, probes=tuple(
            replace(probe, report_on_change=True) for probe in room.probes))})
        data = fixture(cfg)
        for probe in room.probes:
            data[probe.entity_id]["last_updated"] = (NIGHT - timedelta(hours=3)).isoformat()
        self.assertTrue(self.render(cfg, data))
        data[room.probes[0].entity_id]["state"] = "70"
        self.assertFalse(self.render(cfg, data))
