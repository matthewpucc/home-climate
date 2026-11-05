"""Explicit room-source wiring and truthful observation provenance."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from house_climate.bindings import BindingsError, HouseBindings, example_bindings
from house_climate.config import Config, Probe
from house_climate.house_policy import house_configuration
from house_climate.sensors import Sensors, probe_evidence, probe_value

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def sample(value=70, now=NOW, **attrs):
    return {"state": str(value), "last_reported": now.isoformat(),
            "attributes": {"unit_of_measurement": "°F", **attrs}}


def configuration(probes):
    return Config.from_dict({"schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.shared"}, "rooms": {"room": {
            "head": "shared", "sensors": probes, "profile": {"target_f": 70}}}})


class SourceEvidenceTests(unittest.TestCase):
    def test_packet_receipt_cannot_renew_malformed_future_or_mismatched_evidence(self):
        probe = Probe("sensor.a", "desk")
        state = sample(68, NOW - timedelta(hours=1))
        for receipt in ({}, {"received_at": NOW.timestamp()},
                        {"received_at": NOW.timestamp(), "temperature_c": True},
                        {"received_at": NOW.timestamp(), "temperature_c": float("nan")},
                        {"received_at": NOW.timestamp() + 1, "temperature_c": 20},
                        {"received_at": NOW.timestamp() - 901, "temperature_c": 20},
                        {"received_at": NOW.timestamp(), "temperature_c": 25}):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                probe_evidence(probe, {"sensor.a": {**state, "bluetooth_temperature_receipt": receipt}}, NOW, 900)
        evidence = probe_evidence(probe, {"sensor.a": {**state, "bluetooth_temperature_receipt": {
            "received_at": NOW.timestamp(), "temperature_c": 20}}}, NOW, 900)
        self.assertEqual(evidence["reported_at"], (NOW - timedelta(hours=1)).timestamp())
        self.assertIsNone(evidence["observed_at"])
        self.assertIsNone(evidence["measurement_at"])

    def test_external_head_probe_is_explicit_and_roundtrips_without_offset_guess(self):
        raw = example_bindings().to_dict()
        raw["rooms"]["office"].append({"entity_id": raw["heads"]["bonus"],
            "source": " office_desk_probe ", "attribute": "current_temperature"})
        bindings = HouseBindings.from_dict(raw)
        probe = bindings.room_probes("office")[-1]
        self.assertEqual((probe.source, probe.offset_f), ("office_desk_probe", 0))
        cfg = house_configuration(bindings=bindings)
        self.assertIn(probe, cfg.zones["office"].probes)
        self.assertEqual(HouseBindings.from_dict(bindings.to_dict()), bindings)
        self.assertNotIn(probe, cfg.zones["entertainment"].probes)
        self.assertTrue(all(p.entity_id != bindings.heads["living"] for p in cfg.zones["living"].probes))

    def test_physical_source_cannot_be_assigned_to_two_rooms_via_alias(self):
        raw = example_bindings().to_dict()
        raw["rooms"]["office"] = [{"entity_id": "sensor.a", "source": "desk"}]
        raw["rooms"]["entertainment"] = [{"entity_id": "sensor.b", "source": " desk "}]
        with self.assertRaisesRegex(BindingsError, "multiple rooms"):
            HouseBindings.from_dict(raw)

    def test_room_aliases_select_newest_qualified_value_once(self):
        cfg = configuration([{"entity_id": "sensor.a", "source": "desk"},
                             {"entity_id": "sensor.b", "source": "desk"}])
        readings, _ = Sensors(cfg).read({"sensor.a": sample(68, NOW - timedelta(seconds=30)),
                                       "sensor.b": sample(70)}, NOW)
        self.assertEqual((readings["room"].temperature, readings["room"].sources), (70, 1))

    def test_metadata_only_report_does_not_create_new_temperature_observation(self):
        cfg = configuration([{"entity_id": "sensor.a", "source": "desk"}])
        reader = Sensors(cfg)
        first = reader.read({"sensor.a": sample()}, NOW)[0]["room"]
        later = NOW + timedelta(minutes=5)
        repeated = reader.read({"sensor.a": sample(now=later)}, later)[0]["room"]
        self.assertEqual(repeated.observation_id, first.observation_id)
        self.assertEqual(repeated.observed_at, first.observed_at)
        changed = reader.read({"sensor.a": sample(70.5, later)}, later)[0]["room"]
        self.assertNotEqual(changed.observation_id, first.observation_id)

    def test_physical_timestamp_can_qualify_unchanged_temperature(self):
        cfg = configuration([{"entity_id": "sensor.a", "source": "desk",
                              "observation_time_entity": "sensor.acquired"}])
        reader = Sensors(cfg)
        data = {"sensor.a": sample(), "sensor.acquired": sample(NOW.isoformat())}
        first = reader.read(data, NOW)[0]["room"]
        later = NOW + timedelta(minutes=5)
        data = {"sensor.a": sample(now=later), "sensor.acquired": sample(later.isoformat(), later)}
        second = reader.read(data, later)[0]["room"]
        self.assertNotEqual(first.observation_id, second.observation_id)
        self.assertEqual(second.source_evidence[0]["timestamp_kind"], "device_observation")

    def test_cached_change_only_source_does_not_claim_qualified_progress(self):
        cfg = configuration([{"entity_id": "sensor.a", "source": "desk", "report_on_change": True}])
        readings, warnings = Sensors(cfg).read({"sensor.a": sample(now=NOW - timedelta(hours=2))}, NOW)
        self.assertEqual(readings["room"].temperature, 70)
        self.assertIsNone(readings["room"].observed_at)
        self.assertEqual(readings["room"].observation_id, ())
        self.assertTrue(any("freshness unverified" in warning for warning in warnings))

    def test_new_ha_write_cannot_refresh_expired_physical_acquisition(self):
        probe = Probe("sensor.a", "desk", observation_time_entity="sensor.acquired")
        data = {"sensor.a": sample(), "sensor.acquired": sample((NOW - timedelta(hours=1)).isoformat())}
        with self.assertRaisesRegex(ValueError, "physical observation"):
            probe_value(probe, data, NOW, 900)

    def test_restored_assumed_boolean_values_are_not_valid_temperatures(self):
        probe = Probe("sensor.a", "desk")
        for state in (sample(restored=True), sample(assumed_state=True),
                      {**sample(), "state": True}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                probe_value(probe, {"sensor.a": state}, NOW, 900)

    def test_explicit_fan_condition_rejects_zero_speed_without_banning_other_probes(self):
        probe = Probe("sensor.a", "purifier", require_active_entity="fan.circulation")
        data = {"sensor.a": sample(), "fan.circulation": sample("on", percentage=0)}
        with self.assertRaisesRegex(ValueError, "circulation"):
            probe_value(probe, data, NOW, 900)
        data["fan.circulation"]["attributes"]["percentage"] = 40
        self.assertEqual(probe_value(probe, data, NOW, 900), 70)
        self.assertEqual(probe_value(replace(probe, require_active_entity=None),
                                     {"sensor.a": sample()}, NOW, 900), 70)

    def test_climate_room_temperature_uses_explicit_attribute_and_native_units(self):
        probe = Probe("climate.head", "external_desk", attribute="current_temperature")
        data = {"climate.head": sample("heat", current_temperature=20)}
        data["climate.head"]["attributes"].pop("unit_of_measurement")
        evidence = probe_evidence(probe, data, NOW, 900, "°C")
        self.assertEqual(evidence["temperature_f"], 68)
        self.assertIsNone(evidence["measurement_at"])
        self.assertEqual(evidence["timestamp_kind"], "ha_report")
