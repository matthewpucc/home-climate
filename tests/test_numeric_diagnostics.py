"""Captured evidence expires without re-running admission or acknowledgements."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from house_climate.config import Config, Settings
from house_climate.cadence import DecisionCadence
from house_climate.executor import Executor
from house_climate.ha import HAActivationDeferred
from house_climate.numeric_diagnostics import (
    AIRFLOW_CODES, GUARD_CODES, SOURCE_CODES, fan_values, guard_values,
    metrics, source_values, sources, values,
)
from house_climate.policy import Plan
from house_climate.protection import ProtectionTemperatures
from house_climate.shutdown_diagnostics import ShutdownDiagnostics
from tests.test_controller import FakeHA
from tests.test_directed_airflow import directed_config, FAN
from tests.test_rooms import states


class NumericDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(directed_config())
        self.now = datetime.now(timezone.utc)
        self.wall = self.now.timestamp()
        self.status = {"event": "live", "timestamp": self.now.isoformat(),
            "fan_reports": {FAN: {"state": "on", "last_reported": self.now.isoformat(),
                "attributes": {"percentage": 20, "angle_low": 0, "angle_high": 90}}},
            "fan_observations": {FAN: {"sampled_at": self.wall, "requested_percentage": 60,
                "acknowledged": False, "pending": True, "failed": False}},
            "fan_airflow": {FAN: {"requested": {"lower_angle": 100, "upper_angle": 190},
                "execution": {"phase": "angles"}}}}

    def test_fan_zero_missing_stale_future_and_untrusted_are_distinct(self):
        for attr in ("percentage", "angle_low", "angle_high"):
            for invalid in (None, True, float("nan"), -1, 361):
                data = deepcopy(self.status)
                data["fan_reports"][FAN]["attributes"][attr] = invalid
                field = {"percentage": "reported_percent", "angle_low": "reported_lower_angle",
                         "angle_high": "reported_upper_angle"}[attr]
                self.assertIsNone(fan_values(data, FAN, self.wall, 30).get(field))
        current = fan_values(self.status, FAN, self.wall, 30)
        self.assertEqual(current["reported_lower_angle"], 0)
        self.assertEqual(current["speed_acknowledged"], 0)
        self.assertEqual(current["requested_percent"], 60)
        for mutate in (lambda row: row.update(state="unavailable"),
                       lambda row: row.update(state="unknown"),
                       lambda row: row["attributes"].update(restored=True),
                       lambda row: row["attributes"].update(assumed_state=True),
                       lambda row: row.update(last_reported=(self.now - timedelta(seconds=181)).isoformat()),
                       lambda row: row.update(last_reported=(self.now + timedelta(seconds=1)).isoformat())):
            data = deepcopy(self.status)
            mutate(data["fan_reports"][FAN])
            result = fan_values(data, FAN, self.wall, 30)
            for key in ("reported_percent", "reported_lower_angle", "speed_acknowledged", "airflow_acknowledged"):
                self.assertIsNone(result.get(key), key)
            self.assertEqual(result["requested_percent"], 60)
        stale = fan_values(self.status, FAN, self.wall + 31, 30)
        self.assertEqual(stale, {"reported_at": self.wall})

    def test_preview_and_missing_execution_do_not_claim_requests_or_ack(self):
        self.status["event"] = "shadow"
        result = fan_values(self.status, FAN, self.wall, 30)
        self.assertEqual(result["reported_percent"], 20)
        self.assertNotIn("requested_percent", result)
        self.assertNotIn("airflow_acknowledged", result)
        self.status["event"] = "live"
        self.status.pop("fan_observations")
        self.assertNotIn("speed_pending", fan_values(self.status, FAN, self.wall, 30))
        for phase, code in AIRFLOW_CODES.items():
            self.status["fan_airflow"][FAN]["execution"]["phase"] = phase
            self.assertEqual(fan_values(self.status, FAN, self.wall, 30)["airflow_phase_code"], code)
        self.status["fan_airflow"][FAN]["execution"]["phase"] = "future_phase"
        self.assertIsNone(fan_values(self.status, FAN, self.wall, 30)["airflow_pending"])

    def test_actual_speed_match_pending_quantization_and_deferred_capture(self):
        data = states(self.cfg, now=self.now)
        data[FAN]["attributes"].update(percentage_step=100 / 3)
        ha = FakeHA(data)
        executor = Executor(self.cfg, ha, "°F")
        executor.fan(FAN, 60, Plan(), ha.states(), 0)
        self.assertEqual(executor.fan_observations[FAN]["requested_percentage"], 66)
        self.assertFalse(executor.fan_observations[FAN]["acknowledged"])
        self.assertTrue(executor.fan_observations[FAN]["pending"])
        executor.fan(FAN, 60, Plan(), ha.states(), 1)
        self.assertTrue(executor.fan_observations[FAN]["acknowledged"])
        self.assertFalse(executor.fan_observations[FAN]["pending"])
        with patch.object(ha, "service", side_effect=HAActivationDeferred("synthetic defer")):
            executor.fan(FAN, 100, Plan(), ha.states(), 2)
        self.assertFalse(executor.fan_observations[FAN]["acknowledged"])
        self.assertFalse(executor.fan_observations[FAN]["pending"])
        # Restart has no acknowledgment history, including a currently matching fan.
        self.assertEqual(Executor(self.cfg, ha, "°F").fan_observations, {})

    def test_sources_aliases_acquisition_and_expiry(self):
        source = self.cfg.zones["alpha"].probes[0].source
        sample = {"source": source, "reported_at": self.wall, "measurement_at": self.wall - 10,
                  "freshness": "qualified"}
        self.status["rooms"] = {"alpha": {"source_evidence": [sample]}}
        result = source_values(self.status, "alpha", source, self.cfg, self.wall, 30)
        self.assertEqual(result["qualified"], 1)
        self.assertEqual(result["measurement_at"], self.wall - 10)
        self.assertIsNone(source_values(self.status, "alpha", source, self.cfg, self.wall + 31, 30).get("qualified"))
        sample["measurement_at"] = self.wall - self.cfg.sensor_stale_seconds - 1
        self.assertIsNone(source_values(self.status, "alpha", source, self.cfg, self.wall, 30)["qualified"])
        self.assertIsNone(source_values(self.status, "alpha", "missing", self.cfg, self.wall, 30)["qualified"])
        sample["measurement_at"] = None
        self.assertIsNone(source_values(self.status, "alpha", source, self.cfg, self.wall, 30)["measurement_at"])
        zone = self.cfg.zones["alpha"]
        alias = replace(zone.probes[0], entity_id="sensor.alias")
        cfg = replace(self.cfg, zones={**self.cfg.zones, "alpha": replace(zone, probes=(*zone.probes, alias))})
        self.assertEqual(len(sources(cfg)), len(sources(self.cfg)))

    def test_outdoor_existing_evaluation_captured_and_maintenance_renews_snapshot(self):
        cfg = replace(self.cfg, protection=ProtectionTemperatures(),
            outdoor_probes=[{"entity_id": "sensor.outside", "source": "garden", "role": "primary"}])
        data = states(cfg, now=self.now)
        data["sensor.outside"] = {"state": "50", "last_reported": self.now.isoformat(),
                                  "attributes": {"unit_of_measurement": "°F"}}
        cadence = DecisionCadence(cfg)
        settings = Settings.read({}, cfg)
        for seconds in (0, 15):
            instant = self.now + timedelta(seconds=seconds)
            plan = cadence.decide(data, instant, settings, monotonic_now=seconds)
            evidence = plan.outdoor_source_evidence
            self.assertEqual(evidence["sampled_at"], instant.timestamp())
            result = source_values({"outdoor_source_evidence": evidence}, None, "garden", cfg, instant.timestamp(), 30)
            self.assertEqual(result["reported_at"], self.wall)
            self.assertEqual(result["qualified"], 1)
            self.assertIsNone(result["measurement_at"])

    def test_guard_captured_only_manual_and_stale_unknown(self):
        observations = ShutdownDiagnostics()
        observations.sampled_at = self.wall
        observations.guard_evaluated_at = self.wall
        observations.guard_current = {"blocked": False, "live": True, "manual_override": False,
                                      "battery_reserve_percent": 40}
        self.assertEqual(guard_values(observations, self.wall)["ready"], 1)
        observations.guard_current["manual_override"] = True
        self.assertEqual(guard_values(observations, self.wall)["ready"], 0)
        self.assertEqual(guard_values(observations, self.wall)["reason_code"], GUARD_CODES["manual"])
        self.assertNotIn("blocked", guard_values(observations, self.wall + 31))
        self.assertEqual(guard_values(observations, self.wall + 31)["reserve_percent"], 40)
        observations.sampled_at = self.wall + 31  # Republishing cannot renew an old evaluation.
        self.assertNotIn("ready", guard_values(observations, self.wall + 31))
        self.assertNotIn("ready", guard_values(ShutdownDiagnostics(), self.wall))
        class Guard:
            shutdown_observations = observations
            def __getattr__(self, name):
                raise AssertionError(f"Safety getter read: {name}")
        runtime = SimpleNamespace(config=self.cfg, status=self.status, power_guard=Guard(),
                                  controls=SimpleNamespace(auxiliary_manual=True))
        definitions = metrics(self.cfg, guard=True)
        before = deepcopy(observations.__dict__)
        values(runtime, definitions, self.wall)
        self.assertEqual(before, observations.__dict__)
