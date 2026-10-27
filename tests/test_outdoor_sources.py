"""Explicit current outdoor data has units, provenance, and bounded validity."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from math import inf, nan
import unittest

from house_climate.bindings import BindingsError, HouseBindings, example_bindings
from house_climate.config import Config, Probe
from house_climate.house_policy import house_configuration
from house_climate.outdoor import OUTDOOR_MAX_AGE_SECONDS, WEATHER_MAX_AGE_SECONDS, read_outdoor
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import configuration

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def sample(value, unit="°F", *, age=0, attributes=None, state=None):
    return {"state": str(value) if state is None else state,
            "attributes": {"unit_of_measurement": unit, **(attributes or {})},
            "last_reported": (NOW - timedelta(seconds=age)).isoformat()}


def weather(value, *, age=0):
    return sample("clear", age=age, attributes={"temperature": value, "temperature_unit": "°F"})


class OutdoorSourceTests(unittest.TestCase):
    def cfg(self, *probes):
        return replace(Config.from_dict(configuration()), outdoor_probes=probes)

    def test_no_sources_is_unknown_even_without_protection_configuration(self):
        cfg = self.cfg()
        self.assertIsNone(cfg.protection)
        reading = read_outdoor(cfg, {}, NOW)
        self.assertEqual((reading.status, reading.temperature_f, reading.sources), ("unknown", None, 0))
        self.assertIsNone(reading.next_expiry)
        self.assertIn("No outdoor", reading.warnings[0])

    def test_sensor_state_units_are_normalized_without_climate_unit_inference(self):
        cfg = self.cfg(Probe("sensor.exterior_f", "exterior"), Probe("sensor.garden_c", "garden"))
        states = {"sensor.exterior_f": sample(35), "sensor.garden_c": sample(-2, "°C")}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.status, reading.sources), ("known", 2))
        self.assertAlmostEqual(reading.temperature_f, 28.4)
        self.assertEqual(reading.next_expiry, NOW.timestamp() + OUTDOOR_MAX_AGE_SECONDS)
        self.assertEqual(dict(reading.source_timestamps), {"exterior": NOW.timestamp(), "garden": NOW.timestamp()})

    def test_weather_reads_only_current_temperature_and_current_units(self):
        cfg = self.cfg(Probe("weather.local_current", "provider"))
        state = sample("cloudy", state="cloudy", attributes={
            "temperature": 0, "temperature_unit": "°C", "forecast": [{"temperature": -50}],
        })
        # The generic unit field is deliberately wrong for this weather value.
        state["attributes"]["unit_of_measurement"] = "°F"
        reading = read_outdoor(cfg, {"weather.local_current": state}, NOW)
        self.assertEqual(reading.temperature_f, 32)
        state["attributes"].pop("temperature")
        missing_current = read_outdoor(cfg, {"weather.local_current": state}, NOW)
        self.assertEqual(missing_current.status, "unknown")
        self.assertIsNone(missing_current.temperature_f)

    def test_explicit_sensor_attribute_and_unit_override_are_supported(self):
        cfg = self.cfg(Probe("sensor.external_multisensor", "external", attribute="temperature", unit="°C"))
        state = sample("ready", attributes={"temperature": 5}, state="ready")
        reading = read_outdoor(cfg, {"sensor.external_multisensor": state}, NOW)
        self.assertEqual(reading.temperature_f, 41)

    def test_aliases_count_once_and_coldest_valid_source_is_conservative(self):
        cfg = self.cfg(Probe("sensor.station_a", "station"), Probe("sensor.station_alias", " station "),
                       Probe("weather.nearby_station", "provider"))
        states = {"sensor.station_a": sample(35), "sensor.station_alias": sample(31),
                  "weather.nearby_station": sample("clear", state="clear", attributes={
                      "temperature": 45, "temperature_unit": "°F"})}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.sources, reading.temperature_f), (2, 31))
        self.assertEqual(set(dict(reading.source_timestamps)), {"station", "provider"})

    def test_failed_alias_does_not_discard_usable_same_source(self):
        cfg = self.cfg(Probe("sensor.station_a", "station"), Probe("sensor.station_alias", "station"))
        states = {"sensor.station_a": sample(35, age=1801), "sensor.station_alias": sample(33)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.sources, reading.temperature_f), (1, 33))
        self.assertTrue(any("stale" in warning for warning in reading.warnings))
        self.assertTrue(any("only one independent" in warning for warning in reading.warnings))

    def test_expiry_exposes_earliest_alias_horizon_and_recomputes_without_new_report(self):
        cfg = self.cfg(Probe("sensor.old_cold_alias", "station"), Probe("sensor.new_warm_alias", "station"))
        states = {"sensor.old_cold_alias": sample(30, age=1799), "sensor.new_warm_alias": sample(40)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual(reading.next_expiry, NOW.timestamp() + 1)
        self.assertEqual(dict(reading.source_timestamps)["station"], NOW.timestamp())
        self.assertEqual(reading.temperature_f, 30)
        expired = read_outdoor(cfg, states, NOW + timedelta(seconds=1))
        self.assertEqual((expired.temperature_f, expired.sources), (40, 1))

    def test_unknown_restored_future_stale_malformed_or_implausible_data_is_excluded(self):
        cfg = self.cfg(Probe("sensor.outdoor", "station"))
        invalid = [sample(31, state="unknown"), sample(31, state="unavailable"),
                   sample(31, attributes={"restored": True}), sample(31, age=-.001),
                   sample(31, age=1800), sample(31, "K"), sample(31, None),
                   sample(151), sample(-101), sample(nan), sample(inf), sample(-inf),
                   sample(True, state=True), sample("not numeric")]
        for stamp in (None, "invalid", "2026-01-15T12:00:00"):
            state = sample(31)
            state["last_reported"] = stamp
            invalid.append(state)
        for state in invalid:
            with self.subTest(state=state):
                result = read_outdoor(cfg, {"sensor.outdoor": state}, NOW)
                self.assertEqual((result.status, result.sources, result.temperature_f), ("unknown", 0, None))
                self.assertTrue(result.warnings)

    def test_valid_source_survives_another_invalid_source_and_unknown_is_not_zero(self):
        cfg = self.cfg(Probe("sensor.good_station", "good"), Probe("sensor.offline_station", "offline"))
        states = {"sensor.good_station": sample(50), "sensor.offline_station": sample(0, state="unavailable")}
        result = read_outdoor(cfg, states, NOW)
        self.assertEqual((result.temperature_f, result.sources), (50, 1))
        self.assertTrue(result.warnings)
        unknown = read_outdoor(cfg, {}, NOW)
        self.assertIsNone(unknown.temperature_f)
        with self.assertRaises(FrozenInstanceError):
            result.temperature_f = 0

    def test_legacy_mixed_sources_still_choose_coldest_without_an_explicit_primary(self):
        cfg = self.cfg(Probe("sensor.local_air", "local"), Probe("weather.regional", "region"))
        reading = read_outdoor(cfg, {"sensor.local_air": sample(40), "weather.regional": weather(25)}, NOW)
        self.assertEqual((reading.temperature_f, reading.sources, reading.selected_role), (25, 2, "fallback"))

    def test_primary_local_sensor_ignores_a_colder_weather_fallback_and_its_expiry(self):
        cfg = self.cfg({"entity_id": "sensor.local_air", "source": "local", "role": "primary"},
                       {"entity_id": "weather.regional", "source": "region"})
        reading = read_outdoor(cfg, {"sensor.local_air": sample(40),
                                    "weather.regional": weather(20, age=5399)}, NOW)
        self.assertEqual((reading.temperature_f, reading.sources, reading.selected_role), (40, 1, "primary"))
        self.assertEqual(reading.next_expiry, NOW.timestamp() + OUTDOOR_MAX_AGE_SECONDS)
        self.assertEqual(reading.source_timestamps, (("local", NOW.timestamp()),))
        self.assertEqual(reading.coverage_intervals,
                         ((NOW.timestamp(), NOW.timestamp() + OUTDOOR_MAX_AGE_SECONDS),))

    def test_primary_loss_and_recovery_select_weather_then_local_again(self):
        cfg = self.cfg({"entity_id": "sensor.local_air", "source": "local", "role": "primary"},
                       {"entity_id": "weather.regional", "source": "region", "role": "fallback"})
        states = {"sensor.local_air": sample(40), "weather.regional": weather(25)}
        self.assertEqual(read_outdoor(cfg, states, NOW).temperature_f, 40)
        states["sensor.local_air"]["state"] = "unavailable"
        fallback = read_outdoor(cfg, states, NOW)
        self.assertEqual((fallback.temperature_f, fallback.sources, fallback.selected_role), (25, 1, "fallback"))
        self.assertEqual(fallback.next_expiry, NOW.timestamp() + WEATHER_MAX_AGE_SECONDS)
        states["sensor.local_air"] = sample(42)
        self.assertEqual(read_outdoor(cfg, states, NOW).temperature_f, 42)

    def test_coldest_valid_primary_and_alias_deduplication_remain_conservative(self):
        cfg = self.cfg({"entity_id": "sensor.primary_a", "source": "local", "role": "primary"},
                       {"entity_id": "sensor.primary_alias", "source": " local ", "role": "primary"},
                       {"entity_id": "sensor.primary_b", "source": "garden", "role": "primary"},
                       {"entity_id": "weather.regional", "source": "region"})
        states = {"sensor.primary_a": sample(40, age=1800), "sensor.primary_alias": sample(35),
                  "sensor.primary_b": sample(30), "weather.regional": weather(10)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.temperature_f, reading.sources, reading.selected_role), (30, 2, "primary"))
        self.assertEqual(set(dict(reading.source_timestamps)), {"local", "garden"})
        self.assertTrue(any("stale" in warning for warning in reading.warnings))

    def test_weather_report_lifetime_is_90_minutes_and_sensor_lifetime_stays_30(self):
        for age, known in ((5399.999, True), (5400, False)):
            with self.subTest(kind="weather", age=age):
                cfg = self.cfg(Probe("weather.regional", "region"))
                reading = read_outdoor(cfg, {"weather.regional": weather(35, age=age)}, NOW)
                self.assertEqual(reading.status == "known", known)
                if known:
                    self.assertAlmostEqual(reading.next_expiry, NOW.timestamp() + .001, places=5)
        for age, known in ((1799.999, True), (1800, False)):
            with self.subTest(kind="sensor", age=age):
                cfg = self.cfg(Probe("sensor.local_air", "local"))
                reading = read_outdoor(cfg, {"sensor.local_air": sample(35, age=age)}, NOW)
                self.assertEqual(reading.status == "known", known)

    def test_unchanged_report_renewal_uses_last_reported_not_last_updated(self):
        cfg = self.cfg(Probe("weather.regional", "region"))
        state = weather(35)
        state["last_updated"] = (NOW - timedelta(hours=4)).isoformat()
        reading = read_outdoor(cfg, {"weather.regional": state}, NOW)
        self.assertEqual(reading.status, "known")
        self.assertEqual(reading.next_expiry, NOW.timestamp() + WEATHER_MAX_AGE_SECONDS)
        self.assertEqual(read_outdoor(cfg, {"weather.regional": state},
                                     NOW + timedelta(seconds=WEATHER_MAX_AGE_SECONDS)).status, "unknown")

    def test_report_timestamp_falls_back_only_when_last_reported_is_missing(self):
        cfg = self.cfg(Probe("weather.regional", "region"))
        state = weather(35)
        state["last_updated"] = state.pop("last_reported")
        self.assertEqual(read_outdoor(cfg, {"weather.regional": state}, NOW).status, "known")
        state["last_reported"] = "malformed"
        self.assertEqual(read_outdoor(cfg, {"weather.regional": state}, NOW).status, "unknown")

    def test_mixed_domain_aliases_keep_individual_lifetimes_and_coverage(self):
        cfg = self.cfg(Probe("weather.station", "same_station"), Probe("sensor.station_alias", "same_station"))
        states = {"weather.station": weather(25, age=4000), "sensor.station_alias": sample(35, age=1200)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.temperature_f, reading.sources), (25, 1))
        self.assertEqual(reading.next_expiry, NOW.timestamp() + 600)
        self.assertEqual(reading.coverage_intervals, (
            (NOW.timestamp() - 4000, NOW.timestamp() + 1400),
            (NOW.timestamp() - 1200, NOW.timestamp() + 600),
        ))
        later = read_outdoor(cfg, states, NOW + timedelta(seconds=600))
        self.assertEqual((later.temperature_f, later.sources, later.next_expiry), (25, 1, NOW.timestamp() + 1400))

    def test_newer_alias_timestamp_does_not_extend_the_colder_old_alias(self):
        cfg = self.cfg(Probe("weather.cold_alias", "station"), Probe("sensor.fresh_alias", "station"))
        states = {"weather.cold_alias": weather(25, age=5399), "sensor.fresh_alias": sample(35)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual(reading.next_expiry, NOW.timestamp() + 1)
        self.assertEqual(dict(reading.source_timestamps), {"station": NOW.timestamp()})
        later = read_outdoor(cfg, states, NOW + timedelta(seconds=1))
        self.assertEqual((later.temperature_f, later.next_expiry), (35, NOW.timestamp() + 1800))

    def observation_cfg(self, *, entity="weather.station"):
        return self.cfg({"entity_id": entity, "source": "station",
                         "observation_time_entity": "sensor.station_observation_time"})

    def test_actual_observation_age_limits_fresh_weather_publication(self):
        cfg = self.observation_cfg()
        observation = (NOW - timedelta(seconds=5399)).isoformat()
        states = {"weather.station": weather(35), "sensor.station_observation_time": sample(observation)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual(reading.status, "known")
        self.assertEqual(reading.next_expiry, NOW.timestamp() + 1)
        self.assertEqual(reading.coverage_intervals, ((NOW.timestamp(), NOW.timestamp() + 1),))
        self.assertEqual(read_outdoor(cfg, states, NOW + timedelta(seconds=1)).status, "unknown")
        states["weather.station"] = weather(35)
        states["sensor.station_observation_time"]["state"] = (NOW - timedelta(seconds=5400)).isoformat()
        self.assertEqual(read_outdoor(cfg, states, NOW).status, "unknown")

    def test_observation_time_does_not_extend_a_stale_temperature_report(self):
        cfg = self.observation_cfg()
        states = {"weather.station": weather(35, age=5400),
                  "sensor.station_observation_time": sample(NOW.isoformat())}
        self.assertEqual(read_outdoor(cfg, states, NOW).status, "unknown")

    def test_explicit_observation_and_report_keep_their_own_coverage_boundaries(self):
        cfg = self.observation_cfg()
        states = {"weather.station": weather(35, age=1000),
                  "sensor.station_observation_time": sample((NOW - timedelta(seconds=100)).isoformat())}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual(reading.source_timestamps, (("station", NOW.timestamp() - 1000),))
        self.assertEqual(reading.coverage_intervals,
                         ((NOW.timestamp() - 100, NOW.timestamp() + 4400),))
        self.assertEqual(reading.next_expiry, NOW.timestamp() + 4400)

    def test_unbound_model_issue_time_is_not_guessed_to_be_an_observation_time(self):
        cfg = self.cfg(Probe("weather.regional", "model"))
        state = weather(35)
        state["attributes"].update({"updated_at": (NOW - timedelta(hours=4)).isoformat(),
                                    "forecast": [{"temperature": -50}]})
        reading = read_outdoor(cfg, {"weather.regional": state}, NOW)
        self.assertEqual((reading.temperature_f, reading.status), (35, "known"))
        self.assertEqual(reading.next_expiry, NOW.timestamp() + WEATHER_MAX_AGE_SECONDS)

    def test_bound_sensor_observation_age_uses_sensor_limit(self):
        cfg = self.observation_cfg(entity="sensor.station_temperature")
        states = {"sensor.station_temperature": sample(35),
                  "sensor.station_observation_time": sample((NOW - timedelta(seconds=1800)).isoformat())}
        self.assertEqual(read_outdoor(cfg, states, NOW).status, "unknown")

    def test_invalid_observation_time_is_not_replaced_by_a_fresh_ha_report(self):
        cfg = self.observation_cfg()
        invalid = [None, sample("unknown"), sample("unavailable"), sample("not a timestamp"),
                   sample("2026-01-15T12:00:00"), sample((NOW + timedelta(microseconds=1)).isoformat()),
                   sample(NOW.isoformat(), attributes={"restored": True}), sample(True, state=True)]
        for state in invalid:
            with self.subTest(observation_state=state):
                states = {"weather.station": weather(35), "sensor.station_observation_time": state}
                reading = read_outdoor(cfg, states, NOW)
                self.assertEqual(reading.status, "unknown")
                self.assertTrue(any("observation time" in warning for warning in reading.warnings))

    def test_stale_bound_observation_can_fall_back_to_another_valid_source(self):
        cfg = self.cfg({"entity_id": "weather.station", "source": "station",
                        "observation_time_entity": "sensor.station_observation_time", "role": "primary"},
                       {"entity_id": "weather.model", "source": "model"})
        states = {"weather.station": weather(35),
                  "sensor.station_observation_time": sample((NOW - timedelta(seconds=5400)).isoformat()),
                  "weather.model": weather(40)}
        reading = read_outdoor(cfg, states, NOW)
        self.assertEqual((reading.temperature_f, reading.selected_role), (40, "fallback"))


class OutdoorWiringTests(unittest.TestCase):
    def descriptors(self):
        return [{"entity_id": "sensor.exterior_current", "source": "garden"},
                {"entity_id": "weather.local_current", "source": "provider", "attribute": "temperature"}]

    def test_private_wiring_round_trip_is_optional_and_defensive(self):
        old = example_bindings().to_dict()
        self.assertNotIn("outdoor", old)
        raw = deepcopy(old)
        raw["outdoor"] = self.descriptors()
        bindings = HouseBindings.from_dict(raw)
        self.assertIsInstance(bindings.outdoor, tuple)
        self.assertEqual(bindings.to_dict(), raw)
        raw["outdoor"][0]["source"] = "changed"
        self.assertEqual(bindings.outdoor[0].source, "garden")
        self.assertEqual(HouseBindings.from_dict(bindings.to_dict()), bindings)
        self.assertEqual(HouseBindings.from_dict(old).outdoor, ())

    def test_bad_private_source_descriptors_and_embedded_rules_are_rejected(self):
        bad = [None, {}, "sensor.outdoor", ["sensor.outdoor"],
               [{"entity_id": "sensor.outdoor"}],
               [{"entity_id": "sensor.outdoor", "source": ""}],
               [{"entity_id": "climate.outdoor", "source": "station"}],
               [{"entity_id": "weather.outdoor", "source": "provider", "attribute": "forecast"}],
               [{"entity_id": "sensor.outdoor", "source": "station", "unit": "K"}],
               [{"entity_id": "sensor.outdoor", "source": "station", "stale_seconds": 9000}],
               [{"entity_id": "sensor.outdoor", "source": "station", "offset_f": 10}],
               [{"entity_id": "sensor.outdoor", "source": "station"}] * 2]
        for value in bad:
            raw = example_bindings().to_dict()
            raw["outdoor"] = value
            with self.subTest(value=value), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)
        raw = example_bindings().to_dict()
        raw["protection"] = ProtectionTemperatures().to_dict()
        with self.assertRaises(BindingsError):
            HouseBindings.from_dict(raw)

    def test_config_round_trip_preserves_legacy_none_and_explicit_protection(self):
        old = Config.from_dict(configuration())
        self.assertIsNone(old.protection)
        self.assertNotIn("protection", old.to_dict())
        self.assertNotIn("outdoor_probes", old.to_dict())
        raw = configuration()
        raw["outdoor_probes"] = self.descriptors()
        raw["protection"] = {"normal_heating_floor_f": 59, "boundary_tie_mode": "cool"}
        cfg = Config.from_dict(raw)
        self.assertEqual(cfg.protection.normal_heating_floor_f, 59)
        self.assertEqual(cfg.protection.boundary_tie_mode, "cool")
        self.assertEqual(Config.from_dict(cfg.to_dict()).to_dict(), cfg.to_dict())
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))

    def test_fixed_house_chooses_explicit_protection_and_private_outdoor_sources(self):
        raw = example_bindings().to_dict()
        raw["outdoor"] = self.descriptors()
        cfg = house_configuration(bindings=HouseBindings.from_dict(raw))
        self.assertEqual(cfg.protection, ProtectionTemperatures())
        self.assertEqual([probe.entity_id for probe in cfg.outdoor_probes],
                         [row["entity_id"] for row in self.descriptors()])
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))


if __name__ == "__main__":
    unittest.main()
