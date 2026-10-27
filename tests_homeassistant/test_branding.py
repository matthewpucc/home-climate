"""Display-name upgrades retain the identities used by dashboards and storage."""
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate import async_setup_entry, async_unload_entry
from custom_components.house_climate.const import DEVICE_NAME, DOMAIN, NAME
from custom_components.house_climate.sensor import ControllerStatus
from custom_components.house_climate.switch import ControllerEnabled
from tests_homeassistant import test_native as fixtures


class BrandingMigrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    def entry(self, title):
        if self.hass.config_entries is None:
            self.hass.config_entries = ConfigEntries(self.hass, {})
        entry = ConfigEntry(
            version=3, minor_version=1, domain=DOMAIN, title=title,
            source=SOURCE_USER, data={"config_file": "house_climate.toml", "live": False},
            options={}, unique_id=None, discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )
        self.hass.config_entries._entries[entry.entry_id] = entry
        return entry

    async def setup_and_check_title(self, title, expected):
        entry = self.entry(title)
        identity, data, options = entry.entry_id, dict(entry.data), dict(entry.options)
        with (
            patch.object(self.hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
            patch.object(self.hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
            self.runtimes.append(entry.runtime_data)
            self.assertEqual(entry.title, expected)
            self.assertEqual(entry.entry_id, identity)
            self.assertEqual(entry.domain, "house_climate")
            self.assertEqual(dict(entry.data), data)
            self.assertEqual(dict(entry.options), options)
            self.assertIsNone(entry.runtime_data.thread)
            self.assertEqual(self.calls, [])
            self.assertTrue(await async_unload_entry(self.hass, entry))

    async def test_existing_default_titles_refresh_without_replacing_settings(self):
        for title in ("House Climate", "House climate", "House climate controller"):
            with self.subTest(title=title):
                await self.setup_and_check_title(title, NAME)

    async def test_custom_entry_title_is_preserved(self):
        await self.setup_and_check_title("Upstairs comfort", "Upstairs comfort")

    async def test_current_entry_title_is_unchanged(self):
        await self.setup_and_check_title(NAME, NAME)

    async def test_registry_refresh_preserves_old_entity_ids_and_user_assigned_names(self):
        entry = self.entry("House Climate")
        entry.runtime_data = SimpleNamespace(status={"event": "shadow"})
        devices, entities = dr.async_get(self.hass), er.async_get(self.hass)
        old_device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("house_climate", entry.entry_id)},
            name="House climate controller", manufacturer="House climate",
            model="Shared heat-pump controller",
        )
        devices.async_update_device(old_device.id, name_by_user="My comfort controller")
        for domain, entity in (
                ("sensor", ControllerStatus(entry)), ("switch", ControllerEnabled(entry))):
            with self.subTest(domain=domain):
                original = entities.async_get_or_create(
                    domain, DOMAIN, entity.unique_id, config_entry=entry,
                    suggested_object_id=f"house_climate_controller_{domain}",
                    device_id=old_device.id, original_name=entity.name,
                )
                entities.async_update_entity(original.entity_id, name="My dashboard control")
                updated_device = devices.async_get_or_create(
                    config_entry_id=entry.entry_id, **entity.device_info,
                )
                refreshed = entities.async_get_or_create(
                    domain, DOMAIN, entity.unique_id, config_entry=entry,
                    suggested_object_id=f"home_climate_controller_{domain}",
                    device_id=updated_device.id, original_name=entity.name,
                )
                self.assertEqual(updated_device.id, old_device.id)
                self.assertEqual(updated_device.name, DEVICE_NAME)
                self.assertEqual(updated_device.manufacturer, NAME)
                self.assertEqual(updated_device.name_by_user, "My comfort controller")
                self.assertEqual(updated_device.identifiers, {("house_climate", entry.entry_id)})
                self.assertEqual(refreshed.entity_id, original.entity_id)
                self.assertEqual(refreshed.unique_id, original.unique_id)
                self.assertEqual(refreshed.name, "My dashboard control")
                self.assertEqual(refreshed.device_id, old_device.id)
        self.assertEqual(len(devices.devices), 1)
        self.assertEqual(len(entities.entities), 2)
        self.assertEqual(self.calls, [])
