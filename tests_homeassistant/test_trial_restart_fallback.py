"""Native ownership restart must not bypass a pending trial Auto handoff."""
import asyncio
import time
import unittest

from custom_components.house_climate.controls import NativeControls
from tests_homeassistant import test_lifecycle_policy as lifecycle
from tests_homeassistant import test_native as fixtures


class TrialRestartFallbackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = lifecycle.LifecyclePolicyTests.handle
    until = lifecycle.LifecyclePolicyTests.until
    runtime = lifecycle.LifecyclePolicyTests.runtime
    claim = lifecycle.LifecyclePolicyTests.claim
    settle_watchdog = lifecycle.LifecyclePolicyTests.settle_watchdog
    fallback_runtime = lifecycle.LifecyclePolicyTests.fallback_runtime

    async def armed(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls = runtime.controls
        entity = next(iter(runtime.config.heads.values()))
        attrs = dict(self.hass.states.get(entity).attributes)
        attrs.update(fan_mode="low", fan_modes=["auto", "low"])
        self.hass.states.async_set(entity, "off", attrs)
        controls.handoff["blower_recovery"] = {entity:dict(trial_id="test",mode="low",
            baseline="auto",armed_at=time.time()-1,stage="pending",attempts=0)}
        await controls._save(controls.owned, controls.pending_fans)
        return runtime, controls, entity

    async def test_reload_suppresses_saved_enabled_takeover(self):
        runtime, controls, entity = await self.armed()
        restored = NativeControls(self.hass,runtime.config,True,"native",lambda:None)
        await restored.async_load()
        self.assertTrue(restored.enabled_intent)
        self.assertFalse(restored._resume_requested)
        self.assertFalse(restored.takeover_pending)
        self.assertTrue(restored.recovering)
        self.assertIn(entity,restored.handoff["blower_recovery"])
        # Resolve the test fixture's original owner's independent cleanup.
        controls.handoff["blower_recovery"][entity]["stage"]="superseded"

    async def test_stop_resets_blower_and_applies_normal_hvac_auto(self):
        _, controls, entity = await self.armed()
        async def fan(call):
            self.calls.append((call.domain,call.service,dict(call.data)))
            attrs=dict(self.hass.states.get(entity).attributes)
            attrs["fan_mode"]=call.data["fan_mode"]
            self.hass.states.async_set(entity,self.hass.states.get(entity).state,attrs,context=call.context)
        self.hass.services.async_register("climate","set_fan_mode",fan)
        await controls.async_set_enabled(False)
        async with asyncio.timeout(3):
            while controls.handoff:
                await controls.async_check()
                await asyncio.sleep(.02)
        self.assertEqual(self.hass.states.get(entity).attributes["fan_mode"],"auto")
        self.assertEqual(self.hass.states.get(entity).state,"heat_cool")
        self.assertFalse(controls.owned)
        self.assertEqual(sum(service=="set_fan_mode" for _,service,_ in self.calls),1)

    async def test_later_manual_hvac_is_not_overwritten_by_handoff(self):
        _, controls, entity = await self.armed()
        controls.handoff["blower_recovery"][entity].update(stage="superseded",manual_hvac=True)
        before=len(self.calls)
        await controls.async_set_enabled(False)
        await self.settle_watchdog(controls)
        self.assertFalse(self.calls[before:])
        self.assertIn("manual_hvac",controls.handoff["problems"])
        # Test cleanup, not a production automatic release of the hold.
        controls.handoff["blower_recovery"][entity].pop("manual_hvac")

    async def test_manual_hvac_during_blower_service_revokes_same_step_auto(self):
        from homeassistant.core import Context
        from custom_components.house_climate.controller.head_fan_trial import FanTrial
        runtime, controls, entity = await self.armed()
        manager = runtime.head_fan_trials
        manager.trial = FanTrial(trial_id="test", entity_id=entity, mode="low", baseline="auto",
            starts_at=time.time()-30, ends_at=time.time()+600, binding_fingerprint="synthetic",
            ownership_generation=controls.command_generation(entity), stage="active")
        boundary = []
        async def fan(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            # The user changes HVAC while the recovery service is actually in
            # flight, before the same watchdog invocation reaches HVAC Auto.
            await self.hass.services.async_call("climate", "set_hvac_mode",
                {"entity_id":entity,"hvac_mode":"cool"}, blocking=True,
                context=Context(user_id="synthetic_operator"))
            await asyncio.sleep(0)
            boundary.append(len(self.calls))
        self.hass.services.async_register("climate", "set_fan_mode", fan)
        await controls.async_set_enabled(False)
        await self.settle_watchdog(controls)
        self.assertTrue(boundary)
        self.assertTrue(controls.handoff["blower_recovery"][entity]["manual_hvac"])
        self.assertEqual(controls.handoff["stage"], "blocked")
        self.assertFalse(any(service == "set_temperature" and data.get("hvac_mode") == "heat_cool"
                             for _, service, data in self.calls[boundary[0]:]))
        self.assertEqual(self.hass.states.get(entity).state,"cool")
        # Test-fixture cleanup only; production requires explicit resolution.
        controls.handoff["blower_recovery"][entity].pop("manual_hvac")

    async def test_missing_blower_capability_does_not_withhold_hvac_auto(self):
        _, controls, entity = await self.armed()
        attrs=dict(self.hass.states.get(entity).attributes)
        attrs["fan_modes"]=["low"]
        self.hass.states.async_set(entity,"off",attrs)
        await controls.async_set_enabled(False)
        await self.settle_watchdog(controls)
        self.assertEqual(self.hass.states.get(entity).state,"heat_cool")
        self.assertIn(entity,controls.owned)
        self.assertIn(f"{entity}:blower",controls.handoff["problems"])
        controls.handoff["blower_recovery"][entity]["stage"]="superseded"

    async def test_explicit_retry_renews_blower_intent_after_fresh_off(self):
        from unittest.mock import patch
        _, controls, entity = await self.armed()
        record=controls.handoff["blower_recovery"][entity]
        record.update(stage="superseded",manual_hvac=True)
        await controls.async_set_enabled(False)
        await self.settle_watchdog(controls)
        for head in controls.handoff["heads"]:
            self.hass.states.async_set(head,"off",dict(self.hass.states.get(head).attributes),force_update=True)
        with patch.object(controls,"_schedule_check"):
            await controls.async_retry_handoff()
            renewed=controls.handoff["blower_recovery"][entity]
            self.assertEqual(renewed["stage"],"pending")
            self.assertEqual(renewed["attempts"],0)
            self.assertNotIn("manual_hvac",renewed)
            renewed["stage"]="superseded"
