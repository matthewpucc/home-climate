"""Exercise Back through HA's real schema validation and flow dispatch."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

import probatio
from homeassistant.data_entry_flow import FlowManager, InvalidData
from homeassistant.helpers import config_validation as cv

from custom_components.house_climate.config_flow import (
    HouseClimateConfigFlow, HouseClimateOptionsFlow,
)
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures


class EditorManager(FlowManager):
    """Keep HA's dispatch, while recording saves instead of installing entries."""

    def __init__(self, hass, editor):
        super().__init__(hass)
        self.editor = editor
        self.completed = []

    async def async_create_flow(self, handler_key, *, context=None, data=None):
        return self.editor

    async def async_finish_flow(self, flow, result):
        self.completed.append(deepcopy(result))
        return result


class ConfigNavigationTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.NativeTests.handle

    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            for flow in manager.async_progress():
                manager.async_abort(flow["flow_id"])
        await fixtures.NativeTests.asyncTearDown(self)

    async def start(self, raw=None, *, options=False):
        if options:
            entry = SimpleNamespace(
                data={"configuration": deepcopy(raw), "live": False}, options={},
            )
            self.enterContext(patch.object(
                HouseClimateOptionsFlow, "config_entry",
                new_callable=PropertyMock, return_value=entry,
            ))
            editor = HouseClimateOptionsFlow()
        else:
            entry = None
            editor = HouseClimateConfigFlow()
            editor.init_step = "user"
            editor._async_current_entries = Mock(return_value=[])
        manager = EditorManager(self.hass, editor)
        self.managers.append(manager)
        result = await manager.async_init("house_climate", context={"source": "user"})
        self.assertEqual(result["step_id"], "menu")
        if raw is not None and not options:
            editor._begin(raw)
        return manager, result["flow_id"], entry

    async def submit(self, manager, flow_id, **values):
        return await manager.async_configure(flow_id, values)

    async def menu(self, manager, flow_id, step):
        return await self.submit(manager, flow_id, next_step_id=step)

    async def room(self, manager, flow_id, room):
        await self.menu(manager, flow_id, "select_room")
        result = await self.submit(manager, flow_id, room=room)
        self.assertEqual(result["step_id"], "room_menu")
        return result

    def shown_values(self, result):
        """Values an untouched HA form sends, including restored draft suggestions."""
        values = {}
        for marker in result["data_schema"].schema:
            description = marker.description or {}
            if "suggested_value" in description:
                values[marker.schema] = deepcopy(description["suggested_value"])
            elif marker.default is not probatio.UNDEFINED:
                values[marker.schema] = marker.default()
        return values

    async def back(self, manager, flow_id, **changes):
        values = self.shown_values(manager.editor.cur_step)
        values.update(changes)
        values["navigation_action"] = "back"
        return await manager.async_configure(flow_id, values)

    async def continue_shown(self, manager, flow_id, **changes):
        values = self.shown_values(manager.editor.cur_step)
        values.update(changes)
        values["navigation_action"] = "continue"
        return await manager.async_configure(flow_id, values)

    def fields(self, result):
        return {
            marker.schema: marker
            for marker in result["data_schema"].schema
        }

    def suggested(self, result, field):
        return self.fields(result)[field].description["suggested_value"]

    async def test_blank_room_back_survives_real_ha_validation_and_serialization(self):
        manager, flow_id, _ = await self.start()
        form = await self.menu(manager, flow_id, "add_room")
        serialized = probatio.to_field_list(
            form["data_schema"], custom_serializer=cv.custom_serializer,
        )
        fields = {field["name"]: field for field in serialized}
        self.assertTrue(fields["navigation_action"]["required"])
        self.assertFalse(fields["head"]["required"])
        self.assertFalse(fields["sensors"]["required"])
        self.assertFalse(fields["target_f"]["required"])

        result = await self.submit(
            manager, flow_id, navigation_action="back",
            name="Unfinished den", target_f=200,
        )
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual(manager.editor.draft["rooms"], {})
        self.assertEqual(manager.completed, [])
        form = await self.menu(manager, flow_id, "add_room")
        self.assertEqual(self.suggested(form, "name"), "Unfinished den")
        self.assertEqual(self.suggested(form, "target_f"), 200)
        # Continue still validates the required selector before entering the editor.
        with self.assertRaises(InvalidData):
            await self.submit(
                manager, flow_id, navigation_action="continue",
                name="Den", sensors=["sensor.test_temperature"], target_f=72,
            )
        self.assertEqual(manager.editor.draft["rooms"], {})
        self.assertEqual(self.calls, [])

    async def test_initial_setup_back_preserves_earlier_edits_until_explicit_save(self):
        manager, flow_id, _ = await self.start()
        await self.menu(manager, flow_id, "add_room")
        result = await self.submit(
            manager, flow_id, name="Reading corner", head="climate.test_corner",
            sensors=["sensor.test_temperature"], target_f=72,
        )
        self.assertEqual(result["step_id"], "room_menu")
        room_id = manager.editor.room_id
        await self.menu(manager, flow_id, "menu")
        await self.menu(manager, flow_id, "system")
        result = await self.submit(
            manager, flow_id, decision_interval_seconds=120, poll_seconds=10,
        )
        self.assertEqual(result["step_id"], "menu")
        await self.menu(manager, flow_id, "head_limits")
        await self.submit(manager, flow_id, head="climate.test_corner")
        await self.submit(manager, flow_id, override_enabled=True, cool_min_f=65)
        await self.menu(manager, flow_id, "finish")
        result = await self.back(manager, flow_id)
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual(manager.completed, [])
        await self.menu(manager, flow_id, "finish")
        saved = await self.submit(manager, flow_id, navigation_action="continue")
        self.assertEqual(saved["type"], "create_entry")
        cfg = saved["data"]["configuration"]
        self.assertEqual(cfg["rooms"][room_id]["profile"]["target_f"], 72)
        self.assertEqual(cfg["decision_interval_seconds"], 120)
        self.assertEqual(cfg["poll_seconds"], 10)
        self.assertEqual(cfg["head_cool_min_f"], {"climate.test_corner": 65})
        self.assertEqual(len(manager.completed), 1)
        self.assertEqual(self.calls, [])

    async def test_options_keep_combined_changes_private_through_back_and_review(self):
        raw = configuration()
        original = deepcopy(raw)
        manager, flow_id, entry = await self.start(raw, options=True)
        await self.room(manager, flow_id, "alpha")
        await self.menu(manager, flow_id, "room")
        await self.submit(manager, flow_id, name="Library", target_f=73)
        await self.menu(manager, flow_id, "menu")
        await self.menu(manager, flow_id, "system")
        previous = deepcopy(manager.editor.draft)
        result = await self.back(
            manager, flow_id, low_f=999, decision_interval_seconds=120,
        )
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual(manager.editor.draft, previous)
        form = await self.menu(manager, flow_id, "system")
        self.assertEqual(self.suggested(form, "low_f"), 999)
        self.assertEqual(self.suggested(form, "decision_interval_seconds"), 120)
        await self.continue_shown(manager, flow_id, low_f=66, decision_interval_seconds=120)

        await self.menu(manager, flow_id, "head_limits")
        await self.submit(manager, flow_id, head="climate.test_shared")
        await self.submit(manager, flow_id, override_enabled=True, cool_min_f=65)
        await self.menu(manager, flow_id, "head_limits")
        await self.submit(manager, flow_id, head="climate.test_shared")
        result = await self.back(
            manager, flow_id, override_enabled=True, cool_min_f=66, apply_to_all=True,
        )
        self.assertEqual(result["step_id"], "head_limits")
        self.assertEqual(
            manager.editor.draft["head_cool_min_f"],
            {"climate.test_shared": 65},
        )
        form = await self.submit(manager, flow_id, head="climate.test_shared")
        self.assertEqual(self.suggested(form, "cool_min_f"), 66)
        self.assertFalse(self.fields(form)["apply_to_all"].default())
        await self.submit(
            manager, flow_id, override_enabled=True, cool_min_f=66, apply_to_all=True,
        )
        await self.menu(manager, flow_id, "finish")
        await self.back(manager, flow_id)
        self.assertEqual(manager.completed, [])
        self.assertEqual(entry.data, {"configuration": original, "live": False})
        self.assertEqual(entry.options, {})
        self.assertEqual(raw, original)
        await self.menu(manager, flow_id, "finish")
        result = await self.submit(manager, flow_id, navigation_action="continue")
        saved = result["data"]["configuration"]
        self.assertEqual(saved["rooms"]["alpha"]["name"], "Library")
        self.assertEqual(saved["rooms"]["alpha"]["profile"]["target_f"], 73)
        self.assertEqual(saved["decision_interval_seconds"], 120)
        self.assertEqual(saved["head_cool_min_f"], {
            "climate.test_shared": 66, "climate.test_second": 66,
        })
        self.assertEqual(entry.options, {})
        self.assertEqual(self.calls, [])

    async def test_room_buffers_are_isolated_and_cancel_discards_only_private_draft(self):
        raw = configuration()
        manager, flow_id, entry = await self.start(raw, options=True)
        await self.room(manager, flow_id, "alpha")
        await self.menu(manager, flow_id, "room")
        result = await self.back(manager, flow_id, name="Unfinished library")
        self.assertEqual(result["step_id"], "room_menu")
        form = await self.menu(manager, flow_id, "room")
        self.assertEqual(self.suggested(form, "name"), "Unfinished library")
        await self.back(manager, flow_id)
        await self.menu(manager, flow_id, "menu")
        await self.room(manager, flow_id, "beta")
        form = await self.menu(manager, flow_id, "room")
        self.assertEqual(form["data_schema"]({})["name"], "South nook")
        self.assertNotIn(
            "suggested_value", self.fields(form)["name"].description or {},
        )
        await self.back(manager, flow_id)
        await self.menu(manager, flow_id, "menu")
        await self.room(manager, flow_id, "alpha")
        form = await self.menu(manager, flow_id, "room")
        self.assertEqual(self.suggested(form, "name"), "Unfinished library")
        await self.continue_shown(manager, flow_id, name="Saved only in draft", target_f=73)
        self.assertEqual(manager.editor.draft["rooms"]["alpha"]["name"], "Saved only in draft")
        manager.async_abort(flow_id)
        self.assertEqual(manager.async_progress(), [])
        self.assertEqual(manager.completed, [])
        self.assertEqual(entry.data["configuration"], raw)
        self.assertEqual(entry.options, {})

        reopened, new_id, _ = await self.start(entry.data["configuration"], options=True)
        await self.room(reopened, new_id, "alpha")
        form = await self.menu(reopened, new_id, "room")
        self.assertEqual(form["data_schema"]({})["name"], "North alcove")
        self.assertNotIn("suggested_value", self.fields(form)["name"].description or {})
        self.assertEqual(self.calls, [])

    async def test_nested_back_does_not_remove_schedules_fans_or_rooms(self):
        manager, flow_id, _ = await self.start(configuration())
        await self.room(manager, flow_id, "gamma")
        await self.menu(manager, flow_id, "select_schedule")
        await self.submit(manager, flow_id, schedule="0")
        previous = deepcopy(manager.editor.draft)
        result = await self.back(
            manager, flow_id, name="Unfinished sleep", start="not a time", remove=True,
        )
        self.assertEqual(result["step_id"], "room_menu")
        self.assertEqual(manager.editor.draft, previous)
        await self.menu(manager, flow_id, "select_schedule")
        form = await self.submit(manager, flow_id, schedule="0")
        self.assertEqual(self.suggested(form, "name"), "Unfinished sleep")
        self.assertEqual(self.suggested(form, "start"), "not a time")
        self.assertFalse(self.fields(form)["remove"].default())
        await self.back(manager, flow_id)
        await self.menu(manager, flow_id, "select_fan")
        form = await self.submit(manager, flow_id, fan="0")
        with self.assertRaises(InvalidData):
            await self.submit(manager, flow_id, percentage=101)
        await self.back(manager, flow_id, percentage=101, remove=True)
        self.assertEqual(manager.editor.draft, previous)
        await self.menu(manager, flow_id, "select_fan")
        form = await self.submit(manager, flow_id, fan="0")
        self.assertEqual(self.suggested(form, "percentage"), 101)
        self.assertFalse(self.fields(form)["remove"].default())
        await self.back(manager, flow_id)
        await self.menu(manager, flow_id, "remove_room")
        result = await self.back(manager, flow_id, confirm=True)
        self.assertEqual(result["step_id"], "room_menu")
        form = await self.menu(manager, flow_id, "remove_room")
        self.assertFalse(self.fields(form)["confirm"].default())
        self.assertEqual(manager.editor.draft, previous)
        self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])

    async def test_back_keeps_explicitly_cleared_optional_room_fields_empty(self):
        raw = configuration()
        raw["rooms"]["alpha"]["area_id"] = "old_area"
        raw["rooms"]["alpha"]["profile"].update(low_f=68, high_f=74)
        manager, flow_id, _ = await self.start(raw)
        await self.room(manager, flow_id, "alpha")
        await self.menu(manager, flow_id, "room")
        await self.submit(
            manager, flow_id, navigation_action="back",
            name="North alcove", head="climate.test_shared",
            sensors=["sensor.test_a", "sensor.test_b"], target_f=72,
            half_band_f=.5, priority=1, comfort_enabled=True, fan_percentage=0,
        )
        form = await self.menu(manager, flow_id, "room")
        for name in ("area", "low_f", "high_f"):
            marker = self.fields(form)[name]
            self.assertIs(marker.default, probatio.UNDEFINED)
            self.assertNotIn("suggested_value", marker.description or {})
        values = form["data_schema"](self.shown_values(form))
        for name in ("area", "low_f", "high_f"):
            self.assertNotIn(name, values)
        result = await manager.async_configure(flow_id, values)
        self.assertEqual(result["step_id"], "room_menu")
        room = manager.editor.draft["rooms"]["alpha"]
        self.assertNotIn("area_id", room)
        self.assertNotIn("low_f", room["profile"])
        self.assertNotIn("high_f", room["profile"])
        self.assertEqual(raw["rooms"]["alpha"]["area_id"], "old_area")
        self.assertEqual(self.calls, [])

    async def test_back_from_pickers_and_incomplete_auxiliary_or_import_never_applies(self):
        manager, flow_id, _ = await self.start(configuration())
        previous = deepcopy(manager.editor.draft)
        for step, changes in (
            ("select_room", {}),
            ("head_limits", {}),
            ("auxiliary", {"enabled": True, "heater_high_setpoint_f": "unfinished"}),
            ("import_file", {"config_file": "/does-not-exist/unfinished.toml"}),
        ):
            with self.subTest(step=step):
                form = await self.menu(manager, flow_id, step)
                self.assertEqual(form["step_id"], step)
                result = await self.submit(
                    manager, flow_id, navigation_action="back", **changes,
                )
                self.assertEqual(result["step_id"], "menu")
                self.assertEqual(manager.editor.draft, previous)
                self.assertEqual(manager.completed, [])
        self.assertEqual(self.calls, [])

    async def test_unfinished_schedule_follows_its_item_when_an_earlier_item_is_removed(self):
        raw = configuration()
        raw["rooms"]["gamma"]["schedules"].append({
            "name": "Morning", "start": "05:00", "end": "08:00",
            "target_f": 70, "half_band_f": .5, "priority": 4,
        })
        manager, flow_id, _ = await self.start(raw)
        await self.room(manager, flow_id, "gamma")
        await self.menu(manager, flow_id, "select_schedule")
        await self.submit(manager, flow_id, schedule="1")
        await self.back(manager, flow_id, name="Unfinished morning", target_f=71)
        await self.menu(manager, flow_id, "select_schedule")
        await self.submit(manager, flow_id, schedule="0")
        result = await self.submit(manager, flow_id, remove=True)
        self.assertEqual(result["step_id"], "room_menu")
        self.assertEqual(
            [item["name"] for item in manager.editor.draft["rooms"]["gamma"]["schedules"]],
            ["Morning"],
        )
        await self.menu(manager, flow_id, "select_schedule")
        form = await self.submit(manager, flow_id, schedule="0")
        self.assertEqual(self.suggested(form, "name"), "Unfinished morning")
        self.assertEqual(self.suggested(form, "target_f"), 71)
        self.assertFalse(self.fields(form)["remove"].default())
        await self.continue_shown(manager, flow_id)
        schedules = manager.editor.draft["rooms"]["gamma"]["schedules"]
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["name"], "Unfinished morning")
        self.assertEqual(schedules[0]["target_f"], 71)
        self.assertEqual(raw["rooms"]["gamma"]["schedules"][1]["name"], "Morning")
        self.assertEqual(self.calls, [])
