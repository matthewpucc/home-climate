"""Durable room isolation, including failed saves and reactivation boundaries."""
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

from tests_homeassistant import test_comfort_controls as fixtures


class ProtectionOnlyStorageTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.RoomComfortTests.handle
    asyncSetUp = fixtures.RoomComfortTests.asyncSetUp
    asyncTearDown = fixtures.RoomComfortTests.asyncTearDown
    manager = fixtures.RoomComfortTests.manager

    async def test_protection_only_restores_and_does_not_expire_at_schedule(self):
        comfort = await self.manager(entry_id="protected")
        await comfort.async_set_target("guest", 73)
        await comfort.async_set_protection_only("guest")
        before = comfort.effective("guest")
        self.assertEqual(before["mode"], "protection_only")
        self.assertIsNone(before["expires_at"])
        self.assertFalse(before["comfort_enabled"])
        settings = comfort.settings({}, self.cfg, self.now)
        self.assertEqual(settings.protection_only_rooms, frozenset({"guest"}))
        self.assertFalse(settings.room_profiles["guest"].comfort_enabled)
        self.now += timedelta(days=3)
        restored = await self.manager(entry_id="protected")
        self.assertEqual(restored.effective("guest"), before)
        await restored.async_resume("guest")
        self.assertEqual(restored.effective("guest")["mode"], "schedule")
        self.assertEqual(restored.settings({}, self.cfg, self.now).protection_only_rooms, frozenset())

    async def test_protection_only_cannot_be_cleared_by_target_or_range(self):
        comfort = await self.manager()
        await comfort.async_set_protection_only("guest")
        saved = await comfort.store.async_load()
        for edit in (comfort.async_set_target("guest", 72),
                     comfort.async_set_range("guest", 71, 73)):
            with self.assertRaises(ValueError):
                await edit
        self.assertEqual(await comfort.store.async_load(), saved)
        self.assertEqual(comfort.effective("guest")["mode"], "protection_only")
        await comfort.async_hold("guest", persistent=True)
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        await comfort.async_set_target("guest", 72)
        self.assertEqual(comfort.effective("guest")["mode"], "hold")

    async def test_failed_protection_save_or_resume_keeps_published_and_durable_intent(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 72)
        before = comfort.effective("guest")
        saved = await comfort.store.async_load()
        with patch.object(comfort.store, "async_save", AsyncMock(side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                await comfort.async_set_protection_only("guest")
        self.assertEqual(comfort.effective("guest"), before)
        self.assertEqual(await comfort.store.async_load(), saved)
        await comfort.async_set_protection_only("guest")
        protected = comfort.effective("guest")
        with patch.object(comfort.store, "async_save", AsyncMock(side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                await comfort.async_resume("guest")
        self.assertEqual(comfort.effective("guest"), protected)
        restored = await self.manager()
        self.assertEqual(restored.effective("guest"), protected)

    async def test_runtime_auxiliary_authorization_distinguishes_protection_only(self):
        from types import SimpleNamespace
        from custom_components.house_climate.runtime import ControllerRuntime
        from custom_components.house_climate.controller.protection_context import limits
        comfort = await self.manager()
        runtime = SimpleNamespace(
            hass=self.hass, config=self.cfg, comfort=comfort, status={},
            api=SimpleNamespace(_read_states_without_controls=lambda: []))
        previous = ControllerRuntime._auxiliary_room_context(runtime)
        await comfort.async_set_protection_only(self.cfg.auxiliary_room)
        protected = ControllerRuntime._auxiliary_room_context(runtime)
        low, high = limits(self.cfg, None)
        self.assertEqual(protected[:4], [low, high, (low + high) / 2, True])
        self.assertEqual(protected[-1], "protection_only")
        self.assertNotEqual(protected, previous)
        ceiling = (self.cfg.protection.emergency_cooling_at_f if self.cfg.protection else self.cfg.high_f) - 1
        self.assertEqual(ControllerRuntime._auxiliary_room_limit(runtime), ceiling)
        await comfort.async_resume(self.cfg.auxiliary_room)
        self.assertEqual(ControllerRuntime._auxiliary_room_context(runtime), previous)
