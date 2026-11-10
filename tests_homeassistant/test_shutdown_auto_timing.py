"""Terminal Auto skips controller timers, never confirmed stops or real jobs."""
from dataclasses import replace
from unittest.mock import patch
import unittest

from custom_components.house_climate import handoff
from tests_homeassistant import test_lifecycle_policy as lifecycle
from tests_homeassistant import test_native as fixtures


class ShutdownAutoTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = lifecycle.LifecyclePolicyTests.handle
    until = lifecycle.LifecyclePolicyTests.until
    runtime = lifecycle.LifecyclePolicyTests.runtime
    claim = lifecycle.LifecyclePolicyTests.claim
    settle_watchdog = lifecycle.LifecyclePolicyTests.settle_watchdog
    fallback_runtime = lifecycle.LifecyclePolicyTests.fallback_runtime

    async def controls(self):
        runtime, _ = await self.fallback_runtime()
        cfg = replace(runtime.config, head_minimum_off_seconds=180,
                      off_settle_seconds=300, head_activation_timeout_seconds=120)
        runtime.config = runtime.controls.config = cfg
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        return runtime.controls

    def auto_calls(self):
        return [data for domain, service, data in self.calls
                if domain == "climate" and service == "set_temperature"
                and data.get("hvac_mode") == "heat_cool"]

    async def test_shutdown_goes_from_acknowledged_off_to_auto_without_clock_advance(self):
        controls = await self.controls()
        for head in controls.config.heads.values():
            self.hass.states.async_set(head, "heat", dict(self.hass.states.get(head).attributes))
        instant = handoff.now()
        with patch.object(controls, "_schedule_check"), patch.object(handoff, "now", return_value=instant):
            await controls.async_set_enabled(False)
            await controls.async_check()
            self.assertEqual({data["entity_id"] for data in self.auto_calls()},
                             set(controls.config.heads.values()))
            self.assertEqual(controls.handoff["stage"], "applying")
            self.assertIsNone(controls.handoff["settle_until"])
            self.assertEqual(controls.handoff["applying_started"], instant)
            first_auto = next(i for i, (_, service, _) in enumerate(self.calls)
                              if service == "set_temperature")
            stopped = {data["entity_id"] for _, service, data in self.calls[:first_auto]
                       if service == "set_hvac_mode" and data["hvac_mode"] == "off"}
            self.assertTrue(set(controls.config.heads.values()) <= stopped)
            await controls.async_check()
            self.assertIsNone(controls.handoff)
            self.assertFalse(controls.owned)

    async def test_unconfirmed_head_stop_still_blocks_auto(self):
        controls = await self.controls()
        head = next(iter(controls.config.heads.values()))
        self.hass.states.async_set(head, "heat", dict(self.hass.states.get(head).attributes))
        original = self.handle

        async def no_feedback(call):
            if call.data.get("entity_id") == head:
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await original(call)

        self.hass.services.async_register("climate", "set_hvac_mode", no_feedback)
        try:
            with patch.object(controls, "_schedule_check"):
                await controls.async_set_enabled(False)
                await controls.async_check()
                self.assertEqual(self.auto_calls(), [])
                self.assertEqual(controls.handoff["stage"], "stopping")
                self.assertIn(head, controls.owned)
        finally:
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)

    async def test_prior_head_jobs_still_block_auto(self):
        controls = await self.controls()
        head = next(iter(controls.config.heads.values()))
        pending = self.hass.loop.create_future()
        controls.pending_device_jobs = lambda entities: (pending,) if head in entities and not pending.done() else ()
        try:
            with patch.object(controls, "_schedule_check"):
                await controls.async_set_enabled(False)
                await controls.async_check()
                self.assertEqual(self.auto_calls(), [])
                self.assertEqual(controls.handoff["stage"], "stopping")
                pending.set_result(None)
                await controls.async_check()
                self.assertTrue(self.auto_calls())
        finally:
            if not pending.done():
                pending.set_result(None)
            controls.pending_device_jobs = lambda entities: ()

    async def test_legacy_settling_stage_does_not_wait_saved_deadline(self):
        controls = await self.controls()
        with patch.object(controls, "_schedule_check"):
            await controls.async_set_enabled(False)
            controls.handoff.update(stage="settling", settle_until=handoff.now() + 300)
            await controls.async_check()
            self.assertTrue(self.auto_calls())
            self.assertEqual(controls.handoff["stage"], "applying")
            self.assertIsNone(controls.handoff["settle_until"])

    async def test_legacy_settling_stage_still_waits_actual_head_jobs(self):
        controls = await self.controls()
        head = next(iter(controls.config.heads.values()))
        pending = self.hass.loop.create_future()
        controls.pending_device_jobs = lambda entities: (pending,) if head in entities else ()
        try:
            with patch.object(controls, "_schedule_check"):
                await controls.async_set_enabled(False)
                controls.handoff.update(stage="settling", settle_until=handoff.now() + 300)
                await controls.async_check()
                self.assertEqual(self.auto_calls(), [])
                self.assertTrue(controls.owned)
        finally:
            pending.set_result(None)
            controls.pending_device_jobs = lambda entities: ()
