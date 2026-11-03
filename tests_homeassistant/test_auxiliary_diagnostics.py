"""Observe real native command lifetimes without changing their safety gates."""
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.house_climate.auxiliary_diagnostics import HISTORY_LIMIT
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.diagnostics import async_get_config_entry_diagnostics
from tests_homeassistant import test_auxiliary_delayed_off as delayed


class AuxiliaryDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = delayed.AuxiliaryDelayedOffTests.asyncSetUp
    asyncTearDown = delayed.AuxiliaryDelayedOffTests.asyncTearDown
    handle = delayed.AuxiliaryDelayedOffTests.handle
    until = delayed.AuxiliaryDelayedOffTests.until
    runtime = delayed.AuxiliaryDelayedOffTests.runtime
    claim = delayed.AuxiliaryDelayedOffTests.claim
    settle_watchdog = delayed.AuxiliaryDelayedOffTests.settle_watchdog
    timed_runtime = delayed.AuxiliaryDelayedOffTests.timed_runtime
    controlled_clock = delayed.AuxiliaryDelayedOffTests.controlled_clock
    set_device_mode = delayed.AuxiliaryDelayedOffTests.set_device_mode
    delayed_off = delayed.AuxiliaryDelayedOffTests.delayed_off
    feedback = delayed.AuxiliaryDelayedOffTests.feedback

    async def test_renewal_deadline_and_observed_expiry_are_distinct(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        d = c.observations
        self.assertEqual(d.counts['authorization_renewed'], 1)
        renewed = d.times['authorization_renewed']
        c._publish()
        c.confirm_heartbeat()
        self.assertEqual(d.times['authorization_renewed'], renewed)
        self.assertEqual(d.counts['authorization_renewed'], 1)
        values, _ = d.values(c, 220)
        self.assertEqual(values['authorization_valid'], 1)
        self.assertEqual(values['authorization_deadline_at'], renewed + 120)
        self.assertEqual(d.values(c, 221)[0]['authorization_valid'], 0)
        self.assertEqual(d.counts['authorization_expired'], 0)  # Read did not latch.
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertEqual(d.counts['authorization_expired'], 1)
        self.assertIsNotNone(d.times['authorization_expired'])
        self.assertEqual(d.times['authorization_renewed'], renewed)

    async def test_worker_request_dispatch_return_and_ack_order(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        d = c.observations
        initial = dict(d.counts)
        queued, dispatch, entered, finish = (asyncio.Event() for _ in range(4))
        original = runtime.api._async_service_job

        async def held_queue(*args, **kwargs):
            queued.set()
            await dispatch.wait()
            return await original(*args, **kwargs)

        async def held_handler(call):
            await self.handle(call)
            entered.set()
            await finish.wait()

        self.hass.services.async_register('climate', 'set_hvac_mode', held_handler)
        with patch.object(runtime.api, '_async_service_job', new=held_queue):
            command = asyncio.create_task(asyncio.to_thread(
                runtime.api.service, 'climate', 'set_hvac_mode',
                entity_id=runtime.config.heater_entity, hvac_mode='off'))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                self.assertEqual(d.counts['off_requested'], initial['off_requested'] + 1)
                self.assertEqual(d.counts['off_dispatched'], initial['off_dispatched'])
                self.assertEqual(d.values(c, clock.value)[0]['pending_jobs'], 1)
                dispatch.set()
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(d.counts['off_dispatched'], initial['off_dispatched'] + 1)
                self.assertEqual(d.counts['off_completed'], initial['off_completed'])
                self.assertFalse(c._ack_auxiliary_heartbeat_off())
                self.assertEqual(d.counts['off_acknowledged'], 0)
                self.assertEqual(d.values(c, clock.value)[0]['ownership_retained'], 1)
            finally:
                dispatch.set()
                finish.set()
                await command
                self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        self.assertEqual(d.counts['off_completed'], initial['off_completed'] + 1)
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertEqual(d.counts['off_acknowledged'], 1)
        c._auxiliary_heat_possible = True  # Rechecking the same receipt does not count twice.
        c._ack_auxiliary_heartbeat_off()
        self.assertEqual(d.counts['off_acknowledged'], 1)
        events = [row['event'] for row in d.history]
        self.assertLess(events.index('off_completed'), events.index('off_acknowledged'))
        self.assertEqual(d.history[-1]['proof'], 'ha_report')
        self.assertGreater(d.ack_reported_at, d.history[-1]['dispatched_at'])
        self.assertLessEqual(d.ack_reported_at, d.times['off_acknowledged'])

    async def test_watchdog_and_idle_service_are_counted_without_receipts(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        d = c.observations
        initial = dict(d.counts)
        await c._off(runtime.config.heater_entity)
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertEqual(d.counts['off_requested'], initial['off_requested'] + 1)
        self.assertEqual(d.counts['off_dispatched'], initial['off_dispatched'] + 1)
        self.assertEqual(d.counts['off_completed'], initial['off_completed'] + 1)
        self.assertEqual(d.history[-2]['source'], 'watchdog')
        self.assertIsNone(c.auxiliary_off_started())
        await asyncio.to_thread(runtime.api.service, 'climate', 'set_hvac_mode',
                                entity_id=runtime.config.heater_entity, hvac_mode='off')
        self.assertEqual(d.counts['off_dispatched'], initial['off_dispatched'] + 2)
        self.assertEqual(d.counts['off_completed'], initial['off_completed'] + 2)
        self.assertEqual(d.counts['off_acknowledged'], 1)

    async def test_stale_callback_keeps_receipt_ownership_and_cannot_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        old = c._auxiliary_heartbeat_off_receipt
        c.auxiliary_activation_started({'hvac_mode': 'heat'})
        c.auxiliary_off_started()
        c.auxiliary_activation_finished()
        c.auxiliary_off_finished(old)
        self.set_device_mode(runtime.config.heater_entity, 'off')
        self.assertFalse(c._ack_auxiliary_heartbeat_off())
        d = c.observations
        self.assertEqual(d.counts['off_stale_callback'], 1)
        self.assertEqual(d.counts['off_acknowledged'], 0)
        self.assertLess(d.history[-1]['receipt_epoch'], d.history[-1]['stop_epoch'])
        self.assertEqual(d.values(c, clock.value)[0]['stop_pending'], 1)

    async def test_late_callback_after_new_activation_is_observed_without_pending_stop(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        old = c._auxiliary_heartbeat_off_receipt
        c.auxiliary_activation_started({'hvac_mode': 'heat'})
        self.assertFalse(c._auxiliary_stop_pending)
        c.auxiliary_off_finished(old)
        self.assertEqual(c.observations.counts['off_stale_callback'], 1)
        self.assertEqual(c.observations.counts['off_acknowledged'], 0)
        self.assertFalse(c._auxiliary_stop_pending)
        self.assertIsNone(c._auxiliary_heartbeat_off_receipt)
        c.auxiliary_activation_finished()

    async def test_failed_service_is_not_completion_or_acknowledgement(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        d = c.observations
        completed = d.counts['off_completed']

        async def failure(call):
            await self.handle(call)
            raise RuntimeError('synthetic failure')

        self.hass.services.async_register('climate', 'set_hvac_mode', failure)
        try:
            with self.assertRaises(HAError):
                await asyncio.to_thread(runtime.api.service, 'climate', 'set_hvac_mode',
                                        entity_id=runtime.config.heater_entity, hvac_mode='off')
        finally:
            self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        self.assertEqual(d.counts['off_failed'], 1)
        self.assertEqual(d.counts['off_completed'], completed)
        self.assertEqual(d.counts['off_acknowledged'], 0)

    async def test_guard_delegation_and_isolation_do_not_claim_native_completion(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        d = c.observations
        before = dict(d.counts)

        async def guard_stop():
            return True

        guard = SimpleNamespace(emergency_fallback_enabled=True, power_cut_confirmed=True,
                                async_request_heater_off=guard_stop, shutdown_confirmed=True)
        with patch.object(c, 'power_guard', guard), patch.object(runtime.api, 'power_guard', guard):
            await c._off(runtime.config.heater_entity)
            self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertEqual(d.counts['off_dispatched'], before['off_dispatched'])
        self.assertEqual(d.counts['off_completed'], before['off_completed'])
        self.assertEqual(d.counts['off_delegated'], 1)
        self.assertEqual(d.counts['off_delegate_completed'], 1)
        self.assertEqual(d.counts['off_acknowledged'], 0)
        self.assertEqual(d.counts['off_isolated'], 1)

    async def test_report_quality_does_not_reuse_historical_ack_as_current_evidence(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, 'off')
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        acknowledged = c.observations.times['off_acknowledged']
        reported = self.hass.states.get(runtime.config.heater_entity).last_reported
        for kwargs, quality in (
            ({'attributes': {'restored': True}}, 'restored'),
            ({'attributes': {'assumed_state': True}}, 'assumed'),
            ({'state': 'unknown'}, 'unavailable'),
            ({'state': 'unavailable'}, 'unavailable'),
            ({'reported': reported - timedelta(seconds=181)}, 'stale'),
            ({'reported': reported + timedelta(seconds=60)}, 'future'),
        ):
            with self.subTest(quality=quality), self.feedback(runtime, **kwargs):
                values, attributes = c.observations.values(c, clock.value)
                self.assertIsNone(values['reported_off'])
                self.assertEqual(attributes['report_quality'], quality)
                self.assertEqual(values['off_acknowledged_at'], acknowledged)
        self.assertEqual(c.observations.values(c, clock.value)[0]['reported_off'], 1)

    async def test_restart_keeps_cleanup_obligation_but_does_not_restore_evidence(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        restarted = NativeControls(self.hass, runtime.config, True, 'native', lambda: None)
        await restarted.async_load()
        values, _ = restarted.observations.values(restarted, clock.value)
        self.assertEqual(values['ownership_retained'], 1)
        self.assertEqual(values['off_completed_count'], 0)
        self.assertIsNone(values['off_completed_at'])
        self.assertIsNone(values['authorization_valid'])
        self.assertIsNone(values['authorization_deadline_at'])
        self.assertNotEqual(c.observations.started_at, restarted.observations.started_at)

    async def test_bounded_history_and_diagnostic_failures_do_not_block_off(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        for _ in range(100):
            c.confirm_auxiliary_authorization()
        download = await async_get_config_entry_diagnostics(self.hass, SimpleNamespace(runtime_data=runtime))
        history = download['auxiliary_observations']['recent_events']
        self.assertEqual(len(history), HISTORY_LIMIT)
        self.assertEqual(c.observations.counts['authorization_renewed'], 101)
        self.assertNotIn('entity_id', json.dumps(download))
        with patch.object(c.observations, '_record', side_effect=RuntimeError('diagnostic failure')):
            await c._off(runtime.config.heater_entity)
        self.assertEqual(self.hass.states.get(runtime.config.heater_entity).state, 'off')
        self.assertGreater(c.observations.errors, 0)

    async def test_reading_diagnostics_has_no_control_side_effects(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        before = (dict(c._snapshot), c._auxiliary_stop_epoch, list(self.calls), c.observations.sequence)
        with patch.object(c, '_ack_auxiliary_heartbeat_off', side_effect=AssertionError('mutating read')), \
             patch.object(c, '_is_off', side_effect=AssertionError('mutating read')), \
             patch.object(c, '_check_auxiliary_heartbeat_deadline', side_effect=AssertionError('mutating read')):
            for _ in range(20):
                c.observations.values(c, 1000)
        self.assertEqual(before, (dict(c._snapshot), c._auxiliary_stop_epoch, list(self.calls), c.observations.sequence))

    async def test_alternating_stop_proof_does_not_recount_the_same_receipt(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        await c._off(runtime.config.heater_entity)
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        receipt = c._auxiliary_heartbeat_off_receipt
        acknowledged = c.observations.times['off_acknowledged']
        guard = SimpleNamespace(power_cut_confirmed=True)
        with patch.object(c, 'power_guard', guard), self.feedback(runtime, state='unavailable'):
            self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertEqual(c._auxiliary_heartbeat_off_receipt, receipt)
        self.assertEqual(c.observations.counts['off_acknowledged'], 1)
        self.assertEqual(c.observations.counts['off_isolated'], 1)
        self.assertEqual(c.observations.times['off_acknowledged'], acknowledged)

    async def test_queued_old_owner_is_recorded_without_dispatch(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        old_generation = c.command_generation(heater)
        c.ownership_generation += 1
        dispatched = c.observations.counts['off_dispatched']
        with self.assertRaises(HAError):
            await runtime.api._async_request(
                'services/climate/set_hvac_mode', {'entity_id': heater, 'hvac_mode': 'off'},
                clock.value + 10, generation=old_generation)
        await self.hass.async_block_till_done()
        self.assertEqual(c.observations.counts['off_dispatched'], dispatched)
        self.assertEqual(c.observations.counts['off_not_dispatched'], 1)
        rejected = c.observations.history[-1]
        self.assertEqual(rejected['request_generation'], old_generation)
        self.assertNotEqual(rejected['generation'], old_generation)

    async def test_uncertain_command_is_not_counted_as_a_running_job(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        c.activation_job_uncertain(runtime.config.heater_entity)
        try:
            values, _ = c.observations.values(c, clock.value)
            self.assertEqual(values['pending_jobs'], 0)
            self.assertEqual(values['command_uncertain'], 1)
            self.assertEqual(values['ownership_retained'], 1)
        finally:
            c._uncertain_device_jobs.clear()

    async def test_owned_cleanup_is_visible_until_release(self):
        runtime, switch = await self.runtime()
        await self.claim(runtime)
        c = runtime.controls
        self.assertEqual(c.observations.values(c, 0)[0]['ownership_retained'], 1)
        await switch.async_turn_off()
        await self.settle_watchdog(c)
        await self.until(lambda: not c.owned)
        self.assertEqual(c.observations.values(c, 0)[0]['ownership_retained'], 0)

    async def test_cancelled_handler_remains_distinct_from_successful_return(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        entered, finish = asyncio.Event(), asyncio.Event()
        completed = c.observations.counts['off_completed']

        async def held(call):
            entered.set()
            await finish.wait()

        self.hass.services.async_register('climate', 'set_hvac_mode', held)
        request = asyncio.create_task(runtime.api._async_request(
            'services/climate/set_hvac_mode',
            {'entity_id': runtime.config.heater_entity, 'hvac_mode': 'off'}, clock.value + 10,
            generation=c.command_generation(runtime.config.heater_entity)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            job = next(task for task, operation in runtime.api._service_jobs.items()
                       if operation.entity == runtime.config.heater_entity)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await self.hass.async_block_till_done()
            self.assertEqual(c.observations.counts['off_cancelled'], 1)
            self.assertEqual(c.observations.counts['off_completed'], completed)
            self.assertEqual(c.observations.counts['off_acknowledged'], 0)
        finally:
            finish.set()
            self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
