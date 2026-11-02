"""Reserve/supply policy, explicit restoration approval and dispatch races."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import unittest

from tests import test_power_guard as f
from tests import test_heater_power_guard as watts

power, B = f.power, f.B


class SupplyPolicyAsyncTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = f.PowerGuardAsyncTests.asyncSetUp
    asyncTearDown = f.PowerGuardAsyncTests.asyncTearDown
    settle = f.PowerGuardAsyncTests.settle
    trip = f.PowerGuardAsyncTests.trip
    healthy = f.PowerGuardAsyncTests.healthy

    async def failed_stop(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard._shutdown_fault)
        self.guard._store = type('Store', (), {})()
        self.guard._store.async_save = AsyncMock()
        self.guard.recovery_controller_off = lambda: True
        self.clock.advance(301)
        self.healthy()
        self.guard._observe()
        self.hass.ignore_climate = False

    async def test_all_aggregate_levels_and_missing_upstream_are_diagnostic(self):
        self.hass.set(B.heater, 'heat', {'hvac_action': 'heating'})
        for entity in (B.input_power, B.circuit_power, B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            for value in (5000, 'unavailable', 'NaN', 0):
                self.hass.set(entity, value)
                self.hass.set(B.output_power, 2029)
                self.guard._observe()
                self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})
                self.assertFalse(self.guard.blocked)
                self.assertEqual(self.hass.calls, [])

    async def test_queued_activation_revalidates_supply_not_watt_totals(self):
        self.guard._observe()
        self.hass.set(B.input_power, 5000)
        self.hass.set(B.output_power, 5000)
        self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})
        self.hass.set(B.battery, 40)
        with self.assertRaises(Exception):
            self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})

    async def test_failed_stop_never_restores_without_explicit_approval(self):
        await self.failed_stop()
        for _ in range(20):
            self.clock.advance(60)
            self.hass.refresh()
            self.guard._observe()
            await asyncio.sleep(0)
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_confirmation_and_storage_failure_cannot_authorize_restoration(self):
        await self.failed_stop()
        for confirmation in (False, None, 1, 'true'):
            with self.assertRaises(RuntimeError):
                await self.guard.async_approve_restore(confirm=confirmation)
        self.guard._store.async_save.side_effect = OSError('disk')
        with self.assertRaises(RuntimeError):
            await self.guard.async_approve_restore(confirm=True)
        self.assertIsNone(self.guard._restore_approved_until)
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_approval_rechecks_condition_changes_across_storage_await(self):
        await self.failed_stop()
        async def changed(_data):
            self.hass.set(B.battery, 40)
        self.guard._store.async_save.side_effect = changed
        with self.assertRaises(RuntimeError):
            await self.guard.async_approve_restore(confirm=True)
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_approved_restore_keeps_existing_native_verification(self):
        await self.failed_stop()
        self.hass.set(B.input_power, 5000)
        self.hass.set(B.output_power, 5000)
        self.hass.restore_to_heat = True
        self.assertTrue(await self.guard.async_approve_restore(confirm=True))
        self.guard._observe()
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])
        for _ in range(4):
            self.clock.advance(21)
            self.hass.refresh()
            self.guard._observe()
            await asyncio.sleep(0)
        await self.settle()
        self.assertEqual(sum(c[1] == 'turn_on' for c in self.hass.calls), 1)
        self.assertEqual(self.hass.data[B.heater]['state'], 'off')
        self.assertFalse(self.guard._shutdown_fault)
        self.assertFalse(self.guard.switch_cut_owned)

    async def test_new_supply_loss_revokes_approval(self):
        await self.failed_stop()
        await self.guard.async_approve_restore(confirm=True)
        self.hass.set(B.ac_output_switch, 'off')
        self.guard._observe()
        self.assertIsNone(self.guard._restore_approved_until)
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_manual_takeover_during_restore_persistence_cannot_energize_relay(self):
        await self.failed_stop()
        await self.guard.async_approve_restore(confirm=True)
        persist = self.guard._persist_restore_intent
        async def takeover():
            result = await persist()
            self.guard.manual_override = lambda: True
            return result
        self.guard._persist_restore_intent = takeover
        await self.guard._restore()
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.blocked)

    async def test_normal_successful_stop_recovers_after_sixty_seconds(self):
        self.trip()
        await self.settle()
        self.assertFalse(self.guard._shutdown_fault)
        self.healthy()
        self.hass.set(B.output_power, 5000)
        self.hass.set(B.input_power, 5000)
        self.guard._observe()
        self.clock.advance(61)
        self.hass.refresh()
        self.guard._observe()
        self.assertFalse(self.guard.blocked)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_heat_mode_does_not_assert_heating_draw(self):
        self.hass.set(B.heater, 'heat', {'hvac_action': 'idle'})
        self.guard._observe()
        self.assertEqual(self.guard.diagnostics['heater_draw_status'], 'idle')
        self.hass.set(B.heater_current, 10)
        self.guard._observe()
        self.assertEqual(self.guard.diagnostics['heater_draw_status'], 'heating_draw_observed')
        self.hass.set(B.heater_current, 'unavailable')
        self.guard._observe()
        self.assertEqual(self.guard.diagnostics['heater_draw_status'], 'unknown')
        self.assertFalse(self.guard.blocked)

    async def test_approval_expiry_at_queued_relay_dispatch_prevents_on(self):
        await self.failed_stop()
        await self.guard.async_approve_restore(confirm=True)
        self.hass.refresh()
        self.guard._observe()
        self.clock.advance(1)
        self.hass.refresh()
        self.guard._observe()
        # Expire authority only after the save, at the queued dispatch boundary.
        service = self.guard._service
        def queued(domain, action, entity, **kwargs):
            if action == 'turn_on':
                self.clock.advance(121)
                self.hass.refresh()
            return service(domain, action, entity, **kwargs)
        self.guard._service = queued
        await self.guard._restore()
        await self.settle()
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])
        self.assertTrue(self.guard.blocked)

    async def test_approval_serializes_and_survives_caller_cancellation(self):
        await self.failed_stop()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def save(data):
            entered.set()
            await finish.wait()
        self.guard._store.async_save.side_effect = save
        caller = asyncio.create_task(self.guard.async_approve_restore(confirm=True))
        await asyncio.wait_for(entered.wait(), 1)
        with self.assertRaises(RuntimeError):
            await self.guard.async_approve_restore(confirm=True)
        with self.assertRaises(RuntimeError):
            await self.guard.async_acknowledge_emergency()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        self.assertIsNone(self.guard._restore_approved_until)
        finish.set()
        await self.guard._recovery_task
        self.assertTrue(self.guard._restore_authorized())
        self.assertNotIn('restore_approval_pending', self.guard._saved_state())
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])

    async def test_pending_auxiliary_work_started_during_approval_read_rejects(self):
        await self.failed_stop()
        async def read(entity):
            self.guard.pending_external_jobs = lambda: (object(),)
            return 'off'
        self.guard.async_read_recovery_feedback = read
        with self.assertRaises(RuntimeError):
            await self.guard.async_approve_restore(confirm=True)
        self.assertIsNone(self.guard._restore_approved_until)
        self.assertTrue(self.guard._manual_recovery_required)
        self.guard.pending_external_jobs = lambda: ()

    async def test_failed_cutoff_does_not_disable_negative_cleanup(self):
        self.hass.ignore_climate = True
        service = self.hass.services.async_call
        async def ignore_cut(domain, action, data, **kwargs):
            if action == "turn_off":
                self.hass.calls.append((domain, action, data))
                return
            return await service(domain, action, data, **kwargs)
        self.hass.services.async_call = ignore_cut
        self.trip()
        await self.settle()
        before = len(self.hass.calls)
        self.assertTrue(self.guard._manual_recovery_required)
        self.hass.services.async_call = service
        self.guard._observe()
        await self.settle()
        self.assertGreater(len(self.hass.calls), before)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertNotIn('turn_on', [c[1] for c in self.hass.calls])
