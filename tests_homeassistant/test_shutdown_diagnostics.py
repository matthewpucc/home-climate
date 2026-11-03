"""Record guard and outside HA requests through real state/service registries."""
import asyncio
from dataclasses import replace
from unittest.mock import patch
import unittest

from homeassistant.core import Context
from tests_homeassistant import test_emergency_ac as fixtures


class NativeShutdownDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.EmergencyACGuardTests.asyncSetUp
    asyncTearDown = fixtures.EmergencyACGuardTests.asyncTearDown
    handle = fixtures.EmergencyACGuardTests.handle
    refresh_safe_measurements = fixtures.EmergencyACGuardTests.refresh_safe_measurements
    direct_bindings = fixtures.EmergencyACGuardTests.direct_bindings
    start = fixtures.EmergencyACGuardTests.start
    trip = fixtures.EmergencyACGuardTests.trip
    switch_calls = fixtures.EmergencyACGuardTests.switch_calls
    wait_for = fixtures.EmergencyACGuardTests.wait_for

    async def test_autonomous_shared_cut_captures_trigger_commands_and_isolation(self):
        self.ignore_climate = True
        guard = await self.start()
        await self.trip()
        d = guard.shutdown_observations
        self.assertEqual(d.counts['guard_trip'], 1)
        self.assertEqual(d.times['guard_trip'], guard._trip_wall)
        self.assertGreaterEqual(d.counts['guard_requested'], 2)
        shared = [row for row in d.history if row.get('target') == 'shared_ac'
                  and row['event'] == 'guard_completed']
        self.assertTrue(shared)
        self.assertEqual(shared[-1]['initiator'], 'home_climate_guard')
        self.assertIn('40%', shared[-1]['reason'])
        self.assertEqual(shared[-1]['caller'], 'unknown')
        self.assertEqual(d.current['isolation_confirmed'], 1)
        self.assertIsNone(d.current['dedicated_load_w'])
        self.assertIsNone(d.current['aggregate_drop_w'])
        self.assertEqual(len(self.switch_calls(fixtures.SHARED_AC)), 1)

    async def test_real_ha_outside_request_never_claims_assistant_or_service_completion(self):
        guard = await self.start()
        d = guard.shutdown_observations
        before = d.counts['guard_completed']
        self.hass.states.async_set(fixtures.BASE.heater, 'heat', {'hvac_action': 'heating'})
        await self.hass.services.async_call('climate', 'set_hvac_mode',
            {'entity_id': fixtures.BASE.heater, 'hvac_mode': 'off'}, blocking=True,
            context=Context(user_id='shared-authenticated-user'))
        await self.hass.async_block_till_done()
        request = [row for row in d.history if row['event'] == 'ha_off_requested'][-1]
        self.assertEqual(request['initiator'], 'unknown')
        self.assertIsNone(request['reason'])
        self.assertTrue(request['user_context_present'])
        self.assertEqual(d.counts['guard_completed'], before)
        report = [row for row in d.history if row['event'] == 'ha_off_reported'][-1]
        self.assertEqual(report['service_completion'], 'unknown')
        self.assertEqual(report['initiator'], 'unknown')

    async def test_native_ack_can_precede_actual_handler_return(self):
        guard = await self.start()
        release = asyncio.Event()

        async def held(call):
            await self.handle(call)
            await release.wait()

        self.hass.services.async_register('climate', 'set_hvac_mode', held)
        try:
            self.assertTrue(await guard._off_ack(.1))
            d = guard.shutdown_observations
            self.assertEqual(d.counts['guard_native_ack'], 1)
            self.assertEqual(d.counts['guard_completed'], 0)
            self.assertTrue(guard._service_tasks)
        finally:
            release.set()
            await self.hass.async_block_till_done()
            self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        self.assertEqual(d.counts['guard_completed'], 1)

    async def test_late_shared_completion_cancellation_and_failure_are_distinct(self):
        guard = await self.start()
        guard._schedule_check = lambda: None
        self.hold_ac = True
        job = guard._service('switch', 'turn_off', fixtures.SHARED_AC)
        await asyncio.wait_for(self.ac_entered.wait(), 1)
        self.assertEqual(guard.shutdown_observations.counts['guard_completed'], 0)
        job.cancel()  # This fixture intentionally ignores cancellation while dispatch continues.
        await asyncio.sleep(0)
        self.assertEqual(guard.shutdown_observations.counts['guard_cancelled'], 0)
        self.ac_release.set()
        await job
        await self.hass.async_block_till_done()
        self.assertEqual(guard.shutdown_observations.counts['guard_completed'], 1)
        self.assertEqual(guard.shutdown_observations.counts['guard_cancelled'], 0)

    async def test_failed_and_cancelled_jobs_do_not_count_as_success(self):
        guard = await self.start()
        guard._schedule_check = lambda: None

        async def failed(call):
            raise RuntimeError('synthetic handler failure')

        self.hass.services.async_register('climate', 'set_hvac_mode', failed)
        with self.assertRaises(RuntimeError):
            await guard._service('climate', 'set_hvac_mode', fixtures.BASE.heater, hvac_mode='off')
        await self.hass.async_block_till_done()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(call):
            entered.set()
            await release.wait()

        self.hass.services.async_register('climate', 'set_hvac_mode', held)
        job = guard._service('climate', 'set_hvac_mode', fixtures.BASE.heater, hvac_mode='off')
        await asyncio.wait_for(entered.wait(), 1)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        await self.hass.async_block_till_done()
        self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        self.assertEqual(guard.shutdown_observations.counts['guard_failed'], 1)
        self.assertEqual(guard.shutdown_observations.counts['guard_cancelled'], 1)
        self.assertEqual(guard.shutdown_observations.counts['guard_completed'], 0)

    async def test_positive_restore_is_not_counted_and_observer_failure_cannot_block_off(self):
        guard = await self.start(replace(fixtures.BASE, emergency_ac_switch=fixtures.SHARED_AC,
                                         emergency_ac_fallback_enabled=True))
        await guard._service('switch', 'turn_on', fixtures.BASE.heater_switch)
        await self.hass.async_block_till_done()
        self.assertEqual(guard.shutdown_observations.counts['guard_requested'], 0)
        with patch.object(guard.shutdown_observations, 'record', side_effect=RuntimeError('observer failed')):
            await guard._service('climate', 'set_hvac_mode', fixtures.BASE.heater, hvac_mode='off')
            await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(fixtures.BASE.heater).state, 'off')
        self.assertGreater(guard.shutdown_observations.errors, 0)
