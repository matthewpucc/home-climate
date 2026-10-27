"""HA saves validated protection settings atomically without editing comfort."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import probatio
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import config_validation as cv

from custom_components.house_climate.comfort import RoomComfort
from custom_components.house_climate.configuration import entry_configuration
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.config import Probe
from custom_components.house_climate.controller.house_policy import house_configuration
from custom_components.house_climate.controller.protection import ProtectionTemperatures
from tests_homeassistant import test_config_navigation as flows
from tests_homeassistant import test_native as fixtures


class ProtectionOptionsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flows.FixedFlowTests.asyncSetUp
    asyncTearDown = flows.FixedFlowTests.asyncTearDown
    start = flows.FixedFlowTests.start
    handle = fixtures.NativeTests.handle

    async def test_real_form_renders_bounded_fahrenheit_settings_and_configurable_ties(self):
        manager, form, _ = await self.start()
        fields = {item["name"]: item for item in probatio.to_field_list(
            form["data_schema"], custom_serializer=cv.custom_serializer)}
        self.assertEqual(fields["normal_heating_floor_f"]["selector"]["number"]["min"], 40)
        self.assertEqual(fields["normal_heating_floor_f"]["selector"]["number"]["max"], 70)
        self.assertEqual(fields["emergency_cooling_at_f"]["selector"]["number"]["unit_of_measurement"], "°F")
        self.assertEqual(fields["qualification_seconds"]["selector"]["number"]["unit_of_measurement"], "s")
        self.assertEqual(fields["boundary_tie_mode"]["default"], "heat")
        saved = await manager.async_configure(form["flow_id"], {
            "live": False, "normal_heating_floor_f": 58.5, "freezing_heating_floor_f": 66,
            "emergency_cooling_at_f": 81, "boundary_tie_mode": "cool", "unknown_outdoor_tie_mode": "cool",
        })
        protection = saved["data"]["protection"]
        self.assertEqual(protection["normal_heating_floor_f"], 58.5)
        self.assertEqual(protection["boundary_tie_mode"], "cool")
        cfg = entry_configuration(self.directory.name, saved["data"])
        self.assertEqual(cfg.protection, ProtectionTemperatures.from_dict(protection))
        self.assertEqual((cfg.low_f, cfg.high_f), (66, 75))
        self.assertEqual(self.calls, [])

    async def test_options_use_current_saved_protection_and_preserve_other_values_on_save(self):
        custom = ProtectionTemperatures(normal_heating_floor_f=61, freezing_heating_floor_f=68,
                                        boundary_tie_mode="cool", qualification_seconds=120).to_dict()
        entry = SimpleNamespace(data={"policy": "house", "live": False,
                                      "protection": ProtectionTemperatures().to_dict()},
                                options={"live": True, "protection": custom})
        original = deepcopy((entry.data, entry.options))
        manager, form, _ = await self.start(options=True, entry=entry)
        values = form["data_schema"]({})
        self.assertEqual(values["normal_heating_floor_f"], 61)
        self.assertEqual(values["boundary_tie_mode"], "cool")
        saved = await manager.async_configure(form["flow_id"], {"live": False, "normal_heating_floor_f": 62})
        self.assertEqual(saved["data"]["protection"], {**custom, "normal_heating_floor_f": 62})
        self.assertEqual((entry.data, entry.options), original)
        self.assertEqual(self.calls, [])

    async def test_invalid_cross_field_combination_is_not_partially_saved(self):
        entry = SimpleNamespace(data={"policy": "house", "live": False}, options={})
        original = deepcopy((entry.data, entry.options))
        manager, form, _ = await self.start(options=True, entry=entry)
        result = await manager.async_configure(form["flow_id"], {
            "live": True, "normal_heating_floor_f": 68, "freezing_heating_floor_f": 60})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_config"})
        self.assertEqual((entry.data, entry.options), original)
        self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])
        saved = await manager.async_configure(form["flow_id"], {
            "live": False, "normal_heating_floor_f": 61, "freezing_heating_floor_f": 66})
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"]["protection"]["normal_heating_floor_f"], 61)
        self.assertEqual(saved["data"]["protection"]["freezing_heating_floor_f"], 66)
        self.assertEqual(len(manager.completed), 1)

    async def test_number_selectors_do_not_coerce_boolean_or_string_safety_values(self):
        for name, value in (("freezing_outdoor_below_f", False), ("qualification_seconds", True),
                            ("release_hysteresis_f", True), ("normal_heating_floor_f", "60"),
                            ("boundary_tie_mode", "auto"), ("emergency_cooling_at_f", 120)):
            manager, form, _ = await self.start()
            with self.subTest(name=name, value=value), self.assertRaises(InvalidData):
                await manager.async_configure(form["flow_id"], {"live": False, name: value})
            self.assertEqual(manager.completed, [])
            self.assertEqual(self.calls, [])

    async def test_protection_and_outdoor_wiring_do_not_erase_dashboard_holds(self):
        cfg = house_configuration(bindings=example_bindings())
        first = RoomComfort(self.hass, cfg, "protection_hold", lambda: None)
        second = None
        try:
            await first.async_load()
            await first.async_set_target("office", 71)
            old = first.effective("office")
            self.assertTrue(old["hold"])
            changed = replace(cfg, protection=ProtectionTemperatures(normal_heating_floor_f=62,
                freezing_heating_floor_f=68, emergency_cooling_at_f=82),
                outdoor_probes=(Probe("weather.fixture_current", "fixture_provider"),))
            second = RoomComfort(self.hass, changed, "protection_hold", lambda: None)
            await second.async_load()
            self.assertEqual(second._fingerprint("office"), first._fingerprint("office"))
            self.assertTrue(second.effective("office")["hold"])
            self.assertEqual(second.effective("office")["target_f"], 71)
            self.assertEqual(second.bounds("office"), (66, 75))
            self.assertEqual(self.calls, [])
        finally:
            await first.async_close()
            if second:
                await second.async_close()

    async def test_every_visible_setting_has_english_label_and_help_in_both_flows(self):
        root = Path("custom_components/house_climate")
        strings = json.loads((root / "strings.json").read_text())
        self.assertEqual(strings, json.loads((root / "translations/en.json").read_text()))
        names = ProtectionTemperatures().to_dict()
        for section, step in (("config", "user"), ("options", "init")):
            form = strings[section]["step"][step]
            for name in names:
                self.assertTrue(form["data"][name])
                self.assertTrue(form["data_description"][name])

    async def test_explicit_bad_stored_protection_is_not_silently_replaced_by_defaults(self):
        for protection in (None, False, "defaults", {"normal_heating_floor_f": 90}):
            with self.subTest(protection=protection), self.assertRaises(ValueError):
                entry_configuration(self.directory.name, {
                    "policy": "house", "live": False, "protection": protection})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
