"""Distinguish an abandoned HA-loop queue from a command already dispatched."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import threading
import time
import unittest
from unittest.mock import Mock, patch

from custom_components.house_climate.controller.config import Config, Settings
from custom_components.house_climate.controller.executor import Executor
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.controller.policy import Policy
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class NativeDispatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def running_head(self):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), head_minimum_run_seconds=300)
        runtime, _ = await self.runtime(cfg=cfg)
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        executor = Executor(cfg, runtime.api, "°F")
        executor.phase, executor.direction = "running", "cool"
        states = await asyncio.to_thread(runtime.api.states)
        plan = Policy(cfg).decide(states, datetime.now(timezone.utc), Settings.read(states, cfg))
        plan.mode, plan.head_targets_f = "cool", {"guest": 72}
        plan.head_call_targets_f = {}
        plan.heater_on = plan.auxiliary_fan_only = False
        plan.fans = {}
        await asyncio.to_thread(executor.step, plan, states, 0)
        states = await asyncio.to_thread(runtime.api.states)
        await asyncio.to_thread(executor.step, plan, states, .1)
        guest = cfg.heads["guest"]
        self.assertEqual(self.hass.states.get(guest).state, "cool")
        self.assertEqual(self.hass.states.get(guest).attributes["temperature"], 72)
        self.assertNotIn(guest, executor.pending)
        self.assertIsNotNone(executor.head_cycles[guest].run_since)
        return runtime, executor, guest

    async def test_queued_retarget_timeout_preserves_run_and_abandoned_late_delivery_cannot_dispatch(self):
        runtime, executor, guest = await self.running_head()
        before_cycle = deepcopy(executor.head_cycles[guest])
        baseline = len(self.calls)
        queued = []
        future = Mock()
        future.result.side_effect = FutureTimeout()
        # Deliberately make cancellation ineffective: the dispatch ticket must suffice.
        future.cancel.return_value = False
        def hold(coroutine, loop):
            queued.append(coroutine)
            return future
        try:
            with patch(
                "custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                side_effect=hold,
            ):
                await asyncio.to_thread(
                    executor.climate, guest, "cool", 73,
                    await asyncio.to_thread(runtime.api.states), .2,
                    comfort_target_f=73,
                )
            future.cancel.assert_called_once()
            self.assertEqual(len(queued), 1)
            self.assertEqual(self.calls[baseline:], [])
            self.assertEqual(self.hass.states.get(guest).attributes["temperature"], 72)
            self.assertEqual(executor.head_cycles[guest], before_cycle)
            self.assertNotIn(guest, executor.pending)
            self.assertEqual(executor.recoveries, {})
            self.assertEqual(executor.device_issues, {})
            # Run the exact queued coroutine after the worker already abandoned it.
            with self.assertRaises(HAActivationDeferred):
                await queued.pop()
            self.assertEqual(self.calls[baseline:], [])
            self.assertEqual(runtime.controls.inflight, 0)
            self.assertEqual(runtime.controls.auxiliary_inflight, 0)
            self.assertTrue(runtime.controls.enabled)
        finally:
            for coroutine in queued:
                coroutine.close()

    async def test_abandoned_auxiliary_and_fan_commands_do_not_dispatch_or_gain_inflight_ownership(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        heater = runtime.config.heater_entity
        fan = next(iter(runtime.config.fans.values()))
        for domain, service, data in (
            ("climate", "set_temperature", {"entity_id": heater, "hvac_mode": "heat", "temperature": 86}),
            ("fan", "turn_on", {"entity_id": fan, "percentage": 60}),
        ):
            with self.subTest(domain=domain):
                queued = []
                future = Mock()
                future.result.side_effect = FutureTimeout()
                future.cancel.return_value = False
                def hold(coroutine, loop):
                    queued.append(coroutine)
                    return future
                try:
                    with patch(
                        "custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                        side_effect=hold,
                    ):
                        with self.assertRaises(HAActivationDeferred):
                            await asyncio.to_thread(runtime.api.service, domain, service, **data)
                    with self.assertRaises(HAActivationDeferred):
                        await queued.pop()
                    self.assertEqual(runtime.controls.inflight, 0)
                    self.assertEqual(runtime.controls.auxiliary_inflight, 0)
                    self.assertEqual(self.calls, [])
                    self.assertTrue(runtime.controls.enabled)
                finally:
                    for coroutine in queued:
                        coroutine.close()

    async def test_timeout_after_actual_service_start_remains_uncertain_and_requires_off_recovery(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        executor = Executor(runtime.config, runtime.api, "°F")
        heater = runtime.config.heater_entity
        started, release = threading.Event(), asyncio.Event()
        async def slow_heat(call):
            started.set()
            await release.wait()
            await self.handle(call)
        self.hass.services.async_register("climate", "set_temperature", slow_heat)
        submit = asyncio.run_coroutine_threadsafe
        actual_futures = []
        cancellations = []
        class StartedFuture:
            def result(self, timeout):
                if not started.wait(1):
                    raise AssertionError("The actual HA service never started")
                raise FutureTimeout()
            def cancel(self):
                cancellations.append(True)
                return False
        def started_submission(coroutine, loop):
            actual_futures.append(submit(coroutine, loop))
            return StartedFuture()
        instant = time.monotonic()
        try:
            with patch(
                "custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                side_effect=started_submission,
            ):
                await asyncio.to_thread(
                    executor.climate, heater, "heat", 86,
                    await asyncio.to_thread(runtime.api.states), instant, heater=True,
                )
            self.assertTrue(cancellations)
            self.assertIn(heater, executor.pending)
            self.assertFalse(executor.pending[heater].receipt)
            self.assertIn(heater, executor.recoveries)
            self.assertIn("native operation timed out", executor.device_issues[heater])
            self.assertEqual(runtime.controls.inflight, 1)
            self.assertEqual(runtime.controls.auxiliary_inflight, 1)
        finally:
            release.set()
            for future in actual_futures:
                await asyncio.wrap_future(future)
        self.assertEqual(self.hass.states.get(heater).state, "heat")
        self.assertEqual(runtime.controls.inflight, 0)
        self.assertEqual(runtime.controls.auxiliary_inflight, 0)
        await asyncio.to_thread(
            executor.off, heater, await asyncio.to_thread(runtime.api.states), instant + .1,
        )
        self.assertEqual(self.hass.states.get(heater).state, "off")
        self.assertTrue(any(
            service == "set_hvac_mode" and data["entity_id"] == heater
            and data["hvac_mode"] == "off"
            for _, service, data in self.calls
        ))

    async def test_queued_shutdown_timeout_keeps_existing_failure_semantics(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        queued = []
        future = Mock()
        future.result.side_effect = FutureTimeout()
        future.cancel.return_value = False
        def hold(coroutine, loop):
            queued.append(coroutine)
            return future
        try:
            with patch(
                "custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                side_effect=hold,
            ):
                with self.assertRaises(HAError) as failed:
                    await asyncio.to_thread(
                        runtime.api.service, "climate", "set_hvac_mode",
                        entity_id=runtime.config.heads["guest"], hvac_mode="off",
                    )
            self.assertNotIsInstance(failed.exception, HAActivationDeferred)
            self.assertIn("native operation timed out", str(failed.exception))
            self.assertEqual(self.calls, [])
        finally:
            for coroutine in queued:
                coroutine.close()
