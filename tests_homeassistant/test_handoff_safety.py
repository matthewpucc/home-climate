"""Adversarial terminal handoff failures retain independent cleanup authority."""
import asyncio
import unittest
from unittest.mock import patch

from tests_homeassistant import test_lifecycle_policy as lifecycle
from tests_homeassistant import test_native as fixtures


class HandoffSafetyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = lifecycle.LifecyclePolicyTests.handle
    until = lifecycle.LifecyclePolicyTests.until
    runtime = lifecycle.LifecyclePolicyTests.runtime
    claim = lifecycle.LifecyclePolicyTests.claim
    settle_watchdog = lifecycle.LifecyclePolicyTests.settle_watchdog
    fallback_runtime = lifecycle.LifecyclePolicyTests.fallback_runtime

    async def finish_handoff(self, controls):
        async with asyncio.timeout(3):
            while controls.handoff is not None:
                await controls.async_check()
                await asyncio.sleep(.02)

    async def test_store_failure_does_not_withhold_safety_off_or_allow_positive_preset(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head = next(iter(runtime.config.heads.values()))
        heater = runtime.config.heater_entity
        for entity in (head, heater):
            self.hass.states.async_set(entity, "heat", dict(self.hass.states.get(entity).attributes))
        before = len(self.calls)
        with patch.object(runtime.controls.store, "async_save", side_effect=OSError("synthetic disk full")):
            await runtime.controls.async_set_enabled(False)
            await runtime.controls.async_check()
            await self.settle_watchdog(runtime.controls)
            stops = {data["entity_id"] for domain, service, data in self.calls[before:]
                     if domain == "climate" and service == "set_hvac_mode" and data["hvac_mode"] == "off"}
            self.assertTrue({head, heater} <= stops)
            self.assertFalse(any(service in ("set_temperature", "turn_on")
                                 for _, service, _ in self.calls[before:]))
            self.assertTrue(runtime.controls.owned)
            self.assertIn("synthetic disk full", runtime.controls.problem)
        await self.finish_handoff(runtime.controls)

    async def test_stalled_auxiliary_stop_does_not_hold_completed_comfort_handoff(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls, heater = runtime.controls, runtime.config.heater_entity
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.handle

        async def handle(call):
            if call.data.get("entity_id") == heater and call.service == "set_hvac_mode":
                entered.set()
                await release.wait()
            return await original(call)

        self.hass.services.async_register("climate", "set_hvac_mode", handle)
        self.hass.states.async_set(heater, "heat", dict(self.hass.states.get(heater).attributes))
        try:
            await controls.async_set_enabled(False)
            await controls.async_check()  # The actual auxiliary handler outlives its bounded wait.
            await self.finish_handoff(controls)
            self.assertTrue(entered.is_set())
            self.assertTrue(controls.stop_jobs({heater}))
            self.assertIn(heater, controls.pending_auxiliaries)
            self.assertFalse(controls.recovering)
            self.assertFalse(controls.owned)
            for entity in runtime.config.heads.values():
                self.assertEqual(self.hass.states.get(entity).state, "heat_cool")
            await self.claim(runtime)
            self.assertTrue(controls.enabled)
            self.assertNotIn(heater, controls.owned)
            self.assertIn(heater, (await controls.store.async_load())["pending_auxiliaries"])
        finally:
            release.set()
            await self.hass.async_block_till_done()
            await controls.async_set_enabled(False)
            await self.finish_handoff(controls)

    async def test_resolved_passive_cleanup_does_not_reclaim_manual_auxiliary(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls, heater = runtime.controls, runtime.config.heater_entity
        controls.pending_auxiliaries = (heater,)
        await controls.async_set_auxiliary_manual(True)
        await self.settle_watchdog(controls)
        await controls.async_check()
        self.assertTrue(controls.auxiliary_manual)
        self.assertNotIn(heater, controls.pending_auxiliaries)
        self.assertNotIn(heater, controls.owned)
        saved = await controls.store.async_load()
        self.assertEqual(saved["manual_auxiliary"], heater)
        self.assertNotIn(heater, saved["devices"])

    async def test_completed_handoff_retains_unresolved_old_fan_cleanup(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls = runtime.controls
        retired = "fan.retired_circulation"
        self.hass.states.async_set(retired, "on", {"percentage": 75})
        controls.pending_fans = (retired,)
        await controls._save(controls.owned, controls.pending_fans)
        original = self.handle

        async def handle(call):
            if call.data.get("entity_id") == retired:
                self.calls.append((call.domain, call.service, dict(call.data)))
                return  # Service returned; device has not acknowledged Off.
            return await original(call)

        self.hass.services.async_register("fan", "turn_off", handle)
        try:
            await controls.async_set_enabled(False)
            await self.finish_handoff(controls)
            self.assertEqual(self.hass.states.get(retired).state, "on")
            self.assertIn(retired, controls.pending_fans)
            self.assertIn(retired, (await controls.store.async_load())["pending_fans"])
            self.assertFalse(controls.owned)
        finally:
            self.hass.states.async_set(retired, "off", {"percentage": 0})
            await controls.async_check()

    async def test_cancelled_positive_job_cannot_be_confirmed_by_its_prior_state_echo(self):
        await self.cancelled_positive_case(fan=False)

    async def test_cancelled_fan_job_also_retains_uncertain_ownership(self):
        await self.cancelled_positive_case(fan=True)

    async def cancelled_positive_case(self, *, fan):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls = runtime.controls
        entity = next(iter((runtime.config.fans if fan else runtime.config.heads).values()))
        domain, service = ("fan", "turn_on") if fan else ("climate", "set_temperature")
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.handle

        async def handle(call):
            await original(call)  # Integration publishes intent before its operation completes.
            if call.data.get("entity_id") == entity and call.service == service:
                entered.set()
                await release.wait()

        self.hass.services.async_register(domain, service, handle)
        checking = None
        try:
            await controls.async_set_enabled(False)
            await controls.async_check()
            await asyncio.sleep(.02)
            checking = asyncio.create_task(controls.async_check())
            async with asyncio.timeout(2):
                await entered.wait()
            job = controls._handoff_jobs[entity]
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            await checking
            await controls.async_check()
            self.assertEqual(self.hass.states.get(entity).state, "on" if fan else "heat_cool")
            self.assertIsNotNone(controls.handoff)
            self.assertTrue(controls.handoff["requests"][entity]["uncertain"])
            self.assertNotIn(entity, controls.handoff["completed"])
            self.assertIn(entity, controls.owned)
        finally:
            release.set()
            if checking is not None:
                await asyncio.gather(checking, return_exceptions=True)
            # Explicitly resolve this synthetic uncertainty for fixture teardown.
            controls._uncertain_device_jobs.discard(entity)
            controls.handoff = None

    async def test_cancelling_queued_handoff_job_does_not_leak_service_accounting(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        controls = runtime.controls
        head = next(iter(runtime.config.heads.values()))
        original = self.hass.async_create_background_task
        cancelled = []

        def create(coroutine, name, **kwargs):
            task = original(coroutine, name, **kwargs)
            if name == "Home Climate recovery setting" and not cancelled:
                task.cancel()  # eager_start=False: the handler has never begun.
                cancelled.append(task)
            return task

        try:
            with (patch.object(controls, "_schedule_check"),
                  patch.object(self.hass, "async_create_background_task", side_effect=create)):
                await controls.async_set_enabled(False)
                await controls.async_check()
                await self.settle_watchdog(controls)
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(controls.service_inflight, 0)
            self.assertFalse(any(service == "set_temperature" and data["entity_id"] == head
                                 for _, service, data in self.calls))
            self.assertTrue(controls.owned)
        finally:
            controls._uncertain_device_jobs.discard(head)
            controls.handoff = None
