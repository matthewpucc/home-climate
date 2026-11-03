"""Persistent room holds and scoped Assist/MCP release use real HA storage."""
from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.components.intent import OnOffIntentHandler
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from custom_components.house_climate.button import ResumeRoomSchedule
from custom_components.house_climate.climate import RoomClimate
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.house_policy import house_configuration
from custom_components.house_climate.sensor import RoomComfortMode
from custom_components.house_climate.controls import NativeControls
from tests_homeassistant import test_comfort_controls as fixtures


class PersistentRoomHoldTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RoomComfortTests.asyncSetUp
    asyncTearDown = fixtures.RoomComfortTests.asyncTearDown
    handle = fixtures.RoomComfortTests.handle
    manager = fixtures.RoomComfortTests.manager

    async def house_manager(self):
        self.cfg = house_configuration(bindings=example_bindings())
        self.now = datetime(2026, 11, 7, 5, tzinfo=timezone.utc)  # Friday 21:00 PST
        return await self.manager(self.cfg)

    async def test_explicit_hold_crosses_weekend_overnight_and_seasonal_boundaries(self):
        c = await self.house_manager()
        await c.async_set_target("living", 66)
        await c.async_set_range("living", 66, 72)
        await c.async_hold("living", persistent=True)
        before = c.effective("living")
        self.assertEqual((before["mode"], before["expires_at"]), ("hold", None))
        original_config = self.cfg.to_dict()
        for hours in (2, 12, 36, 60, 240):
            self.now = datetime(2026, 11, 7, 5, tzinfo=timezone.utc) + timedelta(hours=hours)
            c.set_seasonal_profile("warm")
            self.assertEqual(c.effective("living"), before)
            settings = c.settings({}, self.cfg, self.now)
            profile = settings.profile(self.cfg.zones["living"], self.now,
                                       self.cfg.timezone, room_id="living")
            self.assertEqual((profile.low_f, profile.high_f, profile.target_f), (66, 72, 66))
            self.assertEqual(set(settings.room_profiles), {"living"})
        self.assertEqual(self.cfg.to_dict(), original_config)
        self.assertEqual(self.calls, [])

    async def test_hold_edits_restart_and_explicit_schedule_are_durable(self):
        c = await self.house_manager()
        await c.async_hold("living", persistent=True)
        await c.async_set_target("living", 66)
        await c.async_set_range("living", 66, 72)
        await c.async_close()
        self.now += timedelta(days=4)
        restored = await self.manager(self.cfg)
        self.assertEqual(restored.effective("living")["mode"], "hold")
        self.assertEqual(restored.effective("living")["target_f"], 66)
        self.assertEqual(restored.effective("living")["high_f"], 72)
        await restored.async_resume("living")
        await restored.async_resume("living")
        again = await self.manager(self.cfg)
        self.assertEqual(again.effective("living")["mode"], "schedule")
        self.assertEqual((await again.store.async_load())["rooms"], {})

    async def test_persistent_preset_preserves_limits_only_displayed_range(self):
        c = await self.house_manager()
        self.now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
        before = c.effective("living")
        self.assertFalse(before["comfort_enabled"])
        await c.async_hold("living", persistent=True)
        after = c.effective("living")
        self.assertEqual(tuple(after[k] for k in ("low_f", "high_f", "target_f")),
                         tuple(before[k] for k in ("low_f", "high_f", "target_f")))
        self.assertTrue(after["comfort_enabled"])

    async def test_temporary_selection_reinstates_own_room_boundary(self):
        c = await self.house_manager()
        await c.async_hold("living", persistent=True)
        await c.async_hold("office", persistent=True)
        await c.async_hold("living")
        expiry = c.effective("living")["expires_at"]
        self.assertEqual(expiry, "2026-11-07T06:00:00+00:00")
        self.now = datetime.fromisoformat(expiry)
        await c._async_expire()
        self.assertEqual(c.effective("living")["mode"], "schedule")
        self.assertEqual(c.effective("office")["mode"], "hold")

    async def test_queued_old_expiry_cannot_remove_new_hold_during_awaited_save(self):
        c = await self.house_manager()
        await c.async_hold("living")
        expiry = datetime.fromisoformat(c.effective("living")["expires_at"])
        entered, release = asyncio.Event(), asyncio.Event()
        original = c.store.async_save
        async def delayed_save(data):
            entered.set()
            await release.wait()
            await original(data)
        with patch.object(c.store, "async_save", side_effect=delayed_save):
            save = asyncio.create_task(c.async_hold("living", persistent=True))
            await entered.wait()
            self.now = expiry
            c._expiry_due(expiry)
            release.set()
            await save
            await c._expiry_task
        restored = await self.manager(self.cfg)
        self.assertEqual(restored.effective("living")["mode"], "hold")
        self.assertIsNone(restored.effective("living")["expires_at"])
        self.assertEqual(self.calls, [])

    async def test_legacy_no_expiry_does_not_become_persistent(self):
        c = await self.manager()
        await c.async_set_target("guest", 73)  # Legacy unscheduled room.
        data = await c.store.async_load()
        data["rooms"]["guest"].pop("mode")
        await c.store.async_save(data)
        restored = await self.manager()
        self.assertEqual(restored.effective("guest")["mode"], "temporary")
        self.assertIsNone(restored.effective("guest")["expires_at"])

    async def test_corrupt_persistent_modes_are_rejected_without_revival(self):
        c = await self.house_manager()
        await c.async_hold("living", persistent=True)
        valid = await c.store.async_load()
        for changes in ({"mode": "forever"}, {"mode": True},
                        {"expires_at": (self.now + timedelta(days=1)).isoformat()},
                        {"target_f": 65}, {"fingerprint": "wrong-room"}):
            data = deepcopy(valid)
            data["rooms"]["living"].update(changes)
            await c.store.async_save(data)
            restored = await self.manager(self.cfg)
            self.assertEqual(restored.effective("living")["mode"], "schedule")

    async def test_failed_create_edit_and_release_keep_last_durable_intent(self):
        c = await self.house_manager()
        before = c.effective("living")
        with patch.object(type(c.store), "async_save", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                await c.async_hold("living", persistent=True)
        self.assertEqual(c.effective("living"), before)
        await c.async_hold("living", persistent=True)
        before = c.effective("living")
        saved = await c.store.async_load()
        with patch.object(type(c.store), "async_save", side_effect=OSError("storage unavailable")):
            for action in (lambda: c.async_set_target("living", 66),
                           lambda: c.async_set_range("living", 66, 72),
                           lambda: c.async_resume("living")):
                with self.assertRaises(OSError):
                    await action()
                self.assertEqual(c.effective("living"), before)
        self.assertEqual(await c.store.async_load(), saved)
        restored = await self.manager(self.cfg)
        self.assertEqual(restored.effective("living"), before)

    async def test_hold_reload_and_release_preserve_real_manual_auxiliary_storage(self):
        c = await self.house_manager()
        controls = NativeControls(self.hass, self.cfg, False, "comfort", lambda: None)
        await controls.async_load()
        await controls.async_set_auxiliary_manual(True)
        manual = await controls.store.async_load()
        await c.async_set_target("living", 66)
        await c.async_set_range("living", 66, 72)
        await c.async_hold("living", persistent=True)
        self.now += timedelta(days=3)
        await c.async_close()
        restored = await self.manager(self.cfg)
        restored_controls = NativeControls(self.hass, self.cfg, False, "comfort", lambda: None)
        await restored_controls.async_load()
        self.assertTrue(restored_controls.auxiliary_manual)
        self.assertEqual(restored.effective("living")["mode"], "hold")
        await restored.async_resume("living")
        self.assertEqual(await restored_controls.store.async_load(), manual)
        self.assertTrue(restored_controls.auxiliary_manual)
        self.assertEqual(self.calls, [])

    async def test_assist_button_release_has_scoped_durable_sensor_readback(self):
        c = await self.house_manager()
        await c.async_hold("living", persistent=True)
        await c.async_hold("office", persistent=True)
        # Ownership methods are traps: release must not return the manual Dyson
        # or enable/disable the controller, including when used from MCP.
        controls = SimpleNamespace(auxiliary_manual=True,
            async_set_auxiliary_manual=AsyncMock(side_effect=AssertionError("No ownership change")),
            async_set_enabled=AsyncMock(side_effect=AssertionError("No controller change")))
        runtime = SimpleNamespace(config=self.cfg, comfort=c, controls=controls,
            stop_event=threading.Event(),
            status={}, live=False)
        entry = SimpleNamespace(entry_id="comfort", runtime_data=runtime)
        button = ResumeRoomSchedule(entry, "living")
        button.hass, button.entity_id = self.hass, "button.test_living_resume_schedule"
        sensor = RoomComfortMode(entry, "living")
        sensor.hass, sensor.entity_id = self.hass, "sensor.test_living_comfort_mode"
        climate = RoomClimate(entry, "living")
        climate.hass = self.hass
        self.assertEqual(sensor.native_value, "Hold")
        self.assertIsNone(sensor.extra_state_attributes["override_expires_at"])
        self.hass.states.async_set(button.entity_id, "unknown")
        state = self.hass.states.get(button.entity_id)
        pressed = []
        async def press(call):
            pressed.append(call.data["entity_id"])
            await button.async_press()
        self.hass.services.async_register("button", "press", press)
        handler = OnOffIntentHandler(intent.INTENT_TURN_ON, "homeassistant", "turn_on")
        request = SimpleNamespace(hass=self.hass, context=Context())
        for _ in range(2):
            await handler.async_call_service("homeassistant", "turn_on", request, state)
        self.assertEqual(pressed, [button.entity_id, button.entity_id])
        self.assertEqual(sensor.native_value, "Schedule")
        self.assertEqual(climate.preset_mode, "Schedule")
        self.assertEqual(c.effective("office")["mode"], "hold")
        self.assertTrue(controls.auxiliary_manual)
        controls.async_set_auxiliary_manual.assert_not_awaited()
        controls.async_set_enabled.assert_not_awaited()
        self.assertEqual(self.calls, [])
        restored = await self.manager(self.cfg)
        self.assertEqual(restored.effective("living")["mode"], "schedule")
        self.assertEqual(restored.effective("office")["mode"], "hold")
        await c.async_hold("living", persistent=True)
        with patch.object(type(c.store), "async_save", side_effect=OSError("storage unavailable")):
            with self.assertRaisesRegex(HomeAssistantError, "storage unavailable"):
                await button.async_press()
        self.assertEqual(sensor.native_value, "Hold")
        runtime.stop_event.set()
        self.assertFalse(button.available)
        self.assertFalse(sensor.available)
        await c.async_close()
        with self.assertRaisesRegex(HomeAssistantError, "stopping"):
            await button.async_press()
