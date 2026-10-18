"""Head cooling floors are explicit, staged, unit-aware editor settings."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.config_flow import HouseClimateOptionsFlow
from custom_components.house_climate.controller.config import Config
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_rooms_ui as room_fixtures


class HeadLimitsUITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    flow = room_fixtures.RoomUITests.flow

    async def choose(self, flow, entity="climate.test_shared"):
        form = await flow.async_step_head_limits({"head": entity})
        self.assertEqual(form["step_id"], "head_limit")
        return form

    async def test_head_menu_uses_room_labels_and_does_not_install_suggested_default(self):
        flow = await self.flow()
        flow._begin(configuration())
        menu = await flow.async_step_menu()
        self.assertIn("head_limits", menu["menu_options"])
        selection = await flow.async_step_head_limits()
        selector = next(iter(selection["data_schema"].schema.values()))
        choices = {item["value"]: item["label"] for item in selector.config["options"]}
        self.assertIn("North alcove, South nook", choices["climate.test_shared"])
        self.assertIn("Quiet space", choices["climate.test_second"])
        form = await self.choose(flow)
        defaults = form["data_schema"]({})
        self.assertFalse(defaults["override_enabled"])
        self.assertFalse(defaults["apply_to_all"])
        self.assertEqual(defaults["cool_min_f"], 65)
        self.assertNotIn("head_cool_min_f", flow.draft)
        self.assertEqual(Config.from_dict(flow.draft).head_cool_min_f, {})
        self.assertEqual(self.calls, [])

    async def test_selected_head_limit_preserves_other_heads_and_room_profiles(self):
        raw = configuration()
        raw["head_cool_min_f"] = {"climate.test_second": 67}
        flow = await self.flow()
        flow._begin(raw)
        rooms = deepcopy(flow.draft["rooms"])
        form = await self.choose(flow)
        values = form["data_schema"]({"override_enabled": True, "cool_min_f": 65})
        self.assertEqual((await flow.async_step_head_limit(values))["type"], "menu")
        self.assertEqual(flow.draft["head_cool_min_f"],
                         {"climate.test_shared": 65, "climate.test_second": 67})
        self.assertEqual(flow.draft["rooms"], rooms)
        self.assertEqual(raw["head_cool_min_f"], {"climate.test_second": 67})
        reopened = await self.choose(flow)
        self.assertTrue(reopened["data_schema"]({})["override_enabled"])
        self.assertEqual(reopened["data_schema"]({})["cool_min_f"], 65)
        self.assertEqual(self.calls, [])

    async def test_apply_to_all_is_explicit_and_can_clear_all_overrides(self):
        flow = await self.flow()
        flow._begin(configuration())
        form = await self.choose(flow)
        values = form["data_schema"]({
            "override_enabled": True, "cool_min_f": 65, "apply_to_all": True})
        await flow.async_step_head_limit(values)
        self.assertEqual(flow.draft["head_cool_min_f"],
                         {entity: 65 for entity in flow.draft["heads"].values()})
        form = await self.choose(flow)
        self.assertFalse(form["data_schema"]({})["apply_to_all"])
        values = form["data_schema"]({"override_enabled": False, "apply_to_all": True})
        await flow.async_step_head_limit(values)
        self.assertEqual(flow.draft["head_cool_min_f"], {})

    async def test_disabling_only_selected_head_keeps_other_head_limit(self):
        flow = await self.flow()
        raw = configuration()
        raw["head_cool_min_f"] = {"climate.test_shared": 65, "climate.test_second": 66}
        flow._begin(raw)
        form = await self.choose(flow)
        await flow.async_step_head_limit(form["data_schema"]({"override_enabled": False}))
        self.assertEqual(flow.draft["head_cool_min_f"], {"climate.test_second": 66})

    async def test_metric_cooling_minimum_is_an_absolute_temperature_converted_once(self):
        self.hass.config.units = METRIC_SYSTEM
        flow = await self.flow()
        flow._begin(configuration())
        form = await self.choose(flow)
        defaults = form["data_schema"]({})
        self.assertAlmostEqual(defaults["cool_min_f"], (65 - 32) * 5 / 9, places=3)
        control = next(value for marker, value in form["data_schema"].schema.items()
                       if marker.schema == "cool_min_f")
        self.assertEqual(control.config["unit_of_measurement"], "°C")
        values = form["data_schema"]({"override_enabled": True, "cool_min_f": 18})
        await flow.async_step_head_limit(values)
        self.assertAlmostEqual(flow.draft["head_cool_min_f"]["climate.test_shared"], 64.4)
        reopened = await self.choose(flow)
        self.assertEqual(reopened["data_schema"]({})["cool_min_f"], 18)
        await flow.async_step_head_limit(reopened["data_schema"]({}))
        self.assertAlmostEqual(flow.draft["head_cool_min_f"]["climate.test_shared"], 64.4)

    async def test_room_rename_and_logical_head_rename_preserve_entity_keyed_limit(self):
        flow = await self.flow()
        raw = configuration()
        raw["head_cool_min_f"] = {"climate.test_shared": 65}
        flow._begin(raw)
        flow.room_id = "alpha"
        form = await flow.async_step_room()
        values = form["data_schema"]({"name": "Renamed room"})
        await flow.async_step_room(values)
        self.assertEqual(flow.draft["head_cool_min_f"], {"climate.test_shared": 65})
        candidate = deepcopy(flow.draft)
        candidate["heads"]["new_shared_key"] = candidate["heads"].pop("shared")
        for room in candidate["rooms"].values():
            if room["head"] == "shared":
                room["head"] = "new_shared_key"
        normalized = flow.normalized(candidate)
        self.assertEqual(Config.from_dict(normalized).head_cool_min_f, {"climate.test_shared": 65})

    async def test_replacing_and_removing_rooms_prunes_only_unused_head_limits(self):
        flow = await self.flow()
        raw = configuration()
        raw["rooms"]["alpha"]["fans"] = []
        raw["head_cool_min_f"] = {"climate.test_shared": 65, "climate.test_second": 66}
        flow._begin(raw)
        flow.room_id = "alpha"
        form = await flow.async_step_room()
        values = form["data_schema"]({"head": "climate.test_replacement"})
        self.assertEqual((await flow.async_step_room(values))["type"], "menu")
        self.assertEqual(flow.draft["head_cool_min_f"],
                         {"climate.test_shared": 65, "climate.test_second": 66})
        flow.room_id = "beta"
        self.assertEqual((await flow.async_step_remove_room({"confirm": True}))["type"], "menu")
        self.assertNotIn("shared", flow.draft["heads"])
        self.assertEqual(flow.draft["head_cool_min_f"], {"climate.test_second": 66})
        self.assertEqual(Config.from_dict(flow.draft).head_cool_min_f, {"climate.test_second": 66})

    async def test_options_remain_private_until_review_and_save(self):
        raw = configuration()
        entry = SimpleNamespace(data={"configuration": raw, "live": False}, options={})
        flow = HouseClimateOptionsFlow()
        flow.hass = self.hass
        with patch.object(HouseClimateOptionsFlow, "config_entry", new_callable=PropertyMock,
                          return_value=entry):
            await flow.async_step_init()
            form = await self.choose(flow)
            await flow.async_step_head_limit(form["data_schema"]({
                "override_enabled": True, "cool_min_f": 65}))
            self.assertNotIn("head_cool_min_f", raw)
            self.assertEqual(entry.options, {})
            saved = await flow.async_step_finish({})
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"]["configuration"]["head_cool_min_f"],
                         {"climate.test_shared": 65})
        self.assertNotIn("head_cool_min_f", raw)

    async def test_invalid_head_or_limit_preserves_the_private_draft(self):
        flow = await self.flow()
        flow._begin(configuration())
        original = deepcopy(flow.draft)
        invalid = await flow.async_step_head_limits({"head": "climate.not_configured"})
        self.assertEqual(invalid["errors"], {"base": "invalid_config"})
        await self.choose(flow)
        for value in (-41, 141, float("nan"), True):
            with self.subTest(value=value):
                result = await flow.async_step_head_limit({
                    "override_enabled": True, "cool_min_f": value, "apply_to_all": False})
                self.assertEqual(result["errors"], {"base": "invalid_config"})
                self.assertEqual(flow.draft, original)

    async def test_head_limit_setup_without_rooms_returns_to_room_setup(self):
        flow = await self.flow()
        result = await flow.async_step_head_limits()
        self.assertEqual(result["step_id"], "room")
        self.assertNotIn("head_cool_min_f", flow.draft)
