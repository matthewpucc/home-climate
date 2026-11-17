"""Durable Hold ages and dispatch claims against real HA storage."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import unittest

from tests_homeassistant import test_comfort_controls as fixtures


class HoldEpisodeTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.RoomComfortTests.handle
    asyncSetUp = fixtures.RoomComfortTests.asyncSetUp
    asyncTearDown = fixtures.RoomComfortTests.asyncTearDown
    manager = fixtures.RoomComfortTests.manager

    async def test_age_strict_boundary_and_edits_preserve_episode(self):
        comfort = await self.manager()
        start = self.now
        await comfort.async_set_target('guest', 70, persistent=True)
        self.now += timedelta(hours=24)
        self.assertEqual(comfort.overdue_holds(), {})
        await comfort.async_set_target('guest', 71)
        await comfort.async_set_range('guest', 69, 73)
        await comfort.async_hold('guest', persistent=True)
        self.now += timedelta(microseconds=1)
        self.assertEqual(comfort.overdue_holds()['guest']['started_at'], start)
        self.assertTrue(await comfort.async_claim_hold_notification('guest', start))
        self.assertFalse(await comfort.async_claim_hold_notification('guest', start))
        await comfort.async_set_target('guest', 70, persistent=True)
        restored = await self.manager()
        self.assertEqual(restored.overdue_holds()['guest'],
                         {'started_at': start, 'notification_claimed': True})
        self.assertFalse(await restored.async_claim_hold_notification('guest', start))
        self.assertEqual(self.calls, [])

    async def test_exit_and_reentry_start_fresh_room_independent_episode(self):
        comfort = await self.manager()
        start = self.now
        await comfort.async_hold('guest', persistent=True)
        await comfort.async_hold('living', persistent=True)
        self.now += timedelta(hours=25)
        self.assertEqual(set(comfort.overdue_holds()), {'guest','living'})
        await comfort.async_set_protection_only('guest')
        self.assertNotIn('guest', comfort.overdue_holds())
        with self.assertRaises(ValueError):
            await comfort.async_set_target('guest', 70, persistent=True)
        await comfort.async_resume('guest')
        await comfort.async_set_target('guest', 70, persistent=True)
        self.assertFalse(await comfort.async_claim_hold_notification('guest', start))
        self.assertNotIn('guest', comfort.overdue_holds())
        self.assertIn('living', comfort.overdue_holds())
        await comfort.async_hold('living')
        self.assertEqual(comfort.overdue_holds(), {})

    async def test_claim_and_edit_failures_do_not_publish(self):
        comfort = await self.manager()
        start = self.now
        await comfort.async_hold('guest', persistent=True)
        self.now += timedelta(hours=25)
        before = await comfort.store.async_load()
        with patch.object(comfort.store, 'async_save', AsyncMock(side_effect=OSError('disk'))):
            with self.assertRaises(OSError):
                await comfort.async_claim_hold_notification('guest', start)
            with self.assertRaises(OSError):
                await comfort.async_resume('guest')
            with self.assertRaises(OSError):
                await comfort.async_set_target('living', 70, persistent=True)
        self.assertFalse(comfort.overdue_holds()['guest']['notification_claimed'])
        self.assertEqual(comfort.effective('living')['mode'], 'schedule')
        self.assertEqual(await comfort.store.async_load(), before)
        self.assertTrue(await comfort.async_claim_hold_notification('guest', start))

    async def test_legacy_missing_invalid_future_naive_age_migrates_once(self):
        comfort = await self.manager()
        await comfort.async_hold('guest', persistent=True)
        data = await comfort.store.async_load()
        for raw in (None, 'garbage', self.now.replace(tzinfo=None).isoformat(),
                    (self.now+timedelta(days=5)).isoformat()):
            data['rooms']['guest']['started_at'] = raw
            data['rooms']['guest'].pop('notification_claimed', None)
            await comfort.store.async_save(data)
            restored = await self.manager()
            self.assertEqual(restored.overdue_holds(), {})
            saved = await restored.store.async_load()
            self.assertEqual(saved['rooms']['guest']['started_at'], self.now.isoformat())
        self.now += timedelta(hours=25)
        again = await self.manager()
        self.assertIn('guest', again.overdue_holds())

    async def test_legacy_migration_failure_does_not_publish_or_dispatch(self):
        comfort = await self.manager()
        await comfort.async_hold('guest', persistent=True)
        data = await comfort.store.async_load()
        del data['rooms']['guest']['started_at']
        await comfort.store.async_save(data)
        from custom_components.house_climate.comfort import RoomComfort
        fresh = RoomComfort(self.hass,self.cfg,'comfort',lambda:None)
        self.comforts.append(fresh)
        with patch.object(fresh.store,'async_save',AsyncMock(side_effect=OSError('disk'))):
            with self.assertRaises(OSError):
                await fresh.async_load()
        self.assertEqual(fresh.overdue_holds(), {})

    async def test_simultaneous_claims_only_one_succeeds(self):
        import asyncio
        comfort = await self.manager()
        start = self.now
        await comfort.async_hold('guest',persistent=True)
        self.now += timedelta(hours=25)
        results = await asyncio.gather(*(comfort.async_claim_hold_notification('guest',start) for _ in range(5)))
        self.assertEqual(results.count(True),1)

    async def test_actual_ha_silent_write_failure_does_not_publish_claim(self):
        from homeassistant.util.file import WriteError
        comfort = await self.manager()
        start = self.now
        await comfort.async_hold('guest',persistent=True)
        self.now += timedelta(hours=25)
        with patch.object(comfort.store._io,'async_write',AsyncMock(side_effect=WriteError('disk'))):
            with self.assertRaises(OSError):
                await comfort.async_claim_hold_notification('guest',start)
        self.assertFalse(comfort.overdue_holds()['guest']['notification_claimed'])
        restored = await self.manager()
        self.assertFalse(restored.overdue_holds()['guest']['notification_claimed'])
        self.assertTrue(await restored.async_claim_hold_notification('guest',start))

    async def test_cancelled_claim_drains_write_before_release_or_new_edit(self):
        import asyncio
        comfort = await self.manager()
        start = self.now
        await comfort.async_hold('guest',persistent=True)
        self.now += timedelta(hours=25)
        entered, release = asyncio.Event(), asyncio.Event()
        write = comfort.store._io.async_write
        async def delayed(*args):
            entered.set()
            await release.wait()
            await write(*args)
        with patch.object(comfort.store._io,'async_write',delayed):
            claim = asyncio.create_task(comfort.async_claim_hold_notification('guest',start))
            await entered.wait()
            claim.cancel()
            edit = asyncio.create_task(comfort.async_resume('guest'))
            await asyncio.sleep(0)
            self.assertFalse(claim.done())
            self.assertFalse(edit.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await claim
            await edit
        restored = await self.manager()
        self.assertEqual(restored.effective('guest')['mode'],'schedule')
        self.assertEqual(restored.overdue_holds(),{})
