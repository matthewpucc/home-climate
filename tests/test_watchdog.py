"""Render the actual HA templates when Jinja2 is available (test-only dependency)."""
import math
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

try:
    from jinja2 import Environment
except ImportError:
    Environment = None

from house_climate.ha_package import package
from tests.test_controller import NIGHT, config, fixture


@unittest.skipIf(Environment is None, "Install Jinja2 to render HA templates in tests")
class WatchdogTemplateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.data = fixture(self.cfg)
        self.generated = package(self.cfg)
        self.safe_template = self.generated["template"][0]["binary_sensor"][0]["state"]

    def environment(self, states=None):
        raw = self.data

        class HAStates:
            def __getitem__(self, entity):
                state = raw.get(entity)
                return SimpleNamespace(state=state["state"], last_updated=state.get("last_updated"), last_reported=state.get("last_reported")) if state else None

            def __call__(self, entity):
                return raw.get(entity, {}).get("state", "unknown")

        def number(value):
            try:
                return math.isfinite(float(value))
            except (ValueError, TypeError):
                return False

        def timestamp(value, default=0):
            if isinstance(value, datetime):
                return value.timestamp()
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError, AttributeError):
                return default

        env = Environment()
        env.globals.update(states=HAStates(), state_attr=lambda e, a: raw.get(e, {}).get("attributes", {}).get(a),
                           now=lambda: NIGHT, as_timestamp=timestamp, is_number=number,
                           is_state=lambda e, s: raw.get(e, {}).get("state") == s)
        return env

    def render_safe(self):
        return self.environment().from_string(self.safe_template).render().strip()

    def test_normal_room_is_safe(self):
        self.assertEqual(self.render_safe(), "True")

    def test_single_hot_reading_stops_heater_without_median(self):
        self.data[self.cfg.zones["living"].probes[0].entity_id]["state"] = "70"
        self.assertEqual(self.render_safe(), "False")

    def test_stale_and_invalid_probes_are_unsafe(self):
        for probe in self.cfg.zones["living"].probes:
            self.data[probe.entity_id]["last_updated"] = (NIGHT - timedelta(hours=1)).isoformat()
        self.assertEqual(self.render_safe(), "False")
        for probe in self.cfg.zones["living"].probes:
            self.data.pop(probe.entity_id)
        self.assertEqual(self.render_safe(), "False")

    def test_fresh_report_is_accepted_when_temperature_did_not_change(self):
        for probe in self.cfg.zones["living"].probes:
            self.data[probe.entity_id]["last_updated"] = (NIGHT - timedelta(days=1)).isoformat()
            self.data[probe.entity_id]["last_reported"] = NIGHT.isoformat()
        self.assertEqual(self.render_safe(), "True")

    def test_celsius_heater_cutoff(self):
        for probe in self.cfg.zones["living"].probes:
            self.data[probe.entity_id].update(state="20", attributes={"unit_of_measurement": "°C"})
        self.assertEqual(self.render_safe(), "True")
        self.data[self.cfg.zones["living"].probes[0].entity_id]["state"] = "22"
        self.assertEqual(self.render_safe(), "False")

    def test_lease_disable_and_probe_health_conditions(self):
        condition = self.generated["automation"][0]["conditions"][1]["value_template"]
        self.data.update({"input_boolean.hp_enabled": {"state": "on"},
                          "input_datetime.hp_heartbeat": {"state": NIGHT.isoformat()},
                          "binary_sensor.hp_living_probe_safe": {"state": "on"}})
        render = lambda: self.environment().from_string(condition).render().strip()
        self.assertEqual(render(), "False")
        self.data["input_datetime.hp_heartbeat"]["state"] = (NIGHT - timedelta(seconds=121)).isoformat()
        self.assertEqual(render(), "True")
        self.data["input_datetime.hp_heartbeat"]["state"] = NIGHT.isoformat()
        self.data["input_boolean.hp_enabled"]["state"] = "off"
        self.assertEqual(render(), "True")
        self.data["input_boolean.hp_enabled"]["state"] = "on"
        self.data["binary_sensor.hp_living_probe_safe"]["state"] = "off"
        self.data[self.cfg.heater_entity]["state"] = "heat"
        self.assertEqual(render(), "True")

    def test_warm_room_allows_fan_only_but_expired_lease_still_stops_it(self):
        condition = self.generated["automation"][0]["conditions"][1]["value_template"]
        self.data.update({"input_boolean.hp_enabled": {"state": "on"},
                          "input_datetime.hp_heartbeat": {"state": NIGHT.isoformat()},
                          "binary_sensor.hp_living_probe_safe": {"state": "off"}})
        self.data[self.cfg.heater_entity]["state"] = "fan_only"
        render = lambda: self.environment().from_string(condition).render().strip()
        self.assertEqual(render(), "False")
        self.data[self.cfg.heater_entity]["attributes"]["hvac_action"] = "heating"
        self.assertEqual(render(), "True")
        self.data[self.cfg.heater_entity]["attributes"]["hvac_action"] = "fan"
        self.data["input_datetime.hp_heartbeat"]["state"] = (NIGHT - timedelta(seconds=121)).isoformat()
        self.assertEqual(render(), "True")

    def test_unknown_heater_state_remains_protected_by_probe_cutoff(self):
        condition = self.generated["automation"][0]["conditions"][1]["value_template"]
        self.data.update({"input_boolean.hp_enabled": {"state": "on"},
                          "input_datetime.hp_heartbeat": {"state": NIGHT.isoformat()},
                          "binary_sensor.hp_living_probe_safe": {"state": "off"}})
        self.data[self.cfg.heater_entity]["state"] = "unavailable"
        self.assertEqual(self.environment().from_string(condition).render().strip(), "True")


class GenericWatchdogTemplateTests(WatchdogTemplateTests):
    def setUp(self):
        from house_climate.config import Config
        self.cfg = Config.from_dict(config().to_dict())
        self.data = fixture(self.cfg)
        self.generated = package(self.cfg)
        self.safe_template = self.generated["template"][0]["binary_sensor"][0]["state"]

    def environment(self, states=None):
        old_probe_state = self.data.get("binary_sensor.hp_living_probe_safe")
        if old_probe_state is not None:
            self.data[self.cfg.entity("auxiliary_probe_safe", "binary_sensor")] = old_probe_state
        return super().environment(states)
