"""Fixed house loading preserves dashboard identities and physical-target guards."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.helpers import entity_registry as er

from custom_components.house_climate import async_setup_entry, async_unload_entry
from custom_components.house_climate.climate import RoomClimate
from custom_components.house_climate.configuration import entry_configuration, validate_actuators
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.house_policy import house_configuration
from custom_components.house_climate.controller.bindings import BINDINGS_FILENAME, example_bindings
from custom_components.house_climate.sensor import RoomTemperature
from tests.test_rooms import configuration, states
from tests_homeassistant import test_native as fixtures


class FixedRoomUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.bindings = example_bindings()
        Path(self.directory.name, BINDINGS_FILENAME).write_text(json.dumps(self.bindings.to_dict()))
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    async def test_house_setup_needs_no_toml_or_room_editor_and_starts_disabled(self):
        cfg = house_configuration(bindings=self.bindings)
        for entity, value in states(cfg).items():
            self.hass.states.async_set(entity, value["state"], value.get("attributes", {}))
        entry = SimpleNamespace(entry_id="fixed_house", data={"policy": "house", "live": False},
                                options={}, async_on_unload=self.stops.append,
                                add_update_listener=Mock(return_value=lambda: None))
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_setup_entry(self.hass, entry)
        self.runtimes.append(entry.runtime_data)
        self.assertEqual(entry.runtime_data.config.heads, cfg.heads)
        self.assertIsNone(entry.runtime_data.thread)
        sensor = RoomTemperature(entry, "primary")
        self.assertEqual(sensor.unique_id, "fixed_house_room_primary")
        card = RoomClimate(entry, "primary")
        self.assertEqual(card.unique_id, "fixed_house_climate_primary")
        self.assertFalse(entry.runtime_data.live)
        self.assertTrue(await async_unload_entry(self.hass, entry))
        self.assertEqual(self.calls, [])

    async def test_old_room_ids_and_area_placement_survive_fixed_policy_loading(self):
        previous = house_configuration(bindings=self.bindings).to_dict()
        room = previous["rooms"].pop("primary")
        room["area_id"] = "upstairs_bedroom"
        previous["rooms"]["saved_bedroom_uuid"] = room
        before = deepcopy(previous)
        cfg = entry_configuration(self.directory.name, {
            "policy": "house", "configuration": previous, "live": False})
        self.assertEqual(previous, before)
        self.assertNotIn("primary", cfg.zones)
        self.assertEqual(cfg.zones["saved_bedroom_uuid"].area_id, "upstairs_bedroom")
        entry = SimpleNamespace(entry_id="existing_entry", runtime_data=SimpleNamespace(config=cfg))
        self.assertEqual(RoomClimate(entry, "saved_bedroom_uuid").unique_id,
                         "existing_entry_climate_saved_bedroom_uuid")
        self.assertEqual(RoomTemperature(entry, "saved_bedroom_uuid").unique_id,
                         "existing_entry_room_saved_bedroom_uuid")

    async def test_house_marker_ignores_obsolete_file_and_unapproved_old_thresholds(self):
        previous = house_configuration(bindings=self.bindings).to_dict()
        previous.update(low_f=55, high_f=85, heater_high_setpoint_f=110)
        cfg = entry_configuration(self.directory.name, {
            "policy": "house", "config_file": "../not-a-file.toml",
            "configuration": previous, "live": True})
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))
        self.assertEqual(cfg.heater_high_setpoint_f, 86)
        cfg.validate(live=True)

    async def test_legacy_parser_remains_available_for_migration_and_standalone_users(self):
        raw = configuration()
        cfg = entry_configuration(self.directory.name, {"configuration": raw, "live": False})
        self.assertEqual(set(cfg.zones), {"alpha", "beta", "gamma"})
        self.assertEqual(cfg.zones["alpha"].name, "North alcove")

    async def test_live_permission_is_never_inferred_from_a_string(self):
        with self.assertRaisesRegex(ValueError, "boolean"):
            entry_configuration(self.directory.name, {"policy": "house", "live": "false"})

    async def test_room_card_entities_cannot_be_selected_as_physical_equipment(self):
        registry = er.async_get(self.hass)
        virtual = registry.async_get_or_create(
            "climate", "house_climate", "guard_virtual_room", suggested_object_id="guard_virtual_room")
        raw = configuration()
        for auxiliary in (False, True):
            candidate = deepcopy(raw)
            if auxiliary:
                candidate.update(heater_entity=virtual.entity_id, auxiliary_room="alpha")
                candidate["rooms"]["alpha"]["profile"] = {"target_f": 68}
            else:
                candidate["heads"]["shared"] = virtual.entity_id
            with self.subTest(auxiliary=auxiliary), self.assertRaisesRegex(ValueError, "physical"):
                validate_actuators(self.hass, Config.from_dict(candidate))


if __name__ == "__main__":
    unittest.main()
