"""All new numeric entities through HA serialization and its actual exporter."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
import logging
from datetime import timedelta
import unittest
from unittest.mock import patch

from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.numeric_diagnostics import (
    AIRFLOW_CODES, FAN_FIELDS, GUARD_CODES, GUARD_FIELDS, SOURCE_CODES, SOURCE_FIELDS,
    metrics, source_id,
)
from custom_components.house_climate.controller.shutdown_diagnostics import ShutdownDiagnostics
from custom_components.house_climate.sensor import (
    AUXILIARY_METRICS, SHUTDOWN_METRICS, AuxiliaryDiagnostic, HeaterPowerStatus,
    NumericDiagnostic, async_setup_entry,
    ControllerStatus, RoomTemperature, FanPlan,
)
from custom_components.house_climate.climate import RoomClimate
from custom_components.house_climate.number import RoomDesiredTemperature
from custom_components.house_climate.switch import ControllerEnabled, AuxiliaryManual
from custom_components.house_climate.button import ResumeRoomSchedule
from tests.test_directed_airflow import directed_config, FAN
from tests_homeassistant import test_auxiliary_prometheus as base


class NumericPrometheusTests(unittest.IsolatedAsyncioTestCase):
    handle, until, runtime, claim = (base.AuxiliaryPrometheusTests.handle, base.AuxiliaryPrometheusTests.until,
                                   base.AuxiliaryPrometheusTests.runtime, base.AuxiliaryPrometheusTests.claim)
    settle_watchdog, feedback = base.AuxiliaryPrometheusTests.settle_watchdog, base.AuxiliaryPrometheusTests.feedback
    scrape, sample = base.AuxiliaryPrometheusTests.scrape, base.AuxiliaryPrometheusTests.sample
    asyncTearDown = base.AuxiliaryPrometheusTests.asyncTearDown

    async def asyncSetUp(self):
        await base.AuxiliaryPrometheusTests.asyncSetUp(self)
        self.wall = datetime.now(timezone.utc).timestamp()
        previous_config = self.controller.config
        raw = directed_config()
        raw.update(heater_entity=self.controller.config.heater_entity, auxiliary_room="gamma",
                   outdoor_probes=[{"entity_id": "weather.synthetic", "source": "outdoor"}])
        self.controller.config = Config.from_dict(raw)
        observations = ShutdownDiagnostics()
        observations.sampled_at = self.wall
        observations.guard_evaluated_at = self.wall
        observations.guard_current = {"live": True, "blocked": False, "manual_override": False,
                                      "battery_reserve_percent": 40}
        class CapturedGuard:
            shutdown_observations = observations
            def __getattr__(self, name):
                raise AssertionError(f"Sensor read safety getter: {name}")
        self.captured_guard = CapturedGuard()
        self.controller.power_guard = self.captured_guard
        added = []
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.controller.power_guard = None  # Existing fixture teardown has no synthetic guard lifecycle.
        self.numeric = {sensor.key: sensor for sensor in added if isinstance(sensor, NumericDiagnostic)}
        self.group = next(iter(self.numeric.values()))._diagnostic_group
        # A projection-only runtime retains the captured guard and real ownership.
        self.view = SimpleNamespace(config=self.controller.config, controls=self.controller.controls,
                                    power_guard=self.captured_guard, status={})
        self.controller.config = previous_config
        self.group.runtime = self.view
        self.definitions = self.group.definitions
        self.assertEqual(len(self.numeric), len(self.definitions))
        old = next(sensor for sensor in added if isinstance(sensor, AuxiliaryDiagnostic) and sensor.key == "sequence")
        self.assertIs(old._diagnostic_group, self.group)  # No extra timer per new sensor.
        for key, sensor in self.numeric.items():
            sensor.entity_id = "sensor.home_climate_auxiliary_numeric_" + key.replace(".", "_")
            self.sensors[key] = sensor
        self.seed()
        await self.platform.async_add_entities(list(self.numeric.values()))

    def seed(self):
        now = self.wall
        self.view.status = {"event": "live", "timestamp": now, "rooms": {},
                            "fan_reports": {}, "fan_observations": {}, "fan_airflow": {}}
        for entity in self.view.config.fans.values():
            self.view.status["fan_reports"][entity] = {"state": "on", "last_reported": now,
                "attributes": {"percentage": 0, "angle_low": 0, "angle_high": 90}}
            self.view.status["fan_observations"][entity] = {"sampled_at": now,
                "requested_percentage": 20, "acknowledged": False, "pending": True, "failed": False}
            self.view.status["fan_airflow"][entity] = {"requested": {"lower_angle": 0, "upper_angle": 90},
                "execution": {"phase": "angles"}}
        for room, zone in self.view.config.zones.items():
            self.view.status["rooms"][room] = {"source_evidence": [
                {"source": probe.source, "reported_at": now, "measurement_at": now - 1,
                 "freshness": "qualified"} for probe in zone.probes]}
        self.view.status["outdoor_source_evidence"] = {"sampled_at": now, "sources": [
            {"source": "outdoor", "reported_at": now, "measurement_at": now - 1, "valid_until": now + 100}]}

    @staticmethod
    def family(metric):
        return "homeassistant_sensor_" + {None: "state", "s": "unit_s", "%": "unit_percent", "°": "unit_u0xb0"}[metric.unit]

    async def test_every_new_metric_has_actual_series_fixed_labels_and_zero_values(self):
        samples, text = await self.scrape()
        for metric in self.definitions:
            with self.subTest(metric=metric.key):
                series = self.sample(samples, metric.key, self.family(metric))
                if metric.field == "packet_received_at":
                    self.assertEqual(series, [], "No packet receipt is fabricated for ordinary source fixtures")
                    continue
                self.assertEqual(len(series), 1)
                self.assertAlmostEqual(series[0].value, self.group.values[metric.key], delta=1e-5)
                self.assertEqual(set(series[0].labels), {"entity", "friendly_name", "domain"})
        self.assertEqual(self.sample(samples, f"fan_{FAN}_reported_percent", "homeassistant_sensor_unit_percent")[0].value, 0)
        self.assertNotIn("reason=", text)
        self.assertNotIn("profile=", text)
        self.assertEqual(len(self.group.unsubscribers), 2)

    async def test_stale_snapshots_withdraw_flags_and_measurements_but_keep_historical_times(self):
        await self.scrape()
        before = deepcopy(self.view.status)
        with patch("custom_components.house_climate.sensor.time.time", return_value=self.wall + 31):
            samples, _ = await self.scrape()
        for key, family in (("guard_ready", "homeassistant_sensor_state"),
                            (f"fan_{FAN}_speed_acknowledged", "homeassistant_sensor_state"),
                            (f"fan_{FAN}_reported_percent", "homeassistant_sensor_unit_percent"),
                            (f"source_{source_id(None, 'outdoor')}_qualified", "homeassistant_sensor_state")):
            self.assertEqual(self.sample(samples, key, family), [])
            self.assertEqual(self.sample(samples, key, "homeassistant_entity_available")[0].value, 0)
        self.assertEqual(before, self.view.status)
        self.assertTrue(self.sample(samples, "guard_sampled_at", "homeassistant_sensor_unit_s"))
        self.assertEqual(self.sample(samples, "guard_reserve_percent", "homeassistant_sensor_unit_percent")[0].value, 40)

    async def test_manual_ownership_persists_but_restart_does_not_restore_observations(self):
        c = self.controller.controls
        await c.async_set_auxiliary_manual(True)
        samples, _ = await self.scrape()
        self.assertEqual(self.sample(samples, "auxiliary_manual_owned")[0].value, 1)
        restarted = NativeControls(self.hass, c.config, True, "native", lambda: None)
        await restarted.async_load()
        self.assertTrue(restarted.auxiliary_manual)
        self.view.controls = self.group.controls = restarted
        self.view.status = {"event": "waiting_for_home_assistant"}
        self.view.power_guard = SimpleNamespace(shutdown_observations=ShutdownDiagnostics())
        samples, _ = await self.scrape()
        self.assertEqual(self.sample(samples, "auxiliary_manual_owned")[0].value, 1)
        for key in ("guard_ready", f"fan_{FAN}_speed_acknowledged", f"fan_{FAN}_speed_pending"):
            self.assertEqual(self.sample(samples, key), [])

    async def test_text_guard_sensor_and_numeric_refresh_never_read_safety_getters(self):
        sensor = HeaterPowerStatus(SimpleNamespace(entry_id="capture", runtime_data=self.view))
        for _ in range(3):
            self.assertEqual(sensor.native_value, "Ready")
            self.assertEqual(sensor.extra_state_attributes["blocked"], False)
            await self.scrape()
        self.assertEqual(self.calls, [])

    async def test_existing_native_controls_and_sensor_export_inventory(self):
        self.view.stop_event = asyncio.Event()
        self.view.live = False
        self.view.status.update(event="shadow", mode="off", head_targets_f={}, fans={FAN: 20})
        self.view.status["rooms"]["alpha"]["temperature"] = 68
        self.view.comfort = SimpleNamespace(
            effective=lambda room: {"target_f": 72, "low_f": 70, "high_f": 74,
                                    "mode": "schedule", "expires_at": None, "comfort_enabled": True},
            bounds=lambda room: (60, 80))
        entry = SimpleNamespace(entry_id="native", runtime_data=self.view)
        entities = {
            "sensor": [RoomTemperature(entry, "alpha"), FanPlan(entry, self.view.config.fan_controls[FAN], "Test fan"),
                       ControllerStatus(entry)],
            "climate": [RoomClimate(entry, "alpha")],
            "number": [RoomDesiredTemperature(entry, "alpha")],
            "switch": [ControllerEnabled(entry), AuxiliaryManual(entry)],
            "button": [ResumeRoomSchedule(entry, "alpha")],
        }
        for domain, items in entities.items():
            platform = EntityPlatform(hass=self.hass, logger=logging.getLogger(__name__), domain=domain,
                platform_name="house_climate", platform=None, scan_interval=timedelta(seconds=30),
                entity_namespace=None)
            platform.config_entry = self.entry
            self.addAsyncCleanup(platform.async_reset)
            for index, entity in enumerate(items):
                entity.entity_id = f"{domain}.home_climate_inventory_{index}"
            await platform.async_add_entities(items)
            for entity in items:
                self.exporter.handle_state(self.hass.states.get(entity.entity_id))
        samples, _ = await self.scrape()
        expected = {
            "homeassistant_sensor_temperature_celsius": 20,
            "homeassistant_sensor_unit_percent": 20,
            "homeassistant_number_state_celsius": (72 - 32) * 5 / 9,
            "homeassistant_climate_current_temperature_celsius": 20,
            "homeassistant_climate_target_temperature_low_celsius": (70 - 32) * 5 / 9,
            "homeassistant_climate_target_temperature_high_celsius": (74 - 32) * 5 / 9,
            "homeassistant_switch_state": 0,
            "homeassistant_climate_mode": 1,
        }
        inventory = [s for s in samples if ".home_climate_inventory_" in s.labels.get("entity", "")]
        reference = Path("docs/metrics.md").read_text()
        for family, value in expected.items():
            series = [s for s in inventory if s.name == family]
            self.assertTrue(series, family)
            self.assertAlmostEqual(series[0].value, value, msg=family)
        for sample in inventory:
            self.assertIn(sample.name, reference, sample.name)
        families = {sample.name for sample in inventory}
        self.assertNotIn("homeassistant_climate_target_temperature_celsius", families)
        self.assertNotIn("homeassistant_climate_fan_mode", families)
        self.assertNotIn("homeassistant_sensor_state", families)  # Text status isn't a numeric state.

    async def test_expired_or_untrusted_reports_remove_previous_series_with_fresh_worker(self):
        report = self.view.status["fan_reports"][FAN]
        for changed in ({"state": "unknown"}, {"state": "unavailable"},
                        {"last_reported": self.wall - 181}, {"last_reported": self.wall + 1},
                        {"attributes": {"restored": True}}, {"attributes": {"assumed_state": True}}):
            original = deepcopy(report)
            await self.scrape()
            report.update(changed)
            samples, _ = await self.scrape()
            self.assertEqual(self.sample(samples, f"fan_{FAN}_reported_percent", "homeassistant_sensor_unit_percent"), [])
            self.assertEqual(self.sample(samples, f"fan_{FAN}_speed_acknowledged"), [])
            report.clear()
            report.update(original)
        source = self.view.status["rooms"]["alpha"]["source_evidence"][0]
        source["measurement_at"] = self.wall - self.view.config.sensor_stale_seconds - 1
        samples, _ = await self.scrape()
        key = f"source_{source_id('alpha', source['source'])}_qualified"
        self.assertEqual(self.sample(samples, key), [])

    async def test_packet_receipt_exports_separately_without_fabricating_a_measurement(self):
        source = self.view.status["rooms"]["alpha"]["source_evidence"][0]
        source.update(reported_at=self.wall - self.view.config.sensor_stale_seconds - 1,
                      packet_received_at=self.wall, measurement_at=None)
        samples, _ = await self.scrape()
        prefix = f"source_{source_id('alpha', source['source'])}"
        self.assertAlmostEqual(self.sample(samples, prefix + "_packet_received_at", "homeassistant_sensor_unit_s")[0].value,
                               self.wall, delta=1e-5)
        self.assertEqual(self.sample(samples, prefix + "_qualified")[0].value, 1)
        self.assertEqual(self.sample(samples, prefix + "_measurement_at", "homeassistant_sensor_unit_s"), [])

    async def test_reference_covers_every_sensor_key_and_bounded_code(self):
        reference = Path("docs/metrics.md").read_text()
        for key in (*AUXILIARY_METRICS, *SHUTDOWN_METRICS, "auxiliary_manual_owned",
                    *(f"guard_{field}" for field in GUARD_FIELDS),
                    *(f"fan_{{fan}}_{field}" for field in FAN_FIELDS),
                    *(f"source_{{source}}_{field}" for field in SOURCE_FIELDS)):
            self.assertIn(f"`{key}`", reference, key)
        for codes in (GUARD_CODES, AIRFLOW_CODES, SOURCE_CODES):
            for name, value in codes.items():
                self.assertIn(f"`{value}` = `{name}`", reference)
