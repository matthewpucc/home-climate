"""Test every actual head write path, not only a token in isolation."""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from custom_components.house_climate import handoff, handoff_blower
from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.head_write_fence import HeadWriteFence
from tests_homeassistant import test_handoff_blower as blower
from tests_homeassistant import test_handoff_retries as terminal
from tests_homeassistant import test_native as native
from tests_homeassistant import test_native_controls as controls_tests


class NativeWorkerAndWatchdogFenceTests(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeTests.asyncSetUp
    asyncTearDown = native.NativeTests.asyncTearDown
    handle = native.NativeTests.handle
    runtime = controls_tests.NativeControlTests.runtime
    claim = controls_tests.NativeControlTests.claim
    settle_watchdog = controls_tests.NativeControlTests.settle_watchdog

    async def test_queued_worker_stop_is_stale_even_after_head_reopens(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head = next(iter(runtime.config.heads.values()))
        fence = runtime.controls.head_write_fence
        real_submit = asyncio.run_coroutine_threadsafe
        release = asyncio.Event()
        queued = asyncio.Event()

        def submit(coroutine, loop):
            async def hold():
                queued.set()
                await release.wait()
                return await coroutine
            return real_submit(hold(), loop)

        original = list(self.calls)
        with patch("custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                   side_effect=submit):
            job = asyncio.create_task(asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                                        entity_id=head, hvac_mode="off"))
            try:
                await asyncio.wait_for(queued.wait(), timeout=2)
                fence.rotate(head, blocked=True)
                fence.rotate(head, blocked=False)
            finally:
                release.set()
            with self.assertRaises(HAActivationDeferred):
                await job
        self.assertEqual(self.calls, original)

    async def test_queued_watchdog_stop_is_stale_after_head_reopens(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head = next(iter(runtime.config.heads.values()))
        fence = runtime.controls.head_write_fence
        real_off_job = runtime.controls._off_job
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_off_job(*args, **kwargs)

        original = list(self.calls)
        with patch.object(runtime.controls, "_off_job", side_effect=hold):
            job = asyncio.create_task(runtime.controls._off(head))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                fence.rotate(head, blocked=True)
                fence.rotate(head, blocked=False)
            finally:
                release.set()
            self.assertFalse(await job)
        self.assertEqual(self.calls, original)

    async def test_worker_positive_and_watchdog_refuse_fenced_head_only(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head, other, *_remaining = runtime.config.heads.values()
        original = list(self.calls)
        fence = runtime.controls.head_write_fence
        fence.rotate(head, blocked=True)
        try:
            with self.assertRaisesRegex(HAActivationDeferred, "fenced head"):
                await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                        entity_id=head, hvac_mode="heat", temperature=71)
            self.assertFalse(await runtime.controls._off(head))
            self.assertEqual(self.calls, original)
            self.assertTrue(await runtime.controls._off(other))
            self.assertEqual({call[2]["entity_id"] for call in self.calls[len(original):]}, {other})
        finally:
            fence.rotate(head, blocked=False)


class TerminalPresetFenceTest(IsolatedAsyncioTestCase):
    asyncSetUp = terminal.HandoffRetryTests.asyncSetUp
    asyncTearDown = terminal.HandoffRetryTests.asyncTearDown
    report = terminal.HandoffRetryTests.report
    reset = terminal.HandoffRetryTests.reset
    handle = terminal.HandoffRetryTests.handle

    async def test_fence_rotated_while_preset_reservation_is_saved(self):
        original_save = handoff._save

        async def rotate(controls):
            if self.head in controls.handoff["requests"]:
                controls.head_write_fence.rotate(self.head, blocked=True)
                controls.head_write_fence.rotate(self.head, blocked=False)
            await original_save(controls)

        with patch.object(handoff, "_save", side_effect=rotate):
            await handoff.step(self.controls)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.plan["requests"][self.head].get("dispatched"))
        self.assertIn("fenced head", self.plan["problems"][self.head])


class BlowerRestoreFenceTest(IsolatedAsyncioTestCase):
    asyncSetUp = blower.BlowerRecoveryTests.asyncSetUp
    asyncTearDown = blower.BlowerRecoveryTests.asyncTearDown
    state = blower.BlowerRecoveryTests.state

    async def test_fence_rotated_while_blower_restore_reservation_is_saved(self):
        fence = self.controls.head_write_fence = HeadWriteFence([self.entity])

        async def rotate(*_args):
            fence.rotate(self.entity, blocked=True)
            fence.rotate(self.entity, blocked=False)

        self.controls._save.side_effect = rotate
        await handoff_blower.step(self.controls, self.plan)
        self.assertEqual(self.calls, [])
        self.assertNotIn("dispatched_at", self.record)
        self.assertTrue(any("fenced head" in problem for problem in self.plan["problems"].values()))
