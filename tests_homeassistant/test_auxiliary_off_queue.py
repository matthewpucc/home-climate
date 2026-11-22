"""The extra routine Off queue allowance is not a hardware response allowance."""
import asyncio
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.house_climate import power_guard, runtime as native
from custom_components.house_climate.controller.ha import HAError
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as controls


class AuxiliaryOffQueueTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = controls.NativeControlTests.runtime
    claim = controls.NativeControlTests.claim
    settle_watchdog = controls.NativeControlTests.settle_watchdog

    async def controlled(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        return runtime

    def queue_clock(self, elapsed):
        origin = time.monotonic()
        return SimpleNamespace(monotonic=lambda: origin + elapsed
                               if threading.get_ident() == self.hass.loop_thread_id else origin)

    async def send(self, runtime, entity, mode="off"):
        return await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                       entity_id=entity, hvac_mode=mode)

    async def test_loop_resumes_at_12_seconds_once_with_no_response_wait_and_no_duplicate(self):
        runtime = await self.controlled()
        heater = runtime.config.heater_entity
        release = asyncio.Event()
        entered = asyncio.Event()
        cancelled = []
        calls = []
        async def wait_off(call):
            calls.append(call.data["entity_id"])
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            await self.handle(call)
        self.hass.services.async_register("climate", "set_hvac_mode", wait_off)
        try:
            with patch.object(native, "time", self.queue_clock(12)):
                with self.assertRaisesRegex(HAError, "Off dispatched after response deadline.*result unresolved"):
                    await asyncio.wait_for(self.send(runtime, heater), 1)
                self.assertTrue(entered.is_set())
                jobs = runtime.api.service_jobs({heater})
                self.assertEqual(len(jobs), 1)
                self.assertFalse(jobs[0].done())
                with self.assertRaisesRegex(HAError, "no duplicate dispatched"):
                    await self.send(runtime, heater)
                self.assertEqual(calls, [heater])
                self.assertEqual(cancelled, [])
                # The separate critical guard still runs its own stop chain
                # with the independent, unchanged five-second Off budget.
                guard = SimpleNamespace(_emergency_ac_latched=False, power_cut_confirmed=False,
                    _off_ack=AsyncMock(return_value=False), _latched=True,
                    bindings=SimpleNamespace(heater_switch="switch.fixture"),
                    _cut_switch=AsyncMock(return_value=True), _publish=lambda: None)
                await power_guard.HeaterPowerGuard._shed(guard)
                guard._off_ack.assert_awaited_once_with(5.0)
                guard._cut_switch.assert_awaited_once()
                self.assertFalse(jobs[0].done())
        finally:
            release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(cancelled, [])

    async def test_normal_scope_does_not_extend_heads_activation_or_shutdown(self):
        runtime = await self.controlled()
        heater = runtime.config.heater_entity
        head = next(iter(runtime.config.heads.values()))
        before = len(self.calls)
        with patch.object(native, "time", self.queue_clock(12)):
            with self.assertRaisesRegex(HAError, "expired before dispatch"):
                await self.send(runtime, head)
            with self.assertRaises(HAError):
                await self.send(runtime, heater, mode="heat")
            runtime.api.stop_event.set()
            try:
                with self.assertRaisesRegex(HAError, "expired before dispatch"):
                    await self.send(runtime, heater)
            finally:
                runtime.api.stop_event.clear()
        with patch.object(native, "time", self.queue_clock(15)):
            with self.assertRaisesRegex(HAError, "expired before dispatch"):
                await self.send(runtime, heater)
        self.assertEqual(self.calls[before:], [])

    async def test_on_time_off_preserves_real_service_result_and_manual_gate(self):
        runtime = await self.controlled()
        heater = runtime.config.heater_entity
        self.assertEqual(await self.send(runtime, heater), [])
        with patch.object(native, "time", self.queue_clock(12)):
            with patch.object(runtime.controls, "_manual_transition", True):
                with self.assertRaisesRegex(HAError, "manual ownership"):
                    await self.send(runtime, heater)

    async def test_on_time_dispatch_does_not_inherit_the_longer_queue_wait_for_response(self):
        release = asyncio.Event()
        elapsed = time.monotonic()
        job = asyncio.create_task(release.wait())
        started = asyncio.get_running_loop().create_future()
        started.set_result(elapsed)
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(native.NativeAPI._await_routine_off_dispatch(
                    job, started, elapsed + 15, elapsed + .02, "services/climate/set_hvac_mode"), .5)
            self.assertFalse(job.done())
        finally:
            release.set()
            await job

    async def test_auxiliary_off_does_not_enter_head_mode_attribution(self):
        runtime = await self.controlled()
        heater = runtime.config.heater_entity
        head = next(iter(runtime.config.heads.values()))
        await asyncio.to_thread(runtime.api.service_mode_recovery, head)
        diagnostics = runtime.controls.mode_diagnostics
        recorded = diagnostics.rows[head].copy()
        self.assertEqual((recorded["command_path_code"], recorded["command_reason_code"]), (3, 5))
        with patch.object(native, "time", self.queue_clock(12)):
            with self.assertRaisesRegex(HAError, "Off dispatched after response deadline"):
                await self.send(runtime, heater)
        await self.hass.async_block_till_done()
        self.assertNotIn(heater, diagnostics.rows)
        self.assertEqual(diagnostics.rows[head], recorded)
