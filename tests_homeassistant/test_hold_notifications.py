"""Hold reminders use real HA storage, persistent notices and service registry."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import unittest

from homeassistant.components import persistent_notification

from custom_components.house_climate.controller.notifications import NotificationSettings, NotificationRoute
from custom_components.house_climate.hold_notifications import HoldNotificationManager
from tests_homeassistant import test_comfort_controls as fixtures


class HoldNotificationTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.RoomComfortTests.handle
    manager = fixtures.RoomComfortTests.manager

    async def asyncSetUp(self):
        await fixtures.RoomComfortTests.asyncSetUp(self)
        await persistent_notification.async_setup(self.hass, {})
        self.reminders = []
        self.sent = []
        self.hass.states.async_set("notify.fixture_low", "unknown")
        async def send(call):
            self.sent.append(dict(call.data))
        self.hass.services.async_register("notify", "send_message", send)
        self.route = NotificationSettings(normal=NotificationRoute(
            "notify.send_message", ("notify.fixture_low",)),
            critical=NotificationRoute("notify.fixture_critical", data={"data":{"priority":"high"}},
                                       critical_capable=True))

    async def asyncTearDown(self):
        for reminder in self.reminders:
            await reminder.async_close()
        await fixtures.RoomComfortTests.asyncTearDown(self)

    async def create(self, comfort=None, settings=None):
        comfort = comfort or await self.manager()
        reminder = HoldNotificationManager(self.hass, "comfort", comfort,
                                          self.route if settings is None else settings)
        self.reminders.append(reminder)
        return reminder, comfort

    @property
    def notices(self):
        return persistent_notification._async_get_or_create_notifications(self.hass)

    async def reconcile(self, reminder):
        await reminder.async_reconcile()
        await self.hass.async_block_till_done()

    async def overdue(self, comfort, room="guest"):
        await comfort.async_hold(room, persistent=True)
        self.now += timedelta(hours=24, microseconds=1)

    async def test_strict_threshold_and_only_configured_normal_route(self):
        reminder, comfort = await self.create()
        await comfort.async_hold("guest", persistent=True)
        self.now += timedelta(hours=24)
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notices, {})
        self.now += timedelta(microseconds=1)
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["entity_id"], ["notify.fixture_low"])
        self.assertNotIn("data", self.sent[0])
        self.assertNotIn("URGENT", self.sent[0]["message"])
        self.assertIn(reminder._notice_id("guest"), self.notices)
        self.assertEqual(self.calls, [])  # No climate/fan/device services.

    async def test_hold_edits_preserve_age_restart_suppresses_repeat(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        await comfort.async_set_target("guest", 72)
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 1)
        await reminder.async_close()
        restored = await self.manager()
        replacement, _ = await self.create(restored)
        await self.reconcile(replacement)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(replacement._notice_id("guest"), self.notices)
        await restored.async_resume("guest")
        await self.reconcile(replacement)
        self.assertNotIn(replacement._notice_id("guest"), self.notices)
        await self.overdue(restored)
        await self.reconcile(replacement)
        self.assertEqual(len(self.sent), 2)

    async def test_independent_rooms_and_off_clears_only_its_notice(self):
        reminder, comfort = await self.create()
        await comfort.async_hold("guest", persistent=True)
        await comfort.async_hold("living", persistent=True)
        self.now += timedelta(hours=25)
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.notices), 2)
        await comfort.async_set_protection_only("guest")
        await self.reconcile(reminder)
        self.assertNotIn(reminder._notice_id("guest"), self.notices)
        self.assertIn(reminder._notice_id("living"), self.notices)
        self.assertEqual(self.calls, [])

    async def test_failed_claim_storage_keeps_local_notice_without_external_send(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        with patch.object(comfort.store, "async_save", AsyncMock(side_effect=OSError("disk full"))):
            await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertIn(reminder._notice_id("guest"), self.notices)
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 1)

    async def test_unavailable_route_does_not_consume_claim(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        self.hass.services.async_remove("notify", "send_message")
        await self.reconcile(reminder)
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        async def send(call):
            self.sent.append(dict(call.data))
        self.hass.services.async_register("notify", "send_message", send)
        for state in ("unavailable", "disabled"):
            self.hass.states.async_set("notify.fixture_low", state)
            await self.reconcile(reminder)
            self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        self.hass.states.async_set("notify.fixture_low", "unknown")
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 1)

    async def test_no_normal_route_never_uses_critical(self):
        settings = NotificationSettings(critical=self.route.critical)
        reminder, comfort = await self.create(settings=settings)
        await self.overdue(comfort)
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        self.assertIn(reminder._notice_id("guest"), self.notices)

    async def test_unknown_delivery_is_not_retried_after_restart(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        async def fail(call):
            self.sent.append(dict(call.data))
            raise RuntimeError("uncertain downstream delivery")
        self.hass.services.async_register("notify", "send_message", fail)
        await self.reconcile(reminder)
        restored = await self.manager()
        replacement, _ = await self.create(restored)
        await self.reconcile(replacement)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(restored.overdue_holds()["guest"]["notification_claimed"])

    async def test_manual_dismiss_recreated_without_repeat_push(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        reminder.start()
        await self.hass.async_block_till_done()
        notification_id = reminder._notice_id("guest")
        self.assertIn(notification_id, self.notices)
        persistent_notification.async_dismiss(self.hass, notification_id)
        await self.hass.async_block_till_done()
        self.assertIn(notification_id, self.notices)
        self.assertEqual(len(self.sent), 1)
        await comfort.async_resume("guest")
        reminder.request_reconcile()
        await self.hass.async_block_till_done()
        self.assertNotIn(notification_id, self.notices)

    async def test_restart_cleans_stale_local_notice_even_no_active_hold(self):
        reminder, comfort = await self.create()
        persistent_notification.async_create(self.hass, "old notice", notification_id=reminder._notice_id("guest"))
        await self.reconcile(reminder)
        self.assertEqual(self.notices, {})

    async def test_concurrent_reconciliation_only_dispatches_once(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        await asyncio.gather(reminder.async_reconcile(), reminder.async_reconcile())
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.sent), 1)

    async def test_exit_after_claim_before_dispatch_skips_send_and_clears_notice(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        original = comfort.async_claim_hold_notification
        async def claim_then_exit(room_id, started_at):
            result = await original(room_id, started_at)
            await comfort.async_resume(room_id)
            return result
        with patch.object(comfort, "async_claim_hold_notification", claim_then_exit):
            await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notices, {})

    async def test_critical_capable_normal_slot_is_not_used(self):
        reminder, comfort = await self.create(settings=NotificationSettings(normal=self.route.critical))
        await self.overdue(comfort)
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        self.assertIn(reminder._notice_id("guest"), self.notices)

    async def test_missing_notify_entity_keeps_claim_available(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        self.hass.states.async_remove("notify.fixture_low")
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])

    async def test_timeout_claim_survives_and_does_not_retry(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        async def wait_forever(call):
            self.sent.append(dict(call.data))
            await asyncio.Event().wait()
        self.hass.services.async_register("notify", "send_message", wait_forever)
        with patch("custom_components.house_climate.hold_notifications.SERVICE_TIMEOUT_SECONDS", 0.001):
            await self.reconcile(reminder)
        await self.reconcile(reminder)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("unknown", reminder.delivery["guest"])
        self.assertTrue(comfort.overdue_holds()["guest"]["notification_claimed"])

    async def test_registry_disabled_target_with_stale_state_is_not_dispatched(self):
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)
        entity = registry.async_get_or_create("notify", "fixture", "low", suggested_object_id="fixture_low")
        registry.async_update_entity(entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
        self.hass.states.async_set(entity.entity_id, "unknown")
        reminder, comfort = await self.create(settings=NotificationSettings(
            normal=NotificationRoute("notify.send_message", (entity.entity_id,))))
        await self.overdue(comfort)
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])

    async def test_target_disabled_after_queue_before_dispatch_keeps_claim_no_send(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        original = reminder._send
        queued, release = asyncio.Event(), asyncio.Event()
        async def gated(*args):
            queued.set()
            await release.wait()
            await original(*args)
        with patch.object(reminder, "_send", gated):
            await reminder.async_reconcile()
            await queued.wait()
            self.hass.states.async_set("notify.fixture_low", "disabled")
            release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(self.sent, [])
        self.assertTrue(comfort.overdue_holds()["guest"]["notification_claimed"])
        self.assertEqual(reminder.delivery["guest"], "claimed_route_became_unavailable")
        self.hass.states.async_set("notify.fixture_low", "unknown")
        await self.reconcile(reminder)
        self.assertEqual(self.sent, [])

    async def test_provider_payloads_in_normal_slot_are_not_dispatched(self):
        for data in ({"data": {"priority": "high"}},
                     {"data": {"push": {"sound": {"critical": 1}}}},
                     {"priority": "low"}):
            with self.subTest(data=data):
                reminder, comfort = await self.create(settings=NotificationSettings(
                    normal=NotificationRoute("notify.fixture_provider", data=data)))
                async def send(call):
                    self.sent.append(dict(call.data))
                self.hass.services.async_register("notify", "fixture_provider", send)
                await self.overdue(comfort)
                await self.reconcile(reminder)
                self.assertEqual(self.sent, [])
                self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
                self.assertIn(reminder._notice_id("guest"), self.notices)
                await comfort.async_resume("guest")

    async def test_removed_room_notice_cleanup_preserves_other_namespaces(self):
        reminder, comfort = await self.create()
        removed_id = reminder._notice_id("room_removed_from_configuration")
        protected = [
            "some_other_integration_hold_" + "a" * 24,
            "home_climate_other_entry_hold_" + "a" * 24,
            "home_climate_comfort_hold_" + "a" * 25,
            "home_climate_comfort_hold_" + "a" * 23,
            "home_climate_comfort_hold_" + "g" * 24,
            "home_climate_comfort_hold_" + "a" * 24 + "_other",
            "home_climate_comfort_controller_fault",
        ]
        for notice_id in [removed_id, *protected]:
            persistent_notification.async_create(self.hass, "existing", notification_id=notice_id)
        await self.reconcile(reminder)
        self.assertNotIn(removed_id, self.notices)
        self.assertEqual(set(self.notices), set(protected))

    async def test_swallowed_store_write_error_never_consumes_or_dispatches_claim(self):
        from homeassistant.util.file import WriteError
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        # Exercise the real Store implementation, which catches WriteError and
        # returns normally. The durable comfort wrapper must reject that result.
        with patch.object(comfort.store._io, "async_write",
                          AsyncMock(side_effect=WriteError("simulated disk failure"))):
            await self.reconcile(reminder)
        self.assertEqual(self.sent, [])
        self.assertIn(reminder._notice_id("guest"), self.notices)
        self.assertFalse(comfort.overdue_holds()["guest"]["notification_claimed"])
        await reminder.async_close()
        restored = await self.manager()
        self.assertFalse(restored.overdue_holds()["guest"]["notification_claimed"])
        replacement, _ = await self.create(restored)
        await self.reconcile(replacement)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(restored.overdue_holds()["guest"]["notification_claimed"])
        again = await self.manager()
        self.assertTrue(again.overdue_holds()["guest"]["notification_claimed"])
        await self.reconcile(replacement)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.calls, [])

    async def test_close_waits_for_inflight_verified_claim_without_dispatch(self):
        reminder, comfort = await self.create()
        await self.overdue(comfort)
        entered, release = asyncio.Event(), asyncio.Event()
        original_write = comfort.store._io.async_write
        async def gated_write(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_write(*args, **kwargs)
        with patch.object(comfort.store._io, "async_write", gated_write):
            reminder.request_reconcile()
            await entered.wait()
            closing = asyncio.create_task(reminder.async_close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertTrue(comfort._lock.locked())
            release.set()
            await closing
        self.assertFalse(comfort._lock.locked())
        self.assertTrue(reminder._task.done())
        self.assertEqual(self.sent, [])
        self.assertIn(reminder._notice_id("guest"), self.notices)
        # Cancellation consumes a claim which actually reached durable storage;
        # a restart cannot create a duplicate from an uncertain prior owner.
        restored = await self.manager()
        self.assertTrue(restored.overdue_holds()["guest"]["notification_claimed"])
        replacement, _ = await self.create(restored)
        await self.reconcile(replacement)
        self.assertEqual(self.sent, [])
