"""Device-page recovery buttons target the configured head, including in shadow."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.const import EntityCategory, Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.house_climate import PLATFORMS
from custom_components.house_climate.button import ReloadHeadIntegration, async_setup_entry
from custom_components.house_climate.controller.config import Config
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures


class RecoveryButtonTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    def entry(self):
        runtime = SimpleNamespace(
            config=Config.from_dict(configuration()), stop_event=threading.Event(),
            recovery=SimpleNamespace(async_reload_entity=AsyncMock()),
            live=False, controls=None,
        )
        return SimpleNamespace(entry_id="recovery_buttons", runtime_data=runtime,
                               disabled_by=None, pref_disable_new_entities=False)

    async def test_setup_has_one_enabled_button_per_head_with_configured_room_names(self):
        self.assertIn(Platform.BUTTON, PLATFORMS)
        entry = self.entry()
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        self.assertEqual(len(added), 2)
        by_name = {button.name: button for button in added}
        self.assertEqual(set(by_name), {
            "North alcove, South nook reload integration", "Quiet space reload integration"})
        shared = by_name["North alcove, South nook reload integration"]
        self.assertEqual(shared.unique_id, "recovery_buttons_reload_shared")
        self.assertEqual(shared.device_info["identifiers"],
                         {("house_climate", "recovery_buttons")})
        self.assertEqual(shared.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertTrue(shared.entity_registry_enabled_default)
        self.assertTrue(shared.available)
        self.assertEqual(shared.extra_state_attributes["reload_scope"],
                         "All devices in the owning integration entry")

    async def test_manual_press_forwards_exact_head_in_shadow_without_enable_requirement(self):
        entry = self.entry()
        button = ReloadHeadIntegration(entry, "shared")
        await button.async_press()
        entry.runtime_data.recovery.async_reload_entity.assert_awaited_once_with("climate.test_shared")
        self.assertEqual(self.calls, [])
        entry.runtime_data.recovery.async_reload_entity.reset_mock()
        entry.runtime_data.live = True
        entry.runtime_data.controls = SimpleNamespace(enabled=False)
        await ReloadHeadIntegration(entry, "second").async_press()
        entry.runtime_data.recovery.async_reload_entity.assert_awaited_once_with("climate.test_second")

    async def test_stopping_controller_rejects_reload_and_marks_button_unavailable(self):
        entry = self.entry()
        button = ReloadHeadIntegration(entry, "shared")
        entry.runtime_data.stop_event.set()
        self.assertFalse(button.available)
        with self.assertRaisesRegex(HomeAssistantError, "Controller is stopping"):
            await button.async_press()
        entry.runtime_data.recovery.async_reload_entity.assert_not_awaited()

    async def test_reload_error_is_reported_to_the_user(self):
        entry = self.entry()
        entry.runtime_data.recovery.async_reload_entity.side_effect = ValueError("No owning integration entry")
        with self.assertRaisesRegex(HomeAssistantError, "No owning integration entry"):
            await ReloadHeadIntegration(entry, "shared").async_press()

    async def test_reconfigure_removes_obsolete_buttons_and_preserves_existing_ids(self):
        entry = self.entry()
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_get_entry.return_value = entry
        registry = er.async_get(self.hass)
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        old = {}
        for button in added:
            old[button.unique_id] = registry.async_get_or_create(
                "button", "house_climate", button.unique_id, config_entry=entry,
                original_name=button.name,
            ).entity_id
        unrelated = registry.async_get_or_create(
            "button", "another_integration", "recovery_buttons_reload_second", config_entry=entry)
        raw = configuration()
        raw["rooms"]["alpha"]["name"] = "Renamed alcove"
        del raw["rooms"]["gamma"]
        del raw["heads"]["second"]
        entry.runtime_data.config = Config.from_dict(raw)
        refreshed = []
        await async_setup_entry(self.hass, entry, refreshed.extend)
        self.assertEqual(registry.async_get_entity_id(
            "button", "house_climate", "recovery_buttons_reload_shared"),
            old["recovery_buttons_reload_shared"])
        self.assertIsNone(registry.async_get(old["recovery_buttons_reload_second"]))
        self.assertIsNotNone(registry.async_get(unrelated.entity_id))
        self.assertEqual(refreshed[0].name, "Renamed alcove, South nook reload integration")
