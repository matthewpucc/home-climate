"""Private wiring is strict, immutable, and cannot turn into a rule editor."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest

from house_climate.bindings import (
    BINDINGS_FILENAME, BindingsError, HouseBindings, PowerBindings, example_bindings, load_bindings,
)
from house_climate.house_policy import house_configuration


class PrivateBindingsTests(unittest.TestCase):
    def setUp(self):
        self.example = example_bindings()
        self.raw = self.example.to_dict()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / BINDINGS_FILENAME

    def test_synthetic_example_is_complete_and_matches_public_example_file(self):
        public = json.loads(Path("bindings.example.json").read_text())
        self.assertEqual(HouseBindings.from_dict(public), self.example)
        self.assertEqual(self.example.power.heater, self.example.heater)
        self.assertEqual(len(self.example.power.entity_ids), 9)
        self.assertNotIn("heater", self.raw["power"])
        house_configuration(bindings=self.example).validate(live=True)

    def test_no_implicit_device_fallback_in_policy_or_file_loader(self):
        with self.assertRaises(BindingsError):
            house_configuration()
        with self.assertRaisesRegex(BindingsError, BINDINGS_FILENAME):
            load_bindings(self.root)

    def test_loading_private_file_returns_valid_immutable_wiring(self):
        self.path.write_text(json.dumps(self.raw))
        first = load_bindings(self.root)
        self.assertEqual(first, self.example)
        changed = deepcopy(self.raw)
        changed["heads"]["primary"] = "climate.replaced_sleep_head"
        self.path.write_text(json.dumps(changed))
        second = load_bindings(self.root)
        self.assertNotEqual(first.heads["primary"], second.heads["primary"])
        self.assertEqual(first, self.example)

    def test_operating_rules_are_rejected_at_every_level(self):
        for section, key, value in (
                (None, "high_f", 90), (None, "schedules", []),
                (None, "heartbeat_timeout_seconds", 900),
                ("power", "current_limit_a", 20), ("power", "battery_restart_percent", 0),
                ("rooms", "priority", 10), ("heads", "off_settle_seconds", 1)):
            raw = deepcopy(self.raw)
            (raw if section is None else raw[section])[key] = value
            with self.subTest(section=section, key=key), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_missing_roles_and_wrong_domains_fail_closed(self):
        for section, role in (("heads", "primary"), ("rooms", "living"),
                              ("fans", "entertainment"), ("power", "heater_switch")):
            raw = deepcopy(self.raw)
            raw[section].pop(role)
            with self.subTest(section=section), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)
        for section, role, value in (
                ("heads", "primary", "sensor.wrong_domain"),
                ("fans", "guest", "climate.wrong_domain"),
                ("power", "heater_switch", "climate.wrong_domain"),
                ("power", "circuit_power", "switch.wrong_domain"),
                ("rooms", "office", ["climate.wrong_domain"])):
            raw = deepcopy(self.raw)
            raw[section][role] = value
            with self.subTest(section=section, role=role), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_duplicate_devices_and_reused_room_probes_are_rejected(self):
        for kind in ("head", "heater", "fan", "power", "within_room", "between_rooms"):
            raw = deepcopy(self.raw)
            if kind == "head":
                raw["heads"]["primary"] = raw["heads"]["guest"]
            elif kind == "heater":
                raw["heater"] = raw["heads"]["bonus"]
            elif kind == "fan":
                raw["fans"]["primary"] = raw["fans"]["guest"]
            elif kind == "power":
                raw["power"]["input_power"] = raw["power"]["output_power"]
            elif kind == "within_room":
                raw["rooms"]["office"].append(raw["rooms"]["office"][0])
            else:
                raw["rooms"]["office"].append(raw["rooms"]["guest"][0])
            with self.subTest(kind=kind), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_direct_constructors_take_defensive_copies_and_remain_immutable(self):
        power = PowerBindings(heater=self.raw["heater"], **self.raw["power"])
        direct = HouseBindings(self.raw["heads"], self.raw["rooms"], self.raw["fans"],
                               self.raw["heater"], power)
        self.raw["heads"]["primary"] = "climate.changed"
        self.raw["rooms"]["living"].clear()
        self.raw["fans"]["guest"] = "fan.changed"
        self.assertEqual(direct, self.example)
        with self.assertRaises(TypeError):
            direct.heads["primary"] = "climate.changed"
        with self.assertRaises(FrozenInstanceError):
            direct.heater = "climate.changed"
        with self.assertRaises(FrozenInstanceError):
            power.battery = "sensor.changed"
        self.assertIsInstance(direct.rooms["living"], tuple)

    def test_direct_constructors_cannot_bypass_validation_or_share_the_wrong_heater(self):
        with self.assertRaises(BindingsError):
            replace(self.example.power, heater="switch.not_a_climate")
        with self.assertRaises(BindingsError):
            replace(self.example, heater="climate.different_auxiliary_heater")
        with self.assertRaises(BindingsError):
            replace(self.example, heads={"bonus": "climate.only_one"})

    def test_bad_json_duplicate_keys_and_non_utf8_never_use_example_wiring(self):
        for contents in (b"not json", b"[]", b"{}", b'\xff', b'{"heads": {}, "heads": {}}'):
            self.path.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaises(BindingsError):
                load_bindings(self.root)

    def test_private_file_cannot_resolve_outside_config_directory(self):
        config = self.root / "config"
        config.mkdir()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.raw))
        (config / BINDINGS_FILENAME).symlink_to(outside)
        with self.assertRaises(BindingsError):
            load_bindings(config)


if __name__ == "__main__":
    unittest.main()
