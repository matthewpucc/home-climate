"""Exercise dashboard holds with real HA storage and worker settings snapshots."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import Mock, patch

from custom_components.house_climate.comfort import RoomComfort
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.policy import Policy
from tests.test_controller import NIGHT
from tests_homeassistant import test_native as fixtures


class RoomComfortTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.NativeTests.handle
    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.cfg = Config.from_dict(self.cfg.to_dict())
        self.now = NIGHT
        self.comforts = []
        self.addCleanup(patch.stopall)
        patch("custom_components.house_climate.comfort.RoomComfort._now",
              new=staticmethod(lambda: self.now)).start()
        # Deliver HA timer callbacks deterministically while keeping real Store
        # writes and restores. DST boundary selection has separate pure tests.
        self.timer = patch(
            "custom_components.house_climate.comfort.async_track_point_in_utc_time",
            return_value=Mock(),
        ).start()

    async def asyncTearDown(self):
        for comfort in self.comforts:
            await comfort.async_close()
        await fixtures.NativeTests.asyncTearDown(self)

    async def manager(self, config=None, entry_id="comfort"):
        comfort = RoomComfort(self.hass, config or self.cfg, entry_id, Mock(), threading.Event())
        self.comforts.append(comfort)
        await comfort.async_load()
        return comfort

    async def test_desired_temperature_moves_band_within_limits_and_range_clamps_desired(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 74)
        self.assertEqual(comfort.effective("guest")["low_f"], 73.5)
        self.assertEqual(comfort.effective("guest")["high_f"], 74.5)
        await comfort.async_set_target("guest", 75)
        self.assertEqual(comfort.effective("guest")["low_f"], 74)
        self.assertEqual(comfort.effective("guest")["high_f"], 75)
        self.assertEqual(comfort.effective("guest")["target_f"], 75)
        await comfort.async_set_target("guest", 66)
        self.assertEqual(comfort.effective("guest")["low_f"], 66)
        self.assertEqual(comfort.effective("guest")["high_f"], 67)
        await comfort.async_set_range("guest", 70, 71)
        self.assertEqual(comfort.effective("guest")["target_f"], 70)
        await comfort.async_set_range("guest", 68, 69)
        self.assertEqual(comfort.effective("guest")["target_f"], 69)
        await comfort.async_set_range("guest", 68, 70)
        self.assertEqual(comfort.effective("guest")["target_f"], 69)

    async def test_invalid_inputs_never_replace_saved_intent(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 73)
        before = comfort.effective("guest")
        saved = await comfort.store.async_load()
        for low, high in ((65, 74), (70, 76), (72, 72), (74, 70),
                          (True, 74), ("70", 74), (float("nan"), 74), (70, float("inf"))):
            with self.subTest(low=low, high=high):
                with self.assertRaises(ValueError):
                    await comfort.async_set_range("guest", low, high)
        for target in (True, "70", float("nan"), float("inf"), 65, 76):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    await comfort.async_set_target("guest", target)
        with self.assertRaises(ValueError):
            await comfort.async_set_target("missing", 70)
        self.assertEqual(comfort.effective("guest"), before)
        self.assertEqual(await comfort.store.async_load(), saved)

    async def test_auxiliary_cutoff_also_limits_dashboard_edits(self):
        comfort = await self.manager()
        self.assertEqual(comfort.bounds("living"), (66, 70))
        with self.assertRaises(ValueError):
            await comfort.async_set_range("living", 68, 71)
        with self.assertRaises(ValueError):
            await comfort.async_set_target("living", 71)
        await comfort.async_set_target("living", 70)
        effective = comfort.effective("living")
        self.assertEqual((effective["low_f"], effective["high_f"], effective["target_f"]), (69, 70, 70))

    async def test_daytime_edit_and_temporary_preset_enable_a_useful_comfort_band(self):
        self.now = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)
        comfort = await self.manager()
        self.assertFalse(comfort.effective("primary")["comfort_enabled"])
        self.assertEqual(comfort.effective("primary")["low_f"], 69.5)
        self.assertEqual(comfort.effective("primary")["high_f"], 70.5)
        settings = comfort.settings({}, self.cfg, self.now)
        self.assertEqual(Policy(self.cfg).band("primary", settings, self.now)[:2], (66, 75))
        await comfort.async_set_target("primary", 71)
        current = comfort.effective("primary")
        self.assertTrue(current["comfort_enabled"])
        self.assertTrue(current["hold"])
        self.assertEqual((current["low_f"], current["high_f"]), (70.5, 71.5))
        await comfort.async_resume("primary")
        self.assertFalse(comfort.effective("primary")["comfort_enabled"])
        await comfort.async_hold("primary")
        held = comfort.effective("primary")
        self.assertTrue(held["hold"])
        self.assertEqual(held["high_f"] - held["low_f"], 1)

    async def test_inactive_auxiliary_card_stays_inside_editable_limits_without_changing_policy(self):
        room = self.cfg.zones["living"]
        config = replace(self.cfg, zones={**self.cfg.zones, "living": replace(
            room, profile=replace(room.profile, comfort_enabled=False, target_f=74))})
        config.validate()
        comfort = await self.manager(config)
        effective = comfort.effective("living")
        self.assertFalse(effective["comfort_enabled"])
        self.assertFalse(effective["hold"])
        self.assertEqual((effective["low_f"], effective["high_f"], effective["target_f"]),
                         (69, 70, 70))
        settings = comfort.settings({}, config, self.now)
        self.assertEqual(Policy(config).band("living", settings, self.now)[:2], (66, 75))
        self.assertEqual(config.zones["living"].profile.target_f, 74)

    async def test_hold_preserves_schedule_priority_and_fan_percentage(self):
        comfort = await self.manager()
        await comfort.async_set_range("primary", 67.5, 69.5)
        settings = comfort.settings({}, self.cfg, self.now)
        profile = settings.profile(self.cfg.zones["primary"], self.now, self.cfg.timezone,
                                   room_id="primary")
        self.assertEqual((profile.low_f, profile.high_f, profile.target_f), (67.5, 69.5, 68))
        self.assertEqual(profile.priority, 10)
        self.assertEqual(profile.fan_percentage, 60)
        self.assertEqual(comfort.effective("primary")["expires_at"], "2026-10-16T12:00:00+00:00")
        self.assertEqual(self.calls, [], "Editing comfort must not issue equipment commands")

    async def test_valid_overnight_hold_survives_restart_until_boundary(self):
        comfort = await self.manager()
        await comfort.async_set_target("primary", 68.5)
        await comfort.async_close()
        self.now += timedelta(hours=1)
        restored = await self.manager()
        self.assertTrue(restored.effective("primary")["hold"])
        self.assertEqual(restored.effective("primary")["target_f"], 68.5)
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.assertFalse(restored.effective("primary")["hold"])
        self.assertEqual(restored.effective("primary")["high_f"], 70)
        again = await self.manager()
        self.assertFalse(again.effective("primary")["hold"])
        self.assertEqual((await again.store.async_load())["rooms"], {})

    async def test_worker_ignores_expiry_before_timer_can_run(self):
        comfort = await self.manager()
        await comfort.async_set_target("primary", 68.5)
        expiry = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        settings = await asyncio.to_thread(comfort.settings, {}, self.cfg, expiry)
        profile = settings.profile(self.cfg.zones["primary"], expiry, self.cfg.timezone,
                                   room_id="primary")
        self.assertEqual(profile.high_f, 70)
        self.assertEqual(profile.fan_percentage, 0)
        self.assertEqual(profile.priority, 1)
        self.assertNotIn("primary", settings.room_profiles)

    async def test_timer_persists_expiry_notifies_and_wakes_worker(self):
        comfort = await self.manager()
        await comfort.async_set_target("primary", 68.5)
        self.assertEqual(self.timer.call_args.args[2], datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
        scheduled_callback = self.timer.call_args.args[1]
        comfort.notify.reset_mock()
        comfort.wake_event.clear()
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        scheduled_callback(self.now)
        await comfort._expiry_task
        self.assertFalse(comfort.effective("primary")["hold"])
        self.assertEqual((await comfort.store.async_load())["rooms"], {})
        comfort.notify.assert_called_once()
        self.assertTrue(comfort.wake_event.is_set())

    async def test_unscheduled_hold_stays_until_explicit_resume(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 73)
        self.assertIsNone(comfort.effective("guest")["expires_at"])
        self.now += timedelta(days=5)
        self.assertTrue(comfort.effective("guest")["hold"])
        await comfort.async_close()
        restored = await self.manager()
        self.assertEqual(restored.effective("guest")["target_f"], 73)
        await restored.async_resume("guest")
        self.assertFalse(restored.effective("guest")["hold"])
        self.assertEqual(restored.effective("guest")["target_f"], 72)
        self.assertEqual((await restored.store.async_load())["rooms"], {})

    async def test_storage_failure_does_not_publish_unrecoverable_edit(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 73)
        old = comfort.effective("guest")
        comfort.notify.reset_mock()
        comfort.wake_event.clear()
        with patch.object(type(comfort.store), "async_save", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                await comfort.async_set_target("guest", 74)
        self.assertEqual(comfort.effective("guest"), old)
        comfort.notify.assert_not_called()
        self.assertFalse(comfort.wake_event.is_set())
        restored = await self.manager()
        self.assertEqual(restored.effective("guest")["target_f"], 73)

    async def test_changed_room_configuration_discards_its_saved_hold(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 74)
        await comfort.async_set_target("office", 73)
        await comfort.async_close()
        guest = self.cfg.zones["guest"]
        config = replace(self.cfg, zones={**self.cfg.zones, "guest": replace(
            guest, profile=replace(guest.profile, target_f=73))})
        restored = await self.manager(config)
        self.assertFalse(restored.effective("guest")["hold"])
        self.assertEqual(restored.effective("guest")["target_f"], 73)
        self.assertTrue(restored.effective("office")["hold"])

    async def test_removed_rooms_and_malformed_saved_holds_are_discarded(self):
        comfort = await self.manager()
        await comfort.async_set_target("guest", 73)
        data = await comfort.store.async_load()
        value = dict(data["rooms"]["guest"])
        data["rooms"]["missing"] = value
        data["rooms"]["office"] = {**value, "low_f": False}
        data["rooms"]["primary"] = {**value, "expires_at": "2026-10-17T12:00:00"}
        data["rooms"]["living"] = {**value, "high_f": 74}
        await comfort.store.async_save(data)
        await comfort.async_close()
        restored = await self.manager()
        saved = await restored.store.async_load()
        self.assertEqual(list(saved["rooms"]), ["guest"])
        self.assertTrue(restored.effective("guest")["hold"])
        self.assertFalse(restored.effective("office")["hold"])

    async def test_close_cancels_timer_but_keeps_valid_persistent_hold(self):
        comfort = await self.manager()
        await comfort.async_set_target("primary", 68.5)
        remove = self.timer.return_value
        remove.reset_mock()
        await comfort.async_close()
        remove.assert_called_once()
        self.assertIsNone(comfort._remove_expiry)
        with self.assertRaises(ValueError):
            await comfort.async_set_target("primary", 68)
        restored = await self.manager()
        self.assertTrue(restored.effective("primary")["hold"])


if __name__ == "__main__":
    unittest.main()
