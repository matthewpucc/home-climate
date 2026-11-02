"""Synthetic native ownership, independently supervised leases, and handoff."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.core import Context
from homeassistant.helpers.storage import Store

from custom_components.house_climate.controller.config import Config, FallbackPolicy
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate import handoff
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class LifecyclePolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def handle(self, call):
        if call.service == "set_temperature" and "target_temp_low" in call.data:
            self.calls.append((call.domain, call.service, dict(call.data)))
            entity = call.data["entity_id"]
            attrs = dict(self.hass.states.get(entity).attributes)
            attrs.update({k: call.data[k] for k in ("target_temp_low", "target_temp_high")})
            self.hass.states.async_set(entity, "heat_cool", attrs)
            return
        await fixtures.NativeTests.handle(self, call)

    async def fallback_runtime(self, *, live=True):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), fallback=FallbackPolicy(),
                      head_minimum_off_seconds=.01, off_settle_seconds=.01)
        for entity in cfg.heads.values():
            attrs = dict(self.hass.states.get(entity).attributes)
            attrs["hvac_modes"] = ["off", "heat", "cool", "heat_cool"]
            self.hass.states.async_set(entity, "off", attrs)
        return await self.runtime(live=live, cfg=cfg)

    async def test_manual_takeover_persists_and_never_commands_preset(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        heater = runtime.config.heater_entity
        self.hass.states.async_set(heater, "heat", dict(self.hass.states.get(heater).attributes))
        before = len(self.calls)
        await runtime.controls.async_set_auxiliary_manual(True)
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.calls[before:], [])
        self.assertTrue(runtime.controls.enabled)
        self.assertNotIn(heater, runtime.controls.owned)
        saved = await runtime.controls.store.async_load()
        self.assertEqual(saved["manual_auxiliary"], heater)
        restored = NativeControls(self.hass, runtime.config, False, "native", lambda: None)
        await restored.async_load()
        self.assertTrue(restored.auxiliary_manual)
        self.assertNotIn(heater, restored.owned)
        self.assertFalse(restored.enabled)
        with self.assertRaises(HAError):
            await runtime.controls.async_set_auxiliary_manual(False)
        self.hass.states.async_set(heater, "off", dict(self.hass.states.get(heater).attributes))
        await runtime.controls.async_set_auxiliary_manual(False)
        self.assertIn(heater, runtime.controls.owned)
        self.assertEqual(runtime.controls.auxiliary_authorized_at, 0)

    async def test_failed_manual_save_retains_automation_authority_and_fences_old_job(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        heater = runtime.config.heater_entity
        old_generation = runtime.controls.command_generation(heater)
        with patch.object(runtime.controls.store, "async_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await runtime.controls.async_set_auxiliary_manual(True)
        self.assertFalse(runtime.controls.auxiliary_manual)
        self.assertIn(heater, runtime.controls.owned)
        with self.assertRaises(HAError):
            runtime.controls.check_activation(heater, {"hvac_mode": "heat"}, generation=old_generation)

    async def test_takeover_rejects_restore_save_and_positive_jobs_without_dropping_claim(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        controls = runtime.controls
        guard = SimpleNamespace(_schedule_check=Mock(), manual_return_issue=None, power_cut_confirmed=False, blocked=False)
        controls.power_guard = guard
        for name, value in (("_restoring", True), ("_restore_intent", True),
                            ("_positive_tasks", {object()}), ("_compensation_pending", True),
                            ("_save_task", asyncio.get_running_loop().create_future())):
            with self.subTest(name=name):
                setattr(guard, name, value)
                with self.assertRaisesRegex(HAError, "actual auxiliary commands"):
                    await controls.async_set_auxiliary_manual(True)
                self.assertFalse(controls.auxiliary_manual)
                self.assertIn(runtime.config.heater_entity, controls.owned)
                delattr(guard, name)
        controls.power_guard = None

    async def test_return_does_not_erase_genuine_guard_recovery(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await runtime.controls.async_set_auxiliary_manual(True)
        guard = SimpleNamespace(_schedule_check=Mock(), manual_return_issue="unconfirmed shutdown", power_cut_confirmed=False, blocked=True)
        runtime.controls.power_guard = guard
        with self.assertRaisesRegex(HAError, "complete its recovery"):
            await runtime.controls.async_set_auxiliary_manual(False)
        self.assertTrue(runtime.controls.auxiliary_manual)
        runtime.controls.power_guard = None

    async def test_clean_preview_startup_has_no_terminal_authority(self):
        runtime, _ = await self.fallback_runtime(live=False)
        await self.settle_watchdog(runtime.controls)
        await runtime.controls.async_check()
        self.assertIsNone(runtime.controls.handoff)
        self.assertEqual(self.calls, [])

    async def test_new_schedule_admission_does_not_reuse_old_running_stop_bound(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        controls = runtime.controls
        controls.auxiliary_limit_provider = lambda: 73
        controls._auxiliary_stop_limit_f = 66.5
        states = controls._probe_states()  # Synthetic room is 67 F.
        self.assertFalse(controls.probes_safe(states))
        self.assertTrue(controls.probes_safe(states, authorization=False))
        self.assertEqual(controls.overlay(states)[runtime.config.entity(
            "auxiliary_probe_safe", "binary_sensor")]["state"], "on")
        controls.confirm_auxiliary_authorization(stop_limit_f=69)
        self.assertTrue(controls.probes_safe(states))

    async def test_peak_or_schedule_change_revokes_queued_old_context(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        controls = runtime.controls
        context = [68, 72, 69, False, False]
        controls.auxiliary_context_provider = lambda: context
        controls.confirm_auxiliary_authorization(context=context)
        heater = runtime.config.heater_entity
        generation = controls.command_generation(heater)
        context[-1] = True  # 17:00 boundary while an old Heat request was queued.
        with self.assertRaisesRegex(HAError, "context changed"):
            controls.check_activation(heater, {"hvac_mode": "heat"}, generation=generation)
        with self.assertRaisesRegex(HAError, "current decision"):
            controls.confirm_auxiliary_authorization(context=[68, 72, 69, False, False])
        controls.confirm_auxiliary_authorization(context=context)
        with self.assertRaisesRegex(HAError, "earlier controller lease"):
            controls.check_activation(heater, {"hvac_mode": "heat"}, generation=generation)
        context[0], context[2] = 69, 71
        with self.assertRaisesRegex(HAError, "context changed"):
            controls.check_activation(heater, {"hvac_mode": "heat"})

    async def test_exit_applies_saved_dual_setpoints_and_fans_then_releases(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        self.assertTrue((await runtime.controls.store.async_load())["handoff"]["authorized"])
        await runtime.controls.async_set_enabled(False)
        for _ in range(4):
            await runtime.controls.async_check()
            await self.hass.async_block_till_done()
            await asyncio.sleep(.015)
        self.assertFalse(runtime.controls.owned)
        self.assertIsNone(runtime.controls.handoff)
        for entity in runtime.config.heads.values():
            state = self.hass.states.get(entity)
            self.assertEqual(state.state, "heat_cool")
            self.assertEqual((state.attributes["target_temp_low"], state.attributes["target_temp_high"]), (68, 73))
        for entity in runtime.config.fans.values():
            self.assertEqual(self.hass.states.get(entity).attributes["percentage"], 40)
        self.assertEqual(self.hass.states.get(runtime.config.heater_entity).state, "off")

    async def test_interrupted_positive_handoff_is_not_replayed_over_operator_change(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        controls = runtime.controls
        plan = controls.handoff
        entity = next(iter(runtime.config.heads.values()))
        plan["stage"] = "applying"
        plan["requests"][entity] = {"at": handoff.now() - 1, "data": {
            "hvac_mode": "heat_cool", "target_temp_low": 68, "target_temp_high": 73}}
        controls.handoff = handoff.restored(plan)
        self.hass.states.async_set(entity, "heat", dict(self.hass.states.get(entity).attributes),
                                   context=Context(user_id="synthetic_operator"))
        controls.enabled, controls.recovering = False, True
        before = len(self.calls)
        await controls.async_check()
        self.assertEqual(self.calls[before:], [])
        self.assertIn("Operator changed", controls.handoff["problems"][entity])
        self.assertTrue(controls.owned)
        with self.assertRaisesRegex(HAError, "Fresh head Off"):
            await controls.async_retry_handoff()
        for head in runtime.config.heads.values():
            self.hass.states.async_set(head, "off", dict(self.hass.states.get(head).attributes),
                                       force_update=True)
        with patch.object(controls.store, "async_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await controls.async_retry_handoff()
        self.assertTrue(controls.handoff["interrupted"])
        await controls.async_retry_handoff()
        self.assertNotIn("interrupted", controls.handoff)
        for _ in range(5):
            await controls.async_check()
            await asyncio.sleep(.02)
        self.assertFalse(controls.owned)
