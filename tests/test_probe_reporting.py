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


def expected_signature(cfg):
    probes = cfg.zones[cfg.auxiliary_room].probes
    mapping = {"watchdog_version": 4,
               "probes": [{key: value for key, value in asdict(probe).items()
                           if key != "report_on_change" and (value is not None or key not in
                               {"observation_time_entity", "require_active_entity"})} for probe in probes],
               "stale": cfg.sensor_stale_seconds, "cutoff": cfg.heater_cutoff_f,
               "heater": cfg.heater_entity,
               "controller_timeout": cfg.heartbeat_timeout_seconds,
               "auxiliary_timeout": cfg.auxiliary_heartbeat_timeout_seconds}
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

    def test_default_false_uses_current_generic_and_legacy_watchdog_contracts(self):
        for cfg in (configuration(), legacy_configuration()):
            with self.subTest(legacy=cfg.legacy):
                self.assertEqual(watchdog_signature(cfg), expected_signature(cfg))
                name = cfg.auxiliary_room
                room = cfg.zones[name]
                changed = replace(cfg, zones={**cfg.zones, name: replace(room, probes=tuple(
                    replace(probe, report_on_change=True) for probe in room.probes))})
                self.assertNotEqual(watchdog_signature(changed), watchdog_signature(cfg))
                generated = package(changed)["template"][0]["binary_sensor"][0]
                self.assertEqual(generated["attributes"]["configuration_signature"], watchdog_signature(changed))


@unittest.skipIf(watchdog_tests.Environment is None, "Install Jinja2 to render HA templates")
class ProbeReportingWatchdogTests(unittest.TestCase):
    def configurations_for(self, probe):
        for cfg in (configuration(), legacy_configuration()):
            room = cfg.zones[cfg.auxiliary_room]
            yield replace(cfg, zones={**cfg.zones, cfg.auxiliary_room: replace(room, probes=(probe,))})

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

    def test_watchdogs_reject_restored_assumed_and_boolean_temperature(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor")
        for cfg in self.configurations_for(probe):
            for flag in ("restored", "assumed_state", "boolean"):
                with self.subTest(legacy=cfg.legacy, flag=flag):
                    data = sample(stamp=NIGHT.isoformat())
                    if flag == "boolean":
                        data[probe.entity_id]["state"] = True
                    else:
                        data[probe.entity_id]["attributes"][flag] = True
                    self.assertFalse(self.python_safe(cfg, data))
                    self.assertFalse(self.render(cfg, data))

    def test_watchdogs_require_both_acquisition_and_report_evidence(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor",
                      observation_time_entity="sensor.synthetic_observation")
        for cfg in self.configurations_for(probe):
            for case in ("valid", "old_measurement", "future_measurement", "missing_measurement",
                         "naive_measurement", "restored_measurement", "assumed_measurement", "old_report"):
                with self.subTest(legacy=cfg.legacy, case=case):
                    data = sample(stamp=NIGHT.isoformat())
                    observed = {"state": NIGHT.isoformat(), "attributes": {}}
                    data[probe.observation_time_entity] = observed
                    if case == "old_measurement":
                        observed["state"] = (NIGHT - timedelta(hours=1)).isoformat()
                    elif case == "future_measurement":
                        observed["state"] = (NIGHT + timedelta(seconds=1)).isoformat()
                    elif case == "missing_measurement":
                        observed["state"] = "unavailable"
                    elif case == "naive_measurement":
                        observed["state"] = NIGHT.replace(tzinfo=None).isoformat()
                    elif case in ("restored_measurement", "assumed_measurement"):
                        observed["attributes"]["restored" if case.startswith("restored") else "assumed_state"] = True
                    elif case == "old_report":
                        data[probe.entity_id]["last_reported"] = (NIGHT - timedelta(hours=1)).isoformat()
                    self.assertEqual(self.python_safe(cfg, data), case == "valid")
                    self.assertEqual(self.render(cfg, data), case == "valid")

    def test_watchdogs_require_active_condition_with_actual_positive_percentage(self):
        probe = Probe("sensor.synthetic_temperature", "physical_sensor",
                      require_active_entity="fan.synthetic_circulation")
        for cfg in self.configurations_for(probe):
            for state, attrs, expected in (
                    ("on", {}, True), ("on", {"percentage": 40}, True),
                    ("off", {"percentage": 40}, False), ("unavailable", {}, False),
                    ("on", {"percentage": 0}, False), ("on", {"percentage": True}, False),
                    ("on", {"percentage": "40"}, False), ("on", {"percentage": float("nan")}, False),
                    ("on", {"restored": True}, False), ("on", {"assumed_state": True}, False)):
                with self.subTest(legacy=cfg.legacy, state=state, attrs=attrs):
                    data = sample(stamp=NIGHT.isoformat())
                    data[probe.require_active_entity] = {"state": state, "attributes": attrs}
                    self.assertEqual(self.python_safe(cfg, data), expected)
                    self.assertEqual(self.render(cfg, data), expected)

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
