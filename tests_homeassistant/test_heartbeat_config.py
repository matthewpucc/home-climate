"""HA exposes and saves native heartbeat limits without applying Back drafts."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from custom_components.house_climate.controller.config import Config
from tests.test_rooms import configuration
from tests_homeassistant.test_config_navigation import ConfigNavigationTests as Navigation


class HeartbeatOptionsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = Navigation.asyncSetUp
    asyncTearDown = Navigation.asyncTearDown
    handle = Navigation.handle
    start = Navigation.start
    submit = Navigation.submit
    menu = Navigation.menu
    shown_values = Navigation.shown_values
    fields = Navigation.fields
    suggested = Navigation.suggested
    back = Navigation.back
    continue_shown = Navigation.continue_shown

    async def test_system_settings_show_existing_config_defaults_and_seconds(self):
        manager, flow_id, _ = await self.start(configuration(), options=True)
        form = await self.menu(manager, flow_id, "system")
        values = form["data_schema"]({})
        self.assertEqual(values["heartbeat_timeout_seconds"], 300)
        self.assertEqual(values["auxiliary_heartbeat_timeout_seconds"], 120)
        schema = {marker.schema: validator for marker, validator in form["data_schema"].schema.items()}
        self.assertEqual(schema["heartbeat_timeout_seconds"].config["min"], 60)
        self.assertEqual(schema["heartbeat_timeout_seconds"].config["max"], 900)
        self.assertEqual(schema["heartbeat_timeout_seconds"].config["unit_of_measurement"], "s")
        self.assertEqual(schema["auxiliary_heartbeat_timeout_seconds"].config["min"], 30)
        self.assertEqual(self.calls, [])

    async def test_options_save_explicit_windows_only_after_review(self):
        raw = configuration()
        original = deepcopy(raw)
        manager, flow_id, entry = await self.start(raw, options=True)
        await self.menu(manager, flow_id, "system")
        result = await self.submit(manager, flow_id, heartbeat_timeout_seconds=480,
                                   auxiliary_heartbeat_timeout_seconds=90)
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual(entry.data["configuration"], original)
        self.assertEqual(entry.options, {})
        await self.menu(manager, flow_id, "finish")
        saved = await self.submit(manager, flow_id, navigation_action="continue")
        cfg = Config.from_dict(saved["data"]["configuration"])
        self.assertEqual(cfg.heartbeat_timeout_seconds, 480)
        self.assertEqual(cfg.auxiliary_heartbeat_timeout_seconds, 90)
        self.assertEqual(raw, original)
        self.assertEqual(self.calls, [])

    async def test_back_preserves_unapplied_watchdog_edits_without_changing_entry(self):
        raw = configuration()
        manager, flow_id, entry = await self.start(raw, options=True)
        await self.menu(manager, flow_id, "system")
        original_draft = deepcopy(manager.editor.draft)
        result = await self.back(manager, flow_id, heartbeat_timeout_seconds=60,
                                 auxiliary_heartbeat_timeout_seconds=600)
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual(manager.editor.draft, original_draft)
        form = await self.menu(manager, flow_id, "system")
        self.assertEqual(self.suggested(form, "heartbeat_timeout_seconds"), 60)
        self.assertEqual(self.suggested(form, "auxiliary_heartbeat_timeout_seconds"), 600)
        await self.continue_shown(manager, flow_id, heartbeat_timeout_seconds=240,
                                  auxiliary_heartbeat_timeout_seconds=60)
        self.assertEqual(manager.editor.draft["heartbeat_timeout_seconds"], 240)
        self.assertEqual(manager.editor.draft["auxiliary_heartbeat_timeout_seconds"], 60)
        self.assertEqual(entry.data["configuration"], raw)
        self.assertEqual(entry.options, {})
        self.assertEqual(self.calls, [])

    async def test_auxiliary_longer_than_main_has_a_field_error_and_can_be_corrected(self):
        manager, flow_id, _ = await self.start(configuration())
        await self.menu(manager, flow_id, "system")
        original = deepcopy(manager.editor.draft)
        result = await self.submit(manager, flow_id, heartbeat_timeout_seconds=60,
                                   auxiliary_heartbeat_timeout_seconds=120)
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {
            "auxiliary_heartbeat_timeout_seconds": "auxiliary_heartbeat_exceeds_controller"})
        self.assertEqual(manager.editor.draft, original)
        corrected = await self.continue_shown(manager, flow_id,
                                              auxiliary_heartbeat_timeout_seconds=30)
        self.assertEqual(corrected["step_id"], "menu")
        self.assertEqual(manager.editor.draft["heartbeat_timeout_seconds"], 60)
        self.assertEqual(self.calls, [])

    async def test_direct_invalid_inputs_get_specific_field_errors(self):
        manager, flow_id, _ = await self.start(configuration())
        form = await self.menu(manager, flow_id, "system")
        original = deepcopy(manager.editor.draft)
        values = self.shown_values(form)
        values.pop("navigation_action")
        for field, value, code in (
            ("heartbeat_timeout_seconds", 59, "invalid_heartbeat_timeout"),
            ("heartbeat_timeout_seconds", True, "invalid_heartbeat_timeout"),
            ("auxiliary_heartbeat_timeout_seconds", 29, "invalid_auxiliary_heartbeat_timeout"),
            ("auxiliary_heartbeat_timeout_seconds", float("nan"), "invalid_auxiliary_heartbeat_timeout"),
        ):
            with self.subTest(field=field, value=value):
                result = await manager.editor.async_step_system({**values, field: value})
                self.assertEqual(result["errors"], {field: code})
                self.assertEqual(manager.editor.draft, original)
        self.assertEqual(self.calls, [])

    async def test_setup_and_options_translate_both_watchdog_fields_and_errors(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/house_climate"
        strings = json.loads((root / "strings.json").read_text())
        self.assertEqual(strings, json.loads((root / "translations/en.json").read_text()))
        for section in ("config", "options"):
            system = strings[section]["step"]["system"]
            self.assertIn("heartbeat_timeout_seconds", system["data"])
            self.assertIn("auxiliary_heartbeat_timeout_seconds", system["data"])
            self.assertIn("5 minutes", system["data_description"]["heartbeat_timeout_seconds"])
            self.assertIn("2 minutes", system["data_description"]["auxiliary_heartbeat_timeout_seconds"])
            self.assertIn("temperature cutoff", system["data_description"]["auxiliary_heartbeat_timeout_seconds"])
            for error in ("invalid_heartbeat_timeout", "invalid_auxiliary_heartbeat_timeout",
                          "auxiliary_heartbeat_exceeds_controller"):
                self.assertIn(error, strings[section]["error"])


if __name__ == "__main__":
    unittest.main()
