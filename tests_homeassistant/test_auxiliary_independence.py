"""Auxiliary cleanup must remain durable without blocking other room control."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.controls import NativeControls
from tests_homeassistant import test_native_controls as fixtures


class AuxiliaryIndependenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeControlTests.asyncSetUp
    asyncTearDown = fixtures.NativeControlTests.asyncTearDown
    handle = fixtures.NativeControlTests.handle
    until = fixtures.NativeControlTests.until
    runtime = fixtures.NativeControlTests.runtime
    claim = fixtures.NativeControlTests.claim
    settle_watchdog = fixtures.NativeControlTests.settle_watchdog

    async def pending(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        c = runtime.controls
        await self.settle_watchdog(c)
        c._schedule_check = lambda *_: None
        c.power_guard = SimpleNamespace(blocked=True, power_cut_confirmed=False,
                                        emergency_fallback_enabled=False)
        self.hass.states.async_set(self.cfg.heater_entity, 'unavailable')
        for entity in self.cfg.heads.values():
            self.hass.states.async_set(entity, 'off', dict(self.hass.states.get(entity).attributes))
        c.enabled = False
        c.recovering = True
        return runtime, c

    async def test_auxiliary_job_does_not_hold_heads_or_fans_in_recovery(self):
        runtime, c = await self.pending()
        c.inflight = c.auxiliary_inflight = 1
        await c._clear_if_off()
        self.assertFalse(c.recovering)
        self.assertEqual(c.pending_auxiliaries, (self.cfg.heater_entity,))
        saved = await c.store.async_load()
        self.assertEqual(saved['pending_auxiliaries'], [self.cfg.heater_entity])
        await self.claim(runtime)
        for entity in (*self.cfg.heads.values(), *self.cfg.fans.values()):
            c.check_activation(entity, {'hvac_mode': 'cool'})
        with self.assertRaises(HAError):
            c.check_activation(self.cfg.heater_entity, {'hvac_mode': 'heat'})
        c.check_stop(self.cfg.heater_entity)
        c.inflight = c.auxiliary_inflight = 0
        self.hass.states.async_set(self.cfg.heater_entity, 'off')
        c._auxiliary_heat_possible = False
        await c.async_check()
        self.assertEqual(c.pending_auxiliaries, ())
        self.assertIn(self.cfg.heater_entity, c.owned)

    async def test_separation_requires_a_durable_write(self):
        _, c = await self.pending()
        original = c.owned
        c.store.async_save = AsyncMock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):
            await c._clear_if_off()
        self.assertEqual(c.owned, original)
        self.assertTrue(c.recovering)
        self.assertEqual(c.pending_auxiliaries, ())
        c.store.async_save = AsyncMock()
        self.hass.states.async_set(self.cfg.heater_entity, 'off')

    async def test_nonauxiliary_job_still_blocks_recovery_and_activation(self):
        _, c = await self.pending()
        c.inflight = 1
        await c._clear_if_off()
        self.assertTrue(c.recovering)
        await c.async_set_enabled(True)
        self.assertFalse(c._snapshot["enabled"])
        self.assertTrue(c.takeover_pending)
        c.inflight = 0
        self.hass.states.async_set(self.cfg.heater_entity, 'off')

    async def test_reload_keeps_pending_auxiliary_obligation_without_global_recovery(self):
        _, c = await self.pending()
        await c._clear_if_off()
        restarted = NativeControls(self.hass, c.config, True, 'native', lambda: None)
        await restarted.async_load()
        self.assertEqual(restarted.pending_auxiliaries, (self.cfg.heater_entity,))
        self.assertFalse(restarted.recovering)
        self.assertTrue(restarted._auxiliary_heat_possible)
        self.hass.states.async_set(self.cfg.heater_entity, 'off')

    async def test_worker_starts_room_cooling_while_auxiliary_cleanup_remains_unconfirmed(self):
        runtime, c = await self.pending()
        heater = self.cfg.heater_entity
        async def device(call):
            if call.data.get('entity_id') == heater:
                raise RuntimeError('Synthetic disconnected auxiliary')
            await self.handle(call)
        for service in ('set_hvac_mode', 'set_temperature'):
            self.hass.services.async_register('climate', service, device)
        await c._clear_if_off()
        self.assertEqual(c.pending_auxiliaries, (heater,))
        await self.claim(runtime)
        runtime.async_start()
        try:
            try:
                await self.until(lambda: self.hass.states.get(self.cfg.heads['guest']).state == 'cool')
            except TimeoutError:
                self.fail(f'Room cooling stalled: {runtime.status.get("device_issues")}; controls={c._snapshot}')
            self.assertTrue(c.enabled)
            self.assertEqual(c.pending_auxiliaries, (heater,))
            self.assertFalse(any(call[2].get('entity_id') == heater
                                 and call[2].get('hvac_mode') == 'heat' for call in self.calls))
        finally:
            for service in ('set_hvac_mode', 'set_temperature'):
                self.hass.services.async_register('climate', service, self.handle)
            self.hass.states.async_set(heater, 'off')
            c._auxiliary_heat_possible = False
            await c.async_check()
