"""Native purifier policy edits are atomic and preserve unwired preferences."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from custom_components.house_climate.configuration import entry_configuration
from tests_homeassistant import test_config_navigation as navigation


class PurifierEditorTests(unittest.IsolatedAsyncioTestCase):
    handle = navigation.FixedFlowTests.handle
    asyncSetUp = navigation.FixedFlowTests.asyncSetUp
    asyncTearDown = navigation.FixedFlowTests.asyncTearDown
    start = navigation.FixedFlowTests.start

    async def test_room_overrides_save_reload_edit_and_clear_independently(self):
        entry = SimpleNamespace(data={"policy": "house", "live": False}, options={})
        for selected, expected in (
            ({"room_overrides": {"office": 70, "entertainment": 70}}, {"office": 70, "entertainment": 70}),
            ({"minimum_percentage": 30}, {"office": 70, "entertainment": 70}),
            ({"room_overrides": {"office": 50}}, {"office": 50}),
            ({"room_overrides": {}}, {}),
        ):
            manager, form, _ = await self.start(options=True, entry=entry)
            saved = await manager.async_configure(form["flow_id"], {"live": False, "purifiers": selected})
            self.assertEqual(saved["type"], "create_entry")
            entry.options = saved["data"]
            cfg = entry_configuration(self.directory.name, {**entry.data, **entry.options})
            self.assertEqual(cfg.purifiers.room_overrides, expected)
            for fan in cfg.fan_controls.values():
                self.assertEqual(fan.speed_override, expected.get(fan.room))
            self.assertEqual(cfg.fallback.fan_percentage, 40)
        self.assertEqual(self.calls, [])

    async def test_setup_exposes_translated_defaults_and_saves_them(self):
        manager, form, _ = await self.start()
        self.assertEqual(form["data_schema"]({})["purifiers"],
                         {"minimum_percentage": 20, "living_percentage": 100})
        strings_root = Path(__file__).resolve().parents[1] / "custom_components/house_climate"
        strings = json.loads((strings_root / "strings.json").read_text())
        self.assertEqual(strings, json.loads((strings_root / "translations/en.json").read_text()))
        for section, step in (("config", "user"), ("options", "init")):
            self.assertEqual(strings[section]["step"][step]["data"]["purifiers"],
                             "Purifier speeds and room overrides")
            self.assertIn("auxiliary heater", strings[section]["step"][step]["data_description"]["purifiers"])
        saved = await manager.async_configure(form["flow_id"], {"live": False})
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"]["policy_settings"]["purifiers"],
                         {"minimum_percentage": 20, "living_percentage": 100})
        cfg = entry_configuration(self.directory.name, saved["data"])
        self.assertTrue(all(fan.minimum_percentage == 20 for fan in cfg.fan_controls.values()))
        self.assertEqual(self.calls, [])

    async def test_partial_options_save_retains_unbound_living_setting_and_other_policy(self):
        entry = SimpleNamespace(data={"policy": "house", "live": False}, options={
            "policy_settings": {"purifiers": {"minimum_percentage": 25, "living_percentage": 75},
                                "fallback": {"fan_percentage": 45}}})
        previous = deepcopy(entry.options)
        manager, form, _ = await self.start(options=True, entry=entry)
        self.assertEqual(form["data_schema"]({})["purifiers"], previous["policy_settings"]["purifiers"])
        saved = await manager.async_configure(form["flow_id"], {
            "live": False, "purifiers": {"minimum_percentage": 30}})
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"]["policy_settings"]["purifiers"],
                         {"minimum_percentage": 30, "living_percentage": 75})
        cfg = entry_configuration(self.directory.name, {**entry.data, **saved["data"]})
        self.assertTrue(all(fan.minimum_percentage == 30 for fan in cfg.fan_controls.values()))
        self.assertEqual(cfg.purifiers.living_percentage, 75)
        self.assertEqual(cfg.fallback.fan_percentage, 45)
        self.assertEqual(cfg.zones["primary"].schedules[0].profile.fan_percentage, 60)
        self.assertEqual(entry.options, previous)
        self.assertEqual(self.calls, [])

    async def test_invalid_purifier_edits_do_not_save_other_changes(self):
        for selected in ({"minimum_percentage": True}, {"minimum_percentage": "30"},
                         {"minimum_percentage": 101}, {"minimum_percentage": float("nan")},
                         {"living_percentage": -1}, {"minimum_percentage": 50, "living_percentage": 40},
                         {"fan_entity": "fan.fixture_extra"},
                         {"room_overrides": {"office": True}}, {"room_overrides": {"office": 101}},
                         {"room_overrides": {"office": None}}, {"room_overrides": {"unknown": 70}}):
            with self.subTest(selected=selected):
                entry = SimpleNamespace(data={"policy": "house", "live": False}, options={})
                manager, form, _ = await self.start(options=True, entry=entry)
                result = await manager.async_configure(form["flow_id"], {
                    "live": True, "purifiers": selected, "fallback": {"fan_percentage": 50}})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": "invalid_config"})
                self.assertEqual(entry.options, {})
                self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
