"""Native timeout rules are fixed and cannot be changed by stale options."""
import json
from pathlib import Path
import unittest

from custom_components.house_climate.configuration import entry_configuration
from custom_components.house_climate.house_policy import house_configuration
from custom_components.house_climate.controller.bindings import example_bindings


class FixedHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.bindings = example_bindings()

    def test_controller_and_auxiliary_have_distinct_fixed_leases(self):
        cfg = house_configuration(bindings=self.bindings)
        self.assertEqual(cfg.heartbeat_timeout_seconds, 300)
        self.assertEqual(cfg.auxiliary_heartbeat_timeout_seconds, 120)
        self.assertEqual(cfg.head_activation_timeout_seconds, 120)
        self.assertEqual(cfg.command_timeout_seconds, 45)

    def test_legacy_editor_values_cannot_override_fixed_recovery_and_cadence(self):
        previous = house_configuration(bindings=self.bindings).to_dict()
        previous.update(heartbeat_timeout_seconds=60, auxiliary_heartbeat_timeout_seconds=30,
                        decision_interval_seconds=1, head_minimum_off_seconds=0)
        cfg = entry_configuration("/unused", {"policy": "house", "configuration": previous}, self.bindings)
        self.assertEqual(cfg.heartbeat_timeout_seconds, 300)
        self.assertEqual(cfg.auxiliary_heartbeat_timeout_seconds, 120)
        self.assertEqual(cfg.decision_interval_seconds, 90)
        self.assertEqual(cfg.head_minimum_off_seconds, 180)

    def test_setup_and_options_have_translations_for_only_their_actual_form(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/house_climate"
        strings = json.loads((root / "strings.json").read_text())
        self.assertEqual(strings, json.loads((root / "translations/en.json").read_text()))
        for section, step in (("config", "user"), ("options", "init")):
            self.assertEqual(set(strings[section]["step"]), {step})
            self.assertEqual(set(strings[section]["step"][step]["data"]), {"live"})
            self.assertIn("thermostat cards", strings[section]["step"][step]["description"])


if __name__ == "__main__":
    unittest.main()
