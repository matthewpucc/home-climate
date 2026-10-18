"""Exercise real HA selectors, room drafts, unit handling, and config-entry storage."""
import unittest
import probatio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate import async_migrate_entry, async_setup_entry, async_unload_entry
from custom_components.house_climate.config_flow import HouseClimateConfigFlow, HouseClimateOptionsFlow
from custom_components.house_climate.configuration import validate_actuators
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.sensors import probe_value
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

    async def test_system_settings_expose_independent_cadence_in_seconds(self):
        self.hass.config.units = METRIC_SYSTEM
        flow = await self.flow()
        flow._begin(configuration())
        form = await flow.async_step_system()
        controls = {marker.schema: control for marker, control in form["data_schema"].schema.items()}
        expected = {
            "decision_interval_seconds": (60, 120, 90),
            "head_minimum_run_seconds": (0, 3600, 300),
            "head_minimum_off_seconds": (0, 3600, 180),
            "head_activation_timeout_seconds": (1, 300, 120),
            "poll_seconds": (1, 30, 15),
        }
        defaults = form["data_schema"]({})
        for name, (minimum, maximum, default) in expected.items():
            with self.subTest(name=name):
                self.assertEqual(controls[name].config["min"], minimum)
                self.assertEqual(controls[name].config["max"], maximum)
                self.assertEqual(controls[name].config["unit_of_measurement"], "s")
                self.assertEqual(defaults[name], default)
        values = form["data_schema"]({
            "decision_interval_seconds": 120, "head_minimum_run_seconds": 600,
            "head_minimum_off_seconds": 240, "head_activation_timeout_seconds": 150, "poll_seconds": 10})
        result = await flow.async_step_system(values)
        self.assertEqual(result["type"], "menu")
        self.assertEqual(flow.draft["decision_interval_seconds"], 120)
        self.assertEqual(flow.draft["head_minimum_run_seconds"], 600)
        self.assertEqual(flow.draft["head_minimum_off_seconds"], 240)
        self.assertEqual(flow.draft["head_activation_timeout_seconds"], 150)
        self.assertEqual(flow.draft["poll_seconds"], 10)
        self.assertAlmostEqual(flow.draft["low_f"], 66, places=3)
        self.assertAlmostEqual(flow.draft["high_f"], 75, places=3)

    async def test_system_settings_reject_cadence_outside_recommended_range(self):
        flow = await self.flow()
        flow._begin(configuration())
        form = await flow.async_step_system()
        for name, value in (("decision_interval_seconds", 59), ("decision_interval_seconds", 121),
                            ("head_minimum_run_seconds", -1), ("head_minimum_off_seconds", -1),
                            ("poll_seconds", 31), ("head_activation_timeout_seconds", 0),
                            ("head_activation_timeout_seconds", 301)):
            with self.subTest(name=name, value=value), self.assertRaises(probatio.Invalid):
                form["data_schema"]({name: value})

    async def test_system_call_offsets_convert_celsius_differences_without_absolute_offset(self):
        self.hass.config.units = METRIC_SYSTEM
        flow = await self.flow()
        flow._begin(configuration())
        form = await flow.async_step_system()
        controls = {marker.schema: control for marker, control in form["data_schema"].schema.items()}
        defaults = form["data_schema"]({})
        for name in ("head_heat_call_offset_f", "head_cool_call_offset_f"):
            with self.subTest(name=name):
                self.assertAlmostEqual(defaults[name], 5 * 5 / 9, places=3)
                self.assertEqual(controls[name].config["min"], 0)
                self.assertAlmostEqual(controls[name].config["max"], 20 * 5 / 9, places=3)
                self.assertEqual(controls[name].config["step"], .1)
                self.assertEqual(controls[name].config["unit_of_measurement"], "°C")
        values = form["data_schema"]({"head_heat_call_offset_f": 3, "head_cool_call_offset_f": 2})
        self.assertEqual((await flow.async_step_system(values))["type"], "menu")
        self.assertAlmostEqual(flow.draft["head_heat_call_offset_f"], 5.4)
        self.assertAlmostEqual(flow.draft["head_cool_call_offset_f"], 3.6)
        # Reopening and saving uses deltas again, without adding 32 or scaling twice.
        reopened = await flow.async_step_system()
        saved = reopened["data_schema"]({})
        self.assertEqual(saved["head_heat_call_offset_f"], 3)
        self.assertEqual(saved["head_cool_call_offset_f"], 2)
        self.assertEqual((await flow.async_step_system(saved))["type"], "menu")
        self.assertAlmostEqual(flow.draft["head_heat_call_offset_f"], 5.4)
        self.assertAlmostEqual(flow.draft["head_cool_call_offset_f"], 3.6)
        self.assertEqual(flow.draft["rooms"]["alpha"]["profile"]["target_f"], 72)

    async def test_system_call_offsets_use_fahrenheit_differences_and_allow_zero(self):
        flow = await self.flow()
        flow._begin(configuration())
        form = await flow.async_step_system()
        defaults = form["data_schema"]({})
        self.assertEqual(defaults["head_heat_call_offset_f"], 5)
        self.assertEqual(defaults["head_cool_call_offset_f"], 5)
        values = form["data_schema"]({"head_heat_call_offset_f": 0, "head_cool_call_offset_f": 20})
        self.assertEqual((await flow.async_step_system(values))["type"], "menu")
        self.assertEqual(flow.draft["head_heat_call_offset_f"], 0)
        self.assertEqual(flow.draft["head_cool_call_offset_f"], 20)
        self.assertEqual(flow.draft["rooms"]["alpha"]["profile"]["target_f"], 72)
        for name, value in (("head_heat_call_offset_f", -.1), ("head_cool_call_offset_f", 20.1)):
            with self.subTest(name=name), self.assertRaises(probatio.Invalid):
                form["data_schema"]({name: value})

    async def test_room_selects_report_on_change_probes_individually(self):
        flow = await self.flow()
        form = await flow.async_step_add_room()
        values = form["data_schema"]({
            "name": "Study", "head": "climate.study",
            "sensors": ["sensor.study_periodic", "sensor.study_on_change"],
            "report_on_change_sensors": ["sensor.study_on_change"],
        })
        self.assertEqual((await flow.async_step_room(values))["type"], "menu")
        room = Config.from_dict(flow.draft).zones[flow.room_id]
        self.assertEqual({probe.entity_id: probe.report_on_change for probe in room.probes},
                         {"sensor.study_periodic": False, "sensor.study_on_change": True})
        reopened = await flow.async_step_room()
        self.assertEqual(reopened["data_schema"]({})["report_on_change_sensors"],
                         ["sensor.study_on_change"])

    async def test_report_on_change_selection_must_belong_to_the_room(self):
        flow = await self.flow()
        flow._begin(configuration())
        flow.room_id = "alpha"
        original = deepcopy(flow.draft)
        form = await flow.async_step_room()
        values = form["data_schema"]({
            "name": "Changed name", "report_on_change_sensors": ["sensor.not_selected"],
        })
        result = await flow.async_step_room(values)
        self.assertEqual(result["errors"], {"base": "invalid_config"})
        self.assertIn("Also select each sensor", result["description_placeholders"]["detail"])
        self.assertEqual(flow.draft, original)

    async def test_clearing_report_on_change_restores_periodic_age_checks_and_keeps_calibration(self):
        flow = await self.flow()
        raw = configuration()
        raw["rooms"]["alpha"]["sensors"][0].update(
            report_on_change=True, source="calibrated_probe", attribute="temperature",
            unit="°F", offset_f=.5)
        flow._begin(raw)
        flow.room_id = "alpha"
        before = Config.from_dict(flow.draft).zones["alpha"].probes[0]
        now = datetime.now(timezone.utc)
        old_states = {before.entity_id: {
            "state": "70", "last_reported": (now - timedelta(hours=1)).isoformat(),
            "attributes": {"temperature": 70, "unit_of_measurement": "°F"}}}
        self.assertEqual(probe_value(before, old_states, now, 900), 70.5)
        form = await flow.async_step_room()
        values = form["data_schema"]({"report_on_change_sensors": []})
        self.assertEqual((await flow.async_step_room(values))["type"], "menu")
        after = Config.from_dict(flow.draft).zones["alpha"].probes[0]
        self.assertFalse(after.report_on_change)
        self.assertEqual((after.source, after.attribute, after.unit, after.offset_f),
                         ("calibrated_probe", "temperature", "°F", .5))
        with self.assertRaisesRegex(ValueError, "stale"):
            probe_value(after, old_states, now, 900)

    async def test_rename_and_older_input_preserve_probe_reporting_and_metadata(self):
        flow = await self.flow()
        raw = configuration()
        raw["rooms"]["alpha"]["sensors"][0].update(
            report_on_change=True, source="shared_physical_probe", attribute="temperature",
            unit="°C", offset_f=-.75)
        flow._begin(raw)
        flow.room_id = "alpha"
        original_probes = deepcopy(flow.draft["rooms"]["alpha"]["sensors"])
        form = await flow.async_step_room()
        values = form["data_schema"]({"name": "Renamed room"})
        values.pop("report_on_change_sensors")
        self.assertEqual((await flow.async_step_room(values))["type"], "menu")
        self.assertEqual(flow.draft["rooms"]["alpha"]["sensors"], original_probes)
        reopened = await flow.async_step_room()
        self.assertEqual(reopened["data_schema"]({})["report_on_change_sensors"],
                         ["sensor.test_a"])

    async def test_room_can_add_new_report_on_change_probe_in_the_same_edit(self):
        flow = await self.flow()
        flow._begin(configuration())
        flow.room_id = "alpha"
        form = await flow.async_step_room()
        values = form["data_schema"]({
            "sensors": ["sensor.test_a", "sensor.new_change_probe"],
            "report_on_change_sensors": ["sensor.new_change_probe"],
        })
        self.assertEqual((await flow.async_step_room(values))["type"], "menu")
        probes = Config.from_dict(flow.draft).zones["alpha"].probes
        self.assertEqual({probe.entity_id: probe.report_on_change for probe in probes},
                         {"sensor.test_a": False, "sensor.new_change_probe": True})
