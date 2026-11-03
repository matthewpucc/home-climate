"""Takeover abandons preset matching, not actual commands or fresh stop proof."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.switch import ControllerEnabled
from custom_components.house_climate.controller.ha import HAError, HAStopping
from custom_components.house_climate import handoff
from tests_homeassistant import test_lifecycle_policy as lifecycle
from tests_homeassistant import test_native as fixtures


class ControllerTakeoverTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    async def asyncTearDown(self):
        # Finish synthetic fallback obligations before HA removes its temp store.
        # Tests restore their deliberately failed handlers before reaching here.
        for runtime in self.runtimes:
            c = runtime.controls
            if c is None or not c.active:
                continue
            if c.handoff and c.handoff.get('interrupted'):
                # Supply the operator's fresh, correct synthetic preset report
                # so intentionally retained restart recovery can finish.
                for entity, request in c.handoff['requests'].items():
                    state = self.hass.states.get(entity)
                    self.hass.states.async_set(entity, 'heat_cool', {
                        **state.attributes, **request['data']})
            for entity in c.config.heads.values():
                state = self.hass.states.get(entity)
                self.hass.states.async_set(entity, state.state, {
                    **state.attributes, 'hvac_modes': ['off', 'heat', 'cool', 'heat_cool']})
            await c.async_set_enabled(False)
            async with asyncio.timeout(3):
                while c.owned:
                    await c.async_check()
                    await asyncio.sleep(.01)
        await fixtures.NativeTests.asyncTearDown(self)
    handle = lifecycle.LifecyclePolicyTests.handle
    until = lifecycle.LifecyclePolicyTests.until
    runtime = lifecycle.LifecyclePolicyTests.runtime
    claim = lifecycle.LifecyclePolicyTests.claim
    settle_watchdog = lifecycle.LifecyclePolicyTests.settle_watchdog
    fallback_runtime = lifecycle.LifecyclePolicyTests.fallback_runtime

    async def failed_preset(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        c = runtime.controls
        await self.settle_watchdog(c)
        c.enabled = c.enabled_intent = False
        c.recovering = True
        plan = c.handoff
        plan['stage'] = 'applying'
        head = runtime.config.heads['living']
        plan['requests'][head] = {'at': handoff.now() - 121, 'dispatched': True,
            'data': {'hvac_mode': 'heat_cool', 'target_temp_low': 68, 'target_temp_high': 73}}
        plan['completed'] = [e for e in (*plan['heads'], *plan['fans']) if e != head]
        plan['problems'][head] = 'Recovery outcome is unconfirmed'
        self.hass.states.async_set(head, 'heat_cool', {
            **self.hass.states.get(head).attributes, 'target_temp_low': 68, 'target_temp_high': 68})
        await c._save(c.owned, c.pending_fans)
        await self.settle_watchdog(c)
        return runtime, c, head

    async def drain(self, c):
        async with asyncio.timeout(3):
            while c.takeover_pending:
                await c.async_check()
                await asyncio.sleep(.01)

    async def reload_controls(self, runtime, *, live=True):
        old = runtime.controls
        await self.settle_watchdog(old)
        old.active = False
        for unsubscribe in old._unsubscribers:
            unsubscribe()
        old._unsubscribers.clear()
        c = NativeControls(self.hass, runtime.config, live, 'native', lambda: None)
        await c.async_load()
        runtime.controls = runtime.api.controls = c
        c.async_start()
        return c

    async def test_on_supersedes_wrong_range_then_uses_normal_acquisition(self):
        runtime, c, head = await self.failed_preset()
        before = len(self.calls)
        await c.async_set_enabled(True)
        self.assertTrue(c.enabled)
        self.assertTrue(c.takeover_pending)
        self.assertFalse(c._snapshot['enabled'])
        await self.drain(c)
        self.assertFalse(c.owned)
        self.assertIsNone(c.handoff)
        self.assertTrue(c.enabled)
        self.assertEqual(self.hass.states.get(head).state, 'off')
        self.assertFalse(any(service == 'set_temperature' for _, service, _ in self.calls[before:]))
        await self.claim(runtime)
        self.assertEqual(c.handoff['stage'], 'armed')
        self.assertIn(head, c.owned)

    async def test_pending_native_job_prevents_takeover_and_stale_worker_release_does_not_cancel_on(self):
        _, c, head = await self.failed_preset()
        pending = True
        c.pending_device_jobs = lambda entities: (object(),) if pending and head in entities else ()
        await c.async_set_enabled(True)
        await c.async_service('input_boolean', 'turn_off', {'cause': 'disabled'})
        await c.async_check()
        self.assertTrue(c.enabled)
        self.assertTrue(c.takeover_pending)
        self.assertIn(head, c.owned)
        with self.assertRaises(HAError):
            c.check_activation(head, {'hvac_mode': 'heat'})
        pending = False
        await self.drain(c)

    async def test_no_fresh_off_report_remains_blocked_even_after_service_returns(self):
        _, c, head = await self.failed_preset()
        self.hass.states.async_set(head, 'off', dict(self.hass.states.get(head).attributes))
        async def no_report(call):
            if call.data.get('entity_id') != head:
                await self.handle(call)
        self.hass.services.async_register('climate', 'set_hvac_mode', no_report)
        await c.async_set_enabled(True)
        await c.async_check()
        self.assertTrue(c.takeover_pending)
        self.assertIn(head, c.owned)
        self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        await self.drain(c)

    async def test_repeated_on_does_not_reset_receipts_or_replay_presets(self):
        _, c, _ = await self.failed_preset()
        with patch.object(c, '_schedule_check'):
            await c.async_set_enabled(True)
            generation = c.ownership_generation
            await c.async_set_enabled(True)
            self.assertEqual(generation, c.ownership_generation)
        await self.drain(c)

    async def test_failed_enable_save_is_stopped_and_preserves_cleanup(self):
        _, c, head = await self.failed_preset()
        with patch.object(c.store, 'async_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await c.async_set_enabled(True)
            self.assertFalse(c.enabled)
            self.assertFalse(c.enabled_intent)
            self.assertIn(head, c.owned)
            self.assertTrue(c.takeover_pending)
        await c.async_check()

    async def test_restart_saved_on_supersedes_interrupted_preset_only_with_live(self):
        runtime, c, head = await self.failed_preset()
        c.enabled_intent = True
        await c._save(c.owned, c.pending_fans)
        c = await self.reload_controls(runtime)
        await c.async_check()
        await self.drain(c)
        self.assertTrue(c.enabled)
        self.assertTrue(c.enabled_intent)
        self.assertFalse(c.recovering)
        self.assertIsNone(c.handoff)
        self.assertEqual(self.hass.states.get(head).state, 'off')

    async def test_shadow_restart_retains_fallback_instead_of_auto_takeover(self):
        runtime, c, _ = await self.failed_preset()
        c.enabled_intent = True
        await c._save(c.owned, c.pending_fans)
        c = await self.reload_controls(runtime, live=False)
        await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c.takeover_pending)
        self.assertIsNotNone(c.handoff)
        self.assertTrue(c.recovering)

    async def test_saved_on_is_visible_and_can_be_cancelled_in_shadow(self):
        runtime, _ = await self.runtime(live=False)
        c = runtime.controls
        await self.settle_watchdog(c)
        c.enabled_intent = True
        await c._save((), ())
        c = await self.reload_controls(runtime, live=False)
        switch = ControllerEnabled(SimpleNamespace(entry_id='native', runtime_data=runtime))
        switch.hass = self.hass
        self.assertTrue(switch.is_on)
        self.assertFalse(c._snapshot['enabled'])
        self.assertEqual(self.calls, [])
        await switch.async_turn_off()
        self.assertFalse(switch.is_on)
        self.assertFalse((await c.store.async_load())['enabled_intent'])
        c = await self.reload_controls(runtime, live=True)
        await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)
        self.assertEqual(self.calls, [])

    async def test_explicit_off_cancels_pending_startup_resume(self):
        runtime, c, _ = await self.failed_preset()
        c.enabled_intent = True
        await c._save(c.owned, c.pending_fans)
        with patch.object(NativeControls, '_schedule_check'):
            c = await self.reload_controls(runtime)
            await c.async_set_enabled(False)
        await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)

    async def test_legacy_storage_never_infers_on_from_ownership(self):
        runtime, c, _ = await self.failed_preset()
        saved = await c.store.async_load()
        saved.pop('enabled_intent')
        await c.store.async_save(saved)
        c = await self.reload_controls(runtime)
        await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)

    async def test_graceful_close_preserves_on_but_operational_fault_does_not(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await runtime.async_stop()
        self.assertTrue((await runtime.controls.store.async_load())['enabled_intent'])
        c = runtime.controls
        c.pause('synthetic worker fault')
        await c.async_check()
        await c.async_close()
        self.assertFalse((await c.store.async_load())['enabled_intent'])

    async def test_uncertain_fan_call_is_not_erased_with_preset(self):
        _, c, _ = await self.failed_preset()
        fan = next(iter(c.config.fans.values()))
        c.handoff['requests'][fan] = {'at': handoff.now(), 'data': {'percentage': 40},
                                       'dispatched': True, 'uncertain': True}
        await c.async_set_enabled(True)
        await c.async_check()
        self.assertTrue(c.takeover_pending)
        self.assertIn(fan, c._uncertain_device_jobs)
        self.assertIn(fan, (await c.store.async_load())['uncertain_devices'])
        # End this synthetic unknown-handler scenario only after proving it
        # blocks admission and remains durable; no production reset is called.
        c._uncertain_device_jobs.remove(fan)
        c.handoff['requests'][fan]['uncertain'] = False

    async def test_manual_auxiliary_survives_takeover_without_any_auxiliary_command(self):
        runtime, c, _ = await self.failed_preset()
        heater = c.config.heater_entity
        c.auxiliary_manual = True
        c.auxiliary_manual_entity = heater
        c.owned = tuple(e for e in c.owned if e != heater)
        before = len(self.calls)
        await c.async_set_enabled(True)
        await self.drain(c)
        await self.claim(runtime)
        self.assertTrue(c.auxiliary_manual)
        self.assertNotIn(heater, c.owned)
        self.assertFalse(any(data.get('entity_id') == heater for _, _, data in self.calls[before:]))

    async def test_dispatched_late_preset_must_finish_then_receive_fresh_off(self):
        _, c, head = await self.failed_preset()
        entered, release = asyncio.Event(), asyncio.Event()
        async def late_preset():
            c.head_activation_started(head)
            entered.set()
            try:
                await release.wait()
                self.hass.states.async_set(head, 'heat_cool', dict(self.hass.states.get(head).attributes))
            finally:
                c.head_activation_finished(head)
        task = asyncio.create_task(late_preset())
        c._handoff_jobs[head] = task
        await entered.wait()
        try:
            await c.async_set_enabled(True)
            await c.async_check()
            self.assertTrue(c.takeover_pending)
            self.assertIn(head, c.owned)
            self.assertFalse(c._snapshot['enabled'])
            release.set()
            await task
            before = len(self.calls)
            await self.drain(c)
            self.assertTrue(any(service == 'set_hvac_mode' and data.get('entity_id') == head
                                and data.get('hvac_mode') == 'off' for _, service, data in self.calls[before:]))
        finally:
            release.set()
            await task

    async def test_unavailable_head_still_blocks_takeover(self):
        _, c, head = await self.failed_preset()
        original = self.handle
        async def unavailable(call):
            if call.data.get('entity_id') == head:
                return
            await original(call)
        self.hass.services.async_register('climate', 'set_hvac_mode', unavailable)
        self.hass.states.async_set(head, 'unavailable')
        await c.async_set_enabled(True)
        await c.async_check()
        self.assertTrue(c.takeover_pending)
        self.assertFalse(c._snapshot['enabled'])
        self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
        await self.drain(c)

    async def test_explicit_off_during_takeover_keeps_terminal_fallback(self):
        _, c, _ = await self.failed_preset()
        with patch.object(c, '_schedule_check'):
            await c.async_set_enabled(True)
            await c.async_set_enabled(False)
        await self.drain(c)
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)
        self.assertIsNotNone(c.handoff)
        self.assertIn(c.handoff['stage'], ('armed', 'stopping', 'settling'))

    async def test_normal_worker_restarts_after_wrong_preset_without_matching_it(self):
        runtime, c, _ = await self.failed_preset()
        runtime.async_start()
        await c.async_set_enabled(True)
        await self.until(lambda: runtime.status.get('event') == 'live'
                         and self.hass.states.get(runtime.config.heads['guest']).state == 'cool')
        self.assertFalse(c.takeover_pending)
        self.assertTrue(c.owned)
        self.assertEqual(runtime.fault_count, 0)

    async def test_fault_during_enable_save_cannot_revive_controller(self):
        runtime, _ = await self.runtime()
        c = runtime.controls
        original = c.store.async_save
        entered, release = asyncio.Event(), asyncio.Event()
        async def save(data):
            entered.set()
            await release.wait()
            await original(data)
        with patch.object(c, '_schedule_check'), patch.object(c.store, 'async_save', side_effect=save):
            task = asyncio.create_task(c.async_set_enabled(True))
            await entered.wait()
            c.pause('synthetic fault while saving On')
            release.set()
            await task
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)
        await c.async_check()
        self.assertFalse((await c.store.async_load())['enabled_intent'])

    async def test_fault_before_startup_cancels_saved_resume(self):
        runtime, _ = await self.runtime()
        c = runtime.controls
        c.enabled_intent = True
        c._resume_requested = True
        c.pause('synthetic startup fault')
        await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c._resume_requested)
        self.assertFalse((await c.store.async_load())['enabled_intent'])

    async def test_heartbeat_fault_is_durable_before_check_returns(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        c = runtime.controls
        await self.settle_watchdog(c)
        with patch.object(c, '_schedule_check'):
            c.heartbeat_monotonic = time.monotonic() - c.config.heartbeat_timeout_seconds - 1
            await c.async_check()
        self.assertFalse(c.enabled)
        self.assertFalse(c.enabled_intent)
        self.assertFalse((await c.store.async_load())['enabled_intent'])

    async def test_queued_actual_handoff_is_fenced_by_new_on(self):
        runtime, controls, head = await self.failed_preset()
        gate = asyncio.Event()
        original = self.hass.async_create_background_task
        task = None

        def delayed(coro, name, **kwargs):
            async def wrapper():
                await gate.wait()
                return await coro
            return original(wrapper() if name == 'Home Climate recovery setting' else coro,
                            name, **kwargs)

        with patch.object(controls, '_schedule_check'):
            controls.handoff['requests'].pop(head)
            before = len(self.calls)
            with patch.object(self.hass, 'async_create_background_task', side_effect=delayed):
                # The real handoff schedules its actual dispatch closure; its bounded
                # wait returns while that closure is still queued behind our gate.
                await controls.async_check()
            task = controls._handoff_jobs[head]
            try:
                self.assertFalse(task.done())
                await controls.async_set_enabled(True)
                gate.set()
                await task
                self.assertFalse(any(service == 'set_temperature'
                                     for _, service, _ in self.calls[before:]))
                await self.drain(controls)
            finally:
                gate.set()
                await task

    async def test_stale_worker_release_after_new_claim_does_not_revoke_on(self):
        runtime, controls, _ = await self.failed_preset()
        await controls.async_set_enabled(True)
        await self.drain(controls)
        await self.claim(runtime)
        epoch = controls.ownership_generation
        for cause in ('disabled', 'ownership_recovery'):
            await controls.async_service('input_boolean', 'turn_off', {'cause': cause})
            self.assertTrue(controls.enabled)
            self.assertTrue(controls.enabled_intent)
            self.assertEqual(controls.ownership_generation, epoch)

    async def test_old_mapping_head_is_not_discarded_during_on_cleanup(self):
        _, c, _ = await self.failed_preset()
        old_head = 'climate.previous_fixture_head'
        c.owned = (*c.owned, old_head)
        self.hass.states.async_set(old_head, 'heat')
        async def ignored_old(call):
            if call.data.get('entity_id') != old_head:
                await self.handle(call)
        self.hass.services.async_register('climate', 'set_hvac_mode', ignored_old)
        try:
            await c.async_set_enabled(True)
            await c.async_check()
            self.assertTrue(c.enabled)
            self.assertTrue(c.takeover_pending)
            self.assertIn(old_head, c.owned)
            self.assertFalse(c._snapshot['enabled'])
        finally:
            self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
            await self.drain(c)

    async def test_restored_or_assumed_off_does_not_finish_takeover(self):
        _, c, head = await self.failed_preset()
        async def untrusted_off(call):
            if call.data.get('entity_id') == head:
                self.hass.states.async_set(head, 'off', {'restored': True})
            else:
                await self.handle(call)
        self.hass.services.async_register('climate', 'set_hvac_mode', untrusted_off)
        try:
            await c.async_set_enabled(True)
            await c.async_check()
            self.assertTrue(c.takeover_pending)
            self.assertIn(head, c.owned)
        finally:
            self.hass.services.async_register('climate', 'set_hvac_mode', self.handle)
            self.hass.states.async_set(head, 'off', {'hvac_modes': ['off', 'heat', 'cool', 'heat_cool']})
            await self.drain(c)

    async def test_unexpected_worker_exit_inhibits_restart_intent(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        runtime.finished = self.hass.loop.create_future()
        try:
            runtime._async_finished(None)
            await runtime.controls.async_check()
            self.assertFalse(runtime.controls.enabled)
            self.assertFalse(runtime.controls.enabled_intent)
            self.assertFalse((await runtime.controls.store.async_load())['enabled_intent'])
        finally:
            runtime.finished = None

    async def test_intentional_shutdown_rejects_positive_work_but_still_allows_off(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        head = runtime.config.heads['living']
        before = len(self.calls)
        runtime.stop_event.set()
        with self.assertRaises(HAStopping):
            await asyncio.to_thread(runtime.api.service, 'climate', 'set_hvac_mode',
                                    entity_id=head, hvac_mode='heat')
        self.assertEqual(self.calls[before:], [])
        await asyncio.to_thread(runtime.api.service, 'climate', 'set_hvac_mode',
                                entity_id=head, hvac_mode='off')
        self.assertEqual(self.hass.states.get(head).state, 'off')
        await runtime.async_stop()
        self.assertTrue((await runtime.controls.store.async_load())['enabled_intent'])
