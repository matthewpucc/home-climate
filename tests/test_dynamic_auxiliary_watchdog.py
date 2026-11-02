"""Modern dynamic auxiliary bounds do not retain the legacy 70 F ceiling."""
from dataclasses import replace
import unittest

from house_climate.config import Config
from house_climate.ha_package import package, watchdog_signature
from tests.test_controller import NIGHT, config, fixture
from tests.test_watchdog import WatchdogTemplateTests, Environment


@unittest.skipIf(Environment is None, "Install Jinja2 to render HA templates")
class DynamicAuxiliaryWatchdogTests(unittest.TestCase):
    environment = WatchdogTemplateTests.environment

    def test_current_bound_allows_warm_profile_but_missing_or_reached_bound_stops(self):
        cfg = replace(Config.from_dict(config().to_dict()), auxiliary_dynamic_stop=True)
        self.data = fixture(cfg, NIGHT, living=71)
        entity = cfg.entity("auxiliary_stop_limit_f", "input_number")
        generated = package(cfg)
        template = generated["template"][0]["binary_sensor"][0]["state"]
        render = lambda: self.environment().from_string(template).render().strip()
        self.assertEqual(render(), "False")
        self.data[entity] = {"state": "72"}
        self.assertEqual(render(), "True")
        self.data[entity]["state"] = "71"
        self.assertEqual(render(), "False")
        self.assertNotEqual(watchdog_signature(cfg), watchdog_signature(replace(cfg, auxiliary_dynamic_stop=False)))

    def test_persisted_excessive_bound_cannot_bypass_configured_protection(self):
        cfg = replace(Config.from_dict(config().to_dict()), auxiliary_dynamic_stop=True)
        self.data = fixture(cfg, NIGHT, living=80)
        self.data[cfg.entity("auxiliary_stop_limit_f", "input_number")] = {"state": "200"}
        template = package(cfg)["template"][0]["binary_sensor"][0]["state"]
        self.assertEqual(self.environment().from_string(template).render().strip(), "False")
