"""Optional contact wiring preserves existing private maps and room identities."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest

from house_climate.bindings import BINDINGS_FILENAME, BindingsError, HouseBindings, example_bindings, load_bindings
from house_climate.config import Config
from house_climate.house_policy import house_configuration
from tests.test_rooms import configuration


class WindowBindingTests(unittest.TestCase):
    def setUp(self):
        self.example = example_bindings()
        self.raw = self.example.to_dict()

    def test_existing_private_maps_and_example_need_no_new_keys(self):
        self.assertEqual(HouseBindings.from_dict(self.raw), self.example)
        self.assertEqual(dict(self.example.windows), {})
        self.assertNotIn("windows", self.raw)
        raw = deepcopy(self.raw)
        raw["windows"] = {}
        self.assertEqual(HouseBindings.from_dict(raw).to_dict(), self.raw)
        self.assertEqual(json.loads(Path("bindings.example.json").read_text()), self.raw)

    def test_explicit_multi_room_contact_mapping_round_trips(self):
        raw = deepcopy(self.raw)
        raw["windows"] = {
            "office": ["binary_sensor.shared_opening", "binary_sensor.workspace_opening"],
            "entertainment": ["binary_sensor.shared_opening"],
        }
        original = deepcopy(raw)
        bindings = HouseBindings.from_dict(raw)
        self.assertEqual(bindings.windows["office"],
                         ("binary_sensor.shared_opening", "binary_sensor.workspace_opening"))
        self.assertEqual(bindings.to_dict(), original)
        self.assertEqual(raw, original)
        self.assertEqual(HouseBindings.from_dict(bindings.to_dict()), bindings)
        raw["windows"]["office"].clear()
        self.assertEqual(len(bindings.windows["office"]), 2)
        exported = bindings.to_dict()
        exported["windows"]["entertainment"].clear()
        self.assertEqual(bindings.windows["entertainment"], ("binary_sensor.shared_opening",))

    def test_direct_constructor_defensively_freezes_window_mapping(self):
        selected = {"guest": ["binary_sensor.visitor_opening"]}
        bindings = replace(self.example, windows=selected)
        selected["guest"].clear()
        selected["primary"] = ["binary_sensor.other_opening"]
        self.assertEqual(dict(bindings.windows), {"guest": ("binary_sensor.visitor_opening",)})
        with self.assertRaises(TypeError):
            bindings.windows["guest"] = ()
        with self.assertRaises(FrozenInstanceError):
            bindings.windows = {}

    def test_malformed_private_window_mapping_fails_closed(self):
        invalid = [None, [], True, "binary_sensor.opening",
                   {"unknown_room": ["binary_sensor.opening"]},
                   {"office": "binary_sensor.opening"},
                   {"office": ("binary_sensor.opening",)},
                   {"office": ["sensor.not_a_contact"]},
                   {"office": ["binary_sensor.Invalid"]},
                   {"office": [None]},
                   {"office": ["binary_sensor.opening", "binary_sensor.opening"]}]
        for windows in invalid:
            raw = deepcopy(self.raw)
            raw["windows"] = windows
            with self.subTest(windows=windows), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_optional_window_key_does_not_admit_rules_or_missing_required_wiring(self):
        for extra in ("priority", "window_priority_factor", "window_debounce_seconds"):
            raw = deepcopy(self.raw)
            raw["windows"] = {"office": ["binary_sensor.workspace_opening"]}
            raw[extra] = 1
            with self.subTest(extra=extra), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)
        raw = deepcopy(self.raw)
        raw["windows"] = {"office": []}
        raw.pop("heater")
        with self.assertRaises(BindingsError):
            HouseBindings.from_dict(raw)

    def test_optional_contacts_load_from_private_file(self):
        raw = deepcopy(self.raw)
        raw["windows"] = {"office": ["binary_sensor.workspace_opening"]}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, BINDINGS_FILENAME).write_text(json.dumps(raw))
            self.assertEqual(load_bindings(directory).windows["office"],
                             ("binary_sensor.workspace_opening",))

    def test_house_policy_maps_contacts_to_preserved_room_ids(self):
        previous = house_configuration(bindings=self.example).to_dict()
        office = previous["rooms"].pop("office")
        office["area_id"] = "workspace_area"
        previous["rooms"]["saved_workspace_uuid"] = office
        # Old stored contact selections cannot override current private wiring.
        office["windows"] = ["binary_sensor.obsolete_opening"]
        bindings = replace(self.example, windows={"office": ("binary_sensor.current_opening",)})
        fixed = house_configuration(previous, bindings)
        self.assertNotIn("office", fixed.zones)
        self.assertEqual(fixed.zones["saved_workspace_uuid"].windows,
                         ("binary_sensor.current_opening",))
        self.assertEqual(fixed.zones["saved_workspace_uuid"].area_id, "workspace_area")
        self.assertEqual(fixed.zones["entertainment"].windows, ())
        # Adding contacts leaves the fixed thermal and electrical rules intact.
        self.assertEqual((fixed.low_f, fixed.high_f), (66, 75))
        self.assertEqual(fixed.heater_cutoff_f, 70)
        fixed.validate(live=True)


class RoomContactConfigTests(unittest.TestCase):
    def test_existing_generic_schema_and_example_keep_empty_contacts(self):
        cfg = Config.from_dict(configuration())
        self.assertTrue(all(room.windows == () for room in cfg.zones.values()))
        self.assertTrue(all("windows" not in room for room in cfg.to_dict()["rooms"].values()))
        example = Config.load("config.example.toml")
        self.assertTrue(all(room.windows == () for room in example.zones.values()))
        legacy = Config.load("tests/fixtures/legacy.toml")
        self.assertTrue(legacy.legacy)
        self.assertTrue(all(room.windows == () for room in legacy.zones.values()))

    def test_generic_config_contacts_round_trip_and_allow_explicit_shared_opening(self):
        raw = configuration()
        raw["rooms"]["alpha"]["windows"] = ["binary_sensor.common_opening", "binary_sensor.north_opening"]
        raw["rooms"]["beta"]["windows"] = ["binary_sensor.common_opening"]
        before = deepcopy(raw)
        cfg = Config.from_dict(raw)
        self.assertEqual(raw, before)
        self.assertEqual(cfg.zones["alpha"].windows,
                         ("binary_sensor.common_opening", "binary_sensor.north_opening"))
        exported = cfg.to_dict()
        self.assertEqual(exported["rooms"]["beta"]["windows"], ["binary_sensor.common_opening"])
        reloaded = Config.from_dict(exported)
        self.assertEqual(reloaded.to_dict(), exported)
        self.assertEqual({key: room.windows for key, room in reloaded.zones.items()},
                         {key: room.windows for key, room in cfg.zones.items()})

    def test_generic_malformed_or_duplicate_contacts_are_rejected(self):
        for windows in (None, {}, True, "binary_sensor.opening", [None], [[]],
                        ["sensor.not_a_contact"], ["binary_sensor.Invalid"],
                        ["binary_sensor.opening", "binary_sensor.opening"]):
            raw = configuration()
            raw["rooms"]["alpha"]["windows"] = windows
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_direct_room_constructor_freezes_contacts(self):
        cfg = Config.from_dict(configuration())
        contacts = ["binary_sensor.direct_opening"]
        room = replace(cfg.zones["alpha"], windows=contacts)
        contacts.clear()
        self.assertEqual(room.windows, ("binary_sensor.direct_opening",))
        with self.assertRaises(FrozenInstanceError):
            room.windows = ()
        cfg.zones["alpha"] = room
        cfg.validate()


if __name__ == "__main__":
    unittest.main()
