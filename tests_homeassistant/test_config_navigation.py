"""New setup is one opt-in form, with no nested drafts or advanced menus."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

import probatio
from homeassistant.data_entry_flow import FlowManager, InvalidData
from homeassistant.helpers import config_validation as cv

from custom_components.house_climate.config_flow import HouseClimateConfigFlow, HouseClimateOptionsFlow
from custom_components.house_climate.controller.bindings import BINDINGS_FILENAME, example_bindings
from tests_homeassistant import test_native as fixtures


class EditorManager(FlowManager):
    def __init__(self, hass, editor):
        super().__init__(hass)
        self.editor, self.completed = editor, []

    async def async_create_flow(self, handler_key, *, context=None, data=None):
        return self.editor

    async def async_finish_flow(self, flow, result):
        self.completed.append(deepcopy(result))
        return result


class FixedFlowTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.NativeTests.handle

    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.managers = []
        Path(self.directory.name, BINDINGS_FILENAME).write_text(json.dumps(example_bindings().to_dict()))

    async def asyncTearDown(self):
        for manager in self.managers:
            for flow in manager.async_progress():
                manager.async_abort(flow["flow_id"])
        await fixtures.NativeTests.asyncTearDown(self)

    async def start(self, *, options=False, entry=None):
        if options:
            entry = entry or SimpleNamespace(data={"live": False}, options={})
            self.enterContext(patch.object(HouseClimateOptionsFlow, "config_entry",
                                          new_callable=PropertyMock, return_value=entry))
            flow = HouseClimateOptionsFlow()
        else:
            flow = HouseClimateConfigFlow()
            flow.init_step = "user"
            flow._async_current_entries = Mock(return_value=[])
        manager = EditorManager(self.hass, flow)
        self.managers.append(manager)
        result = await manager.async_init("house_climate", context={"source": "user"})
        return manager, result, entry

    async def test_real_setup_serializes_only_live_opt_in_and_saves_in_one_step(self):
        manager, form, _ = await self.start()
        fields = probatio.to_field_list(form["data_schema"], custom_serializer=cv.custom_serializer)
        self.assertEqual([field["name"] for field in fields], ["live"])
        self.assertEqual(form["step_id"], "user")
        self.assertFalse(form["data_schema"]({})["live"])
        saved = await manager.async_configure(form["flow_id"], {"live": False})
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"], {"policy": "house", "live": False})
        self.assertEqual(self.calls, [])

    async def test_options_use_latest_live_permission_and_save_no_tuning(self):
        entry = SimpleNamespace(data={"policy": "house", "live": False}, options={"live": True})
        manager, form, _ = await self.start(options=True, entry=entry)
        self.assertEqual(form["step_id"], "init")
        self.assertTrue(form["data_schema"]({})["live"])
        saved = await manager.async_configure(form["flow_id"], {"live": False})
        self.assertEqual(saved["data"], {"policy": "house", "live": False})
        self.assertEqual(entry.options, {"live": True})
        self.assertEqual(self.calls, [])

    async def test_close_cancels_only_unsaved_live_permission(self):
        entry = SimpleNamespace(data={"policy": "house", "live": False}, options={})
        original = deepcopy(entry.data)
        manager, form, _ = await self.start(options=True, entry=entry)
        manager.async_abort(form["flow_id"])
        self.assertEqual(entry.data, original)
        self.assertEqual(entry.options, {})
        self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])

    async def test_removed_tuning_fields_are_rejected_by_real_form_dispatch(self):
        manager, form, _ = await self.start()
        with self.assertRaises(InvalidData):
            await manager.async_configure(form["flow_id"], {"live": True, "heartbeat_timeout_seconds": 900})
        self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])

    async def test_missing_private_wiring_gives_a_clear_error_without_saving_or_actuating(self):
        Path(self.directory.name, BINDINGS_FILENAME).unlink()
        for options in (False, True):
            with self.subTest(options=options):
                manager, form, entry = await self.start(options=options)
                result = await manager.async_configure(form["flow_id"], {"live": True})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": "invalid_config"})
                self.assertIn(BINDINGS_FILENAME, result["description_placeholders"]["detail"])
                self.assertEqual(manager.completed, [])
                if entry is not None:
                    self.assertEqual(entry.options, {})
        self.assertEqual(self.calls, [])

    async def test_direct_bad_live_value_does_not_enable_control(self):
        manager, _, _ = await self.start()
        result = await manager.editor.async_step_user({"live": "false"})
        self.assertEqual(result["errors"], {"base": "invalid_config"})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
