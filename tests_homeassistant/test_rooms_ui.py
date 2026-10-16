"""Exercise real HA selectors, room drafts, unit handling, and config-entry storage."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate import async_migrate_entry, async_setup_entry, async_unload_entry
from custom_components.house_climate.config_flow import HouseClimateConfigFlow, HouseClimateOptionsFlow
from custom_components.house_climate.configuration import validate_actuators
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.sensor import RoomTemperature
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures


class RoomUITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    async def flow(self):
        flow = HouseClimateConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        flow._async_current_entries = Mock(return_value=[])
        await flow.async_step_user()
        return flow

    async def test_rooms_saved_directly_without_a_toml_file(self):
        flow = await self.flow()
        form = await flow.async_step_add_room()
        values = form["data_schema"]({"name": "Reading corner", "head": "climate.test_corner",
                                      "sensors": ["sensor.test_temperature"]})
        result = await flow.async_step_room(values)
        self.assertEqual(result["type"], "menu")
        key = flow.room_id
        saved = await flow.async_step_finish({})
        self.assertEqual(saved["type"], "create_entry")
        self.assertNotIn("config_file", saved["data"])
        self.assertFalse(saved["data"]["live"])
        self.assertEqual(saved["data"]["configuration"]["rooms"][key]["name"], "Reading corner")
        form = await flow.async_step_room()
        renamed = form["data_schema"]({"name": "Library", "head": "climate.test_corner",
                                       "sensors": ["sensor.test_temperature"]})
        await flow.async_step_room(renamed)
        self.assertEqual(flow.room_id, key)
        self.assertEqual(flow.draft["rooms"][key]["name"], "Library")
        self.assertEqual(saved["data"]["configuration"]["rooms"][key]["name"], "Reading corner")

    async def test_metric_forms_convert_targets_and_deadbands_once(self):
        self.hass.config.units = METRIC_SYSTEM
        flow = await self.flow()
        form = await flow.async_step_add_room()
        values = form["data_schema"]({"name": "Studio", "head": "climate.test_studio",
                     "sensors": ["sensor.test_temperature"], "target_f": 21, "half_band_f": .5})
        result = await flow.async_step_room(values)
        self.assertEqual(result["type"], "menu")
        profile = flow.draft["rooms"][flow.room_id]["profile"]
        self.assertAlmostEqual(profile["target_f"], 69.8)
        self.assertAlmostEqual(profile["half_band_f"], .9)
        form = await flow.async_step_room()
        values = form["data_schema"]({})
        self.assertEqual(values["target_f"], 21)
        self.assertEqual(values["half_band_f"], .5)

    async def test_options_are_private_until_saved_and_removal_checks_references(self):
        original = configuration()
        entry = SimpleNamespace(data={"configuration": original, "live": False}, options={})
        flow = HouseClimateOptionsFlow()
        flow.hass = self.hass
        with patch.object(HouseClimateOptionsFlow, "config_entry", new_callable=PropertyMock, return_value=entry):
            result = await flow.async_step_init()
        self.assertEqual(result["type"], "menu")
        flow.room_id = "beta"
        result = await flow.async_step_remove_room({"confirm": True})
        self.assertEqual(result["errors"], {"base": "room_in_use"})
        self.assertIn("beta", flow.draft["rooms"])
        flow.draft["rooms"]["alpha"]["fans"] = []
        await flow.async_step_remove_room({"confirm": True})
        self.assertNotIn("beta", flow.draft["rooms"])
        self.assertIn("beta", original["rooms"])
        self.assertEqual(original["rooms"]["alpha"]["fans"][0]["strategy"], "transfer")

    async def test_schedule_and_fan_editor_validate_and_preserve_previous_draft(self):
        flow = await self.flow()
        flow._begin(configuration())
        flow.room_id = "gamma"
        form = await flow.async_step_add_schedule()
        values = form["data_schema"]({"name": "Overlap", "start": "04:00:00", "end": "06:00:00"})
        result = await flow.async_step_schedule(values)
        self.assertEqual(result["errors"], {"base": "invalid_config"})
        self.assertEqual(len(flow.draft["rooms"]["gamma"]["schedules"]), 1)
        values.update(start="05:00:00", end="08:00:00")
        self.assertEqual((await flow.async_step_schedule(values))["type"], "menu")
        form = await flow.async_step_add_fan()
        values = form["data_schema"]({"entity_id": "fan.test_airflow", "strategy": "cooling"})
        result = await flow.async_step_fan(values)
        self.assertEqual(result["errors"], {"base": "invalid_config"})

    async def test_ui_configuration_loads_without_files_and_exposes_named_room_sensors(self):
        values = {"configuration": configuration(), "live": False}
        entry = SimpleNamespace(entry_id="test", data=values, options={},
                                async_on_unload=self.stops.append,
                                add_update_listener=Mock(return_value=lambda: None))
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_setup_entry(self.hass, entry)
        self.runtimes.append(entry.runtime_data)
        sensor = RoomTemperature(entry, "alpha")
        self.assertEqual(sensor.name, "North alcove temperature")
        self.assertEqual(sensor.unique_id, "test_room_alpha")
        self.assertFalse(sensor.available)
        entry.runtime_data.status = {"event": "shadow", "rooms": {"alpha": {"temperature": 72, "sources": 2}}}
        self.assertEqual(sensor.native_value, 72)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.extra_state_attributes["independent_sources"], 2)
        self.assertTrue(await async_unload_entry(self.hass, entry))

    async def test_upgrade_preserves_old_file_and_live_helpers_until_user_saves(self):
        entry = SimpleNamespace(version=1, data={"config_file": "house_climate.toml", "live": True})
        self.hass.config_entries = Mock()
        self.assertTrue(await async_migrate_entry(self.hass, entry))
        self.hass.config_entries.async_update_entry.assert_called_once_with(entry, version=2)
        self.assertEqual(entry.data, {"config_file": "house_climate.toml", "live": True})

    async def test_import_to_editor_starts_in_shadow_and_keeps_current_helper_values(self):
        self.hass.states.async_set("input_number.hp_office_target", "73")
        flow = await self.flow()
        await flow.async_step_import_file({"config_file": "house_climate.toml"})
        self.assertEqual(flow.draft["rooms"]["office"]["profile"]["target_f"], 73)
        self.assertFalse(flow.live)

    async def test_virtual_room_climates_are_rejected_as_heads_and_auxiliary_heaters(self):
        registry = er.async_get(self.hass)
        virtual = registry.async_get_or_create(
            "climate", "house_climate", "guard_virtual_room",
            suggested_object_id="guard_virtual_room")
        flow = await self.flow()
        for actuator in ("head", "auxiliary"):
            candidate = configuration()
            if actuator == "head":
                candidate["heads"]["shared"] = virtual.entity_id
            else:
                candidate.update(heater_entity=virtual.entity_id, auxiliary_room="alpha")
                candidate["rooms"]["alpha"]["profile"] = {"target_f": 68}
            with self.subTest(actuator=actuator):
                config = Config.from_dict(candidate)
                with self.assertRaisesRegex(ValueError, "Select physical heat-pump and auxiliary heater entities"):
                    validate_actuators(self.hass, config)
                with self.assertRaisesRegex(ValueError, "Select physical heat-pump and auxiliary heater entities"):
                    flow.validate(candidate)

    async def test_physical_climates_remain_valid_equipment_targets(self):
        registry = er.async_get(self.hass)
        head = registry.async_get_or_create(
            "climate", "physical_integration", "guard_physical_head",
            suggested_object_id="guard_physical_head")
        heater = registry.async_get_or_create(
            "climate", "physical_integration", "guard_physical_heater",
            suggested_object_id="guard_physical_heater")
        candidate = configuration()
        candidate["heads"]["shared"] = head.entity_id
        candidate.update(heater_entity=heater.entity_id, auxiliary_room="alpha")
        candidate["rooms"]["alpha"]["profile"] = {"target_f": 68}
        flow = await self.flow()
        config = flow.validate(candidate)
        validate_actuators(self.hass, config)
        self.assertEqual(config.heads["shared"], head.entity_id)
        self.assertEqual(config.heater_entity, heater.entity_id)

    async def test_equipment_selectors_exclude_virtual_room_climates(self):
        registry = er.async_get(self.hass)
        virtual = registry.async_get_or_create(
            "climate", "house_climate", "guard_virtual_selector",
            suggested_object_id="guard_virtual_selector")
        physical = registry.async_get_or_create(
            "climate", "physical_integration", "guard_physical_selector",
            suggested_object_id="guard_physical_selector")
        flow = await self.flow()
        for form, field in ((await flow.async_step_add_room(), "head"),
                            (await flow.async_step_auxiliary(), "heater_entity")):
            with self.subTest(field=field):
                control = next(value for marker, value in form["data_schema"].schema.items()
                               if marker.schema == field)
                self.assertIn(virtual.entity_id, control.config["exclude_entities"])
                self.assertNotIn(physical.entity_id, control.config["exclude_entities"])
