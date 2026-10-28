"""Exercise emergency notices through Home Assistant's actual notification API."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.components import persistent_notification
from homeassistant.core import callback

from tests_homeassistant import test_emergency_ac as emergency


class EmergencyNotificationTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only fixture helpers, without inheriting unrelated test methods.
    asyncTearDown = emergency.EmergencyACGuardTests.asyncTearDown
    refresh_safe_measurements = emergency.EmergencyACGuardTests.refresh_safe_measurements
    handle = emergency.EmergencyACGuardTests.handle
    direct_bindings = emergency.EmergencyACGuardTests.direct_bindings
    start = emergency.EmergencyACGuardTests.start
    trip = emergency.EmergencyACGuardTests.trip
    switch_calls = emergency.EmergencyACGuardTests.switch_calls
    wait_for = emergency.EmergencyACGuardTests.wait_for
    prepare_manual_recovery = emergency.EmergencyACGuardTests.prepare_manual_recovery

    async def asyncSetUp(self):
        await emergency.EmergencyACGuardTests.asyncSetUp(self)
        self.assertTrue(await persistent_notification.async_setup(self.hass, {}))
        self.updates = []
        self.group_calls = []

        async def notify_group(call):
            self.group_calls.append(dict(call.data))

        self.hass.services.async_register("notify", "fixture_group", notify_group)

        @callback
        def updated(kind, notifications):
            self.updates.append((kind, dict(notifications)))

        self.remove_notice_listener = persistent_notification.async_register_callback(
            self.hass, updated)
        self.addCleanup(self.remove_notice_listener)
        self.notice_id = "home_climate_heater_emergency_emergency-stage-test"

    @property
    def notices(self):
        return persistent_notification._async_get_or_create_notifications(self.hass)

    @property
    def message(self):
        return self.notices[self.notice_id]["message"]

    def assert_no_automatic_power_on(self):
        self.assertEqual(self.switch_calls(emergency.SHARED_AC, "turn_on"), [])
        self.assertEqual(self.switch_calls(emergency.BASE.heater_switch, "turn_on"), [])

    async def test_normal_guard_publishes_no_emergency_notice(self):
        guard = await self.start()
        for _ in range(3):
            guard._publish()
            guard._observe()
        await self.hass.async_block_till_done()
        self.assertEqual(self.notices, {})
        self.assertEqual(self.updates, [])
        self.assert_no_automatic_power_on()

    async def test_requested_then_confirmed_updates_one_notice_without_poll_spam(self):
        self.ignore_climate = self.hold_ac = True
        guard = await self.start()
        await self.trip(wait=False)
        await self.wait_for(self.ac_entered.is_set)
        await self.wait_for(lambda: self.notice_id in self.notices)
        self.assertEqual(set(self.notices), {self.notice_id})
        requested = self.message.lower()
        self.assertIn("requested", requested)
        self.assertIn("manual", requested)
        self.assertIn("other", requested)
        self.assertIn("ac", requested)
        self.assertIn(guard._emergency_ac_reason, self.message)

        self.ac_release.set()
        await self.hass.async_block_till_done()
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        self.assertIn("confirmed", self.message.lower())
        self.assertEqual(set(self.notices), {self.notice_id})
        additions = [kind for kind, _ in self.updates
                     if kind == persistent_notification.UpdateType.ADDED]
        self.assertEqual(len(additions), 1)
        count = len(self.updates)
        created = self.notices[self.notice_id]["created_at"]
        for _ in range(5):
            guard._publish()
            guard._observe()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.updates), count)
        self.assertEqual(self.notices[self.notice_id]["created_at"], created)
        self.assertEqual(self.group_calls, [])
        self.assert_no_automatic_power_on()

    async def test_unconfirmed_shutdown_stays_visible_until_durable_manual_ack(self):
        self.ignore_climate = self.ignore_ac = True
        guard = await self.start()
        await self.trip()
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "unconfirmed")
        self.assertIn("unconfirmed", self.message.lower())
        self.assertIn("manual", self.message.lower())
        await self.prepare_manual_recovery()
        self.assertIn(self.notice_id, self.notices)
        self.assertTrue(await guard.async_acknowledge_emergency())
        await self.hass.async_block_till_done()
        self.assertNotIn(self.notice_id, self.notices)
        self.assertEqual(self.updates[-1][0], persistent_notification.UpdateType.REMOVED)
        self.assert_no_automatic_power_on()

    async def test_failed_manual_ack_save_does_not_dismiss_notice(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        await self.prepare_manual_recovery()
        guard._store = Mock(async_save=AsyncMock(side_effect=OSError("storage unavailable")))
        self.assertIn(self.notice_id, self.notices)
        with self.assertRaisesRegex(RuntimeError, "Cannot persist manual heater recovery"):
            await guard.async_acknowledge_emergency()
        await self.hass.async_block_till_done()
        self.assertIn(self.notice_id, self.notices)
        self.assertTrue(guard.diagnostics["emergency_ac_latched"])
        self.assertFalse(any(kind == persistent_notification.UpdateType.REMOVED
                             for kind, _ in self.updates))
        self.assert_no_automatic_power_on()

    async def test_cancelled_ack_caller_keeps_notice_until_actual_save_finishes(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        await self.prepare_manual_recovery()
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = guard._store.async_save

        async def held_save(data):
            if not data.get("emergency_ac_latched"):
                entered.set()
                await release.wait()
            await original_save(data)

        guard._store.async_save = held_save
        caller = asyncio.create_task(guard.async_acknowledge_emergency())
        await self.wait_for(entered.is_set)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        try:
            self.assertIn(self.notice_id, self.notices)
            self.assertTrue(guard.diagnostics["emergency_ac_latched"])
            self.assertTrue(guard.blocked)
            for _ in range(3):
                guard._observe()
                await asyncio.sleep(0)
            self.assertIn(self.notice_id, self.notices)
            self.assertFalse(any(kind == persistent_notification.UpdateType.REMOVED
                                 for kind, _ in self.updates))
        finally:
            release.set()
            await guard._recovery_task
            await self.hass.async_block_till_done()
        self.assertNotIn(self.notice_id, self.notices)
        self.assert_no_automatic_power_on()

    async def test_reloaded_guard_reuses_existing_stable_notice(self):
        self.ignore_climate = True
        first = await self.start()
        await self.trip()
        saved = first._saved_state()
        self.assertTrue(await first.async_close())
        self.assertIn(self.notice_id, self.notices)
        additions_before = sum(kind == persistent_notification.UpdateType.ADDED
                               for kind, _ in self.updates)
        second = await self.start(saved=saved)
        self.assertTrue(second.diagnostics["emergency_ac_latched"])
        self.assertEqual(set(self.notices), {self.notice_id})
        self.assertEqual(sum(kind == persistent_notification.UpdateType.ADDED
                             for kind, _ in self.updates), additions_before)
        self.assert_no_automatic_power_on()

    async def test_persisted_emergency_recreates_notice_after_core_restart(self):
        self.ignore_climate = True
        first = await self.start()
        await self.trip()
        saved = first._saved_state()
        self.assertTrue(await first.async_close())
        # HA's persistent-notification store is memory-only. Clearing it models
        # Core startup; the guard's separate durable emergency claim remains.
        persistent_notification.async_dismiss_all(self.hass)
        await self.hass.async_block_till_done()
        self.assertEqual(self.notices, {})
        second = await self.start(saved=saved)
        self.assertTrue(second.diagnostics["emergency_ac_latched"])
        self.assertEqual(set(self.notices), {self.notice_id})
        self.assertIn("manual", self.message.lower())
        self.assert_no_automatic_power_on()

    async def test_configured_group_receives_one_latch_entry_not_status_or_polls(self):
        self.ignore_climate = self.hold_ac = True
        bindings = replace(self.direct_bindings(), emergency_notify_service="notify.fixture_group")
        guard = await self.start(bindings)
        self.assertEqual(self.group_calls, [])
        await self.trip(wait=False)
        await self.wait_for(self.ac_entered.is_set)
        await self.wait_for(lambda: len(self.group_calls) == 1)
        self.assertIn(guard._emergency_ac_reason, self.group_calls[0]["message"])
        self.ac_release.set()
        await self.hass.async_block_till_done()
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        for _ in range(4):
            guard._observe()
            guard._publish()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.group_calls), 1)
        self.assertEqual(set(self.notices), {self.notice_id})
        self.assert_no_automatic_power_on()

    async def test_failed_group_delivery_does_not_delay_shared_cutoff_or_local_notice(self):
        self.ignore_climate = True

        async def failed_group(call):
            self.group_calls.append(dict(call.data))
            raise RuntimeError("Fixture notification delivery unavailable")

        self.hass.services.async_register("notify", "fixture_group", failed_group)
        bindings = replace(self.direct_bindings(), emergency_notify_service="notify.fixture_group")
        guard = await self.start(bindings)
        await self.trip()
        self.assertEqual(self.hass.states.get(emergency.SHARED_AC).state, "off")
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        self.assertEqual(len(self.group_calls), 1)
        self.assertIn(self.notice_id, self.notices)
        self.assert_no_automatic_power_on()

    async def test_unavailable_configured_group_preserves_local_notice_and_cutoff(self):
        self.ignore_climate = True
        bindings = replace(self.direct_bindings(), emergency_notify_service="notify.missing_fixture")
        guard = await self.start(bindings)
        await self.trip()
        self.assertEqual(self.hass.states.get(emergency.SHARED_AC).state, "off")
        self.assertEqual(guard.diagnostics["emergency_ac_status"], "confirmed_off")
        self.assertIn(self.notice_id, self.notices)
        self.assertEqual(self.group_calls, [])
        self.assert_no_automatic_power_on()

    async def test_slow_group_delivery_cannot_block_shared_ac_shutdown(self):
        self.ignore_climate = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_group(call):
            self.group_calls.append(dict(call.data))
            entered.set()
            await release.wait()

        self.hass.services.async_register("notify", "fixture_group", slow_group)
        bindings = replace(self.direct_bindings(), emergency_notify_service="notify.fixture_group")
        guard = await self.start(bindings)
        await self.trip(wait=False)
        try:
            await self.wait_for(entered.is_set)
            await self.wait_for(lambda: guard.diagnostics["emergency_ac_status"] == "confirmed_off")
            self.assertFalse(release.is_set())
            self.assertEqual(self.hass.states.get(emergency.SHARED_AC).state, "off")
            self.assertIn(self.notice_id, self.notices)
        finally:
            release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(len(self.group_calls), 1)
        self.assert_no_automatic_power_on()

    async def test_reconstructed_latch_sends_configured_group_once_after_restart(self):
        self.ignore_climate = True
        bindings = replace(self.direct_bindings(), emergency_notify_service="notify.fixture_group")
        first = await self.start(bindings)
        await self.trip()
        saved = first._saved_state()
        self.assertEqual(len(self.group_calls), 1)
        self.assertTrue(await first.async_close())
        second = await self.start(bindings, saved=saved)
        self.assertEqual(len(self.group_calls), 2)
        for _ in range(4):
            second._observe()
            second._publish()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.group_calls), 2)
        self.assertEqual(set(self.notices), {self.notice_id})
        self.assert_no_automatic_power_on()


if __name__ == "__main__":
    unittest.main()
