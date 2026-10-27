"""Real HA dispatch cancellation must not erase an uncertain head activation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.runtime import NativeActivationDispatch
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class RepairDispatchSafetyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def claimed_head(self):
        cfg = replace(Config.from_dict(self.cfg.to_dict()),
                      heater_entity=None, auxiliary_room=None)
        for entity in cfg.climate_entities:
            old = self.hass.states.get(entity)
            self.hass.states.async_set(entity, "off", dict(old.attributes))
        runtime, _ = await self.runtime(cfg=cfg, entry_id="repair_dispatch")
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        return runtime, cfg.heads["guest"]

    def positive_request(self, runtime, head, seconds):
        deadline = time.monotonic() + seconds
        return asyncio.create_task(runtime.api._async_request(
            "services/climate/set_temperature",
            {"entity_id": head, "hvac_mode": "cool", "temperature": 72},
            deadline,
            generation=runtime.controls.ownership_generation,
            dispatch=NativeActivationDispatch(deadline),
        ))

    async def test_dispatched_job_survives_its_callers_timeout(self):
        runtime, head = await self.claimed_head()
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_positive(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            entered.set()
            try:
                await release.wait()
            finally:
                completed.set()

        self.hass.services.async_register("climate", "set_temperature", slow_positive)
        request = self.positive_request(runtime, head, .08)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises((TimeoutError, HAError)):
                await request
            self.assertFalse(completed.is_set(), "Caller timeout cancelled the actual device job")
            self.assertEqual(runtime.controls.inflight, 1)
            self.assertTrue(runtime.controls.owned)
        finally:
            release.set()
            await asyncio.wait_for(completed.wait(), 1)
            await self.until(lambda: runtime.controls.inflight == 0)
            self.hass.services.async_register("climate", "set_temperature", self.handle)

    async def late_positive_after_shutdown(self, *, late_mode=None):
        runtime, head = await self.claimed_head()
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        second_off_started, second_off_release = asyncio.Event(), asyncio.Event()
        events = []
        off_count = 0

        async def slow_positive(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            events.append("positive-start")
            entered.set()
            try:
                await release.wait()
                if late_mode:
                    old = self.hass.states.get(head)
                    self.hass.states.async_set(head, late_mode, dict(old.attributes))
                events.append("positive-finish")
            finally:
                completed.set()

        async def controlled_off(call):
            nonlocal off_count
            if call.data["entity_id"] != head:
                await self.handle(call)
                return
            self.calls.append((call.domain, call.service, dict(call.data)))
            off_count += 1
            events.append(f"off-{off_count}-start")
            if off_count >= 2:
                second_off_started.set()
                await second_off_release.wait()
            old = self.hass.states.get(head)
            self.hass.states.async_set(head, "off", dict(old.attributes))
            events.append(f"off-{off_count}-finish")

        self.hass.services.async_register("climate", "set_temperature", slow_positive)
        self.hass.services.async_register("climate", "set_hvac_mode", controlled_off)
        request = self.positive_request(runtime, head, 30)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertFalse(completed.is_set(), "Caller cancellation cancelled the device job")
            self.assertEqual(runtime.controls.inflight, 1)
            await runtime.controls.async_set_enabled(False)
            await self.until(lambda: "off-1-finish" in events)
            self.assertEqual(self.hass.states.get(head).state, "off")
            self.assertTrue(runtime.controls.owned)

            release.set()
            await asyncio.wait_for(completed.wait(), 1)
            await self.until(lambda: runtime.controls.inflight == 0)
            # A previous check may have been active when the completion arrived.
            # Explicit maintenance is allowed, but it cannot trust the old Off.
            check = asyncio.create_task(runtime.controls.async_check())
            await asyncio.wait_for(second_off_started.wait(), 1)
            self.assertTrue(runtime.controls.owned,
                            "Old cached Off released ownership after a late positive completion")
            self.assertGreater(events.index("off-2-start"), events.index("positive-finish"))
            second_off_release.set()
            await check
            await self.until(lambda: not runtime.controls.owned)
            self.assertEqual(self.hass.states.get(head).state, "off")
        finally:
            release.set()
            second_off_release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.wait_for(completed.wait(), 1)
            await self.until(lambda: runtime.controls.inflight == 0)
            self.hass.services.async_register("climate", "set_temperature", self.handle)
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            await runtime.controls.async_check()

    async def test_cached_off_cannot_acknowledge_late_positive_completion(self):
        await self.late_positive_after_shutdown()

    async def test_reported_late_positive_also_requires_a_superseding_off(self):
        await self.late_positive_after_shutdown(late_mode="cool")

    async def test_old_off_report_cannot_acknowledge_a_newer_stop_receipt(self):
        runtime, head = await self.claimed_head()
        entered, release_positive, positive_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first_off_started, release_first_off, first_off_done = (
            asyncio.Event(), asyncio.Event(), asyncio.Event())
        events = []
        off_count = 0

        async def slow_positive(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            entered.set()
            try:
                await release_positive.wait()
                events.append("positive-finish")
            finally:
                positive_done.set()

        async def ordered_off(call):
            nonlocal off_count
            if call.data["entity_id"] != head:
                await self.handle(call)
                return
            self.calls.append((call.domain, call.service, dict(call.data)))
            off_count += 1
            events.append(f"off-{off_count}-start")
            if off_count == 1:
                first_off_started.set()
                await release_first_off.wait()
                old = self.hass.states.get(head)
                self.hass.states.async_set(head, "off", dict(old.attributes))
                events.append("off-1-finish")
                first_off_done.set()
            # Newer Off services complete, but have no new device report yet.

        self.hass.services.async_register("climate", "set_temperature", slow_positive)
        self.hass.services.async_register("climate", "set_hvac_mode", ordered_off)
        request = self.positive_request(runtime, head, 30)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await runtime.controls.async_set_enabled(False)
            await asyncio.wait_for(first_off_started.wait(), 1)
            release_positive.set()
            await asyncio.wait_for(positive_done.wait(), 1)
            await self.until(lambda: runtime.controls.inflight == 0)
            release_first_off.set()
            await asyncio.wait_for(first_off_done.wait(), 1)
            await self.settle_watchdog(runtime.controls)
            self.assertTrue(runtime.controls.owned,
                            "An Off begun before the late positive completion acknowledged its stop")
            await runtime.controls.async_check()
            self.assertGreaterEqual(off_count, 2)
            self.assertTrue(runtime.controls.owned,
                            "A newer service receipt reused an older Off report as device feedback")
            self.assertLess(events.index("off-1-start"), events.index("positive-finish"))
            self.assertGreater(events.index("off-1-finish"), events.index("positive-finish"))

            old = self.hass.states.get(head)
            self.hass.states.async_set(head, "off", dict(old.attributes))
            await runtime.controls.async_check()
            await self.until(lambda: not runtime.controls.owned)
        finally:
            release_positive.set()
            release_first_off.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if entered.is_set():
                await asyncio.wait_for(positive_done.wait(), 1)
                await self.until(lambda: runtime.controls.inflight == 0)
            self.hass.services.async_register("climate", "set_temperature", self.handle)
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            await runtime.controls.async_check()

    async def test_predispatch_expiry_has_no_activation_or_shutdown_obligation(self):
        runtime, head = await self.claimed_head()
        before = len(self.calls)
        deadline = time.monotonic() - 1
        with self.assertRaises((HAActivationDeferred, HAError)):
            await runtime.api._async_request(
                "services/climate/set_temperature",
                {"entity_id": head, "hvac_mode": "cool", "temperature": 72},
                deadline, generation=runtime.controls.ownership_generation,
                dispatch=NativeActivationDispatch(deadline),
            )
        self.assertEqual(runtime.controls.inflight, 0)
        self.assertEqual(len(self.calls), before)
        await runtime.controls.async_set_enabled(False)
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.owned)

    async def test_shielded_old_off_cannot_outlive_ownership_release(self):
        runtime, head = await self.claimed_head()
        await self.positive_request(runtime, head, 30)
        first_off_started, release_first_off, first_off_done = (
            asyncio.Event(), asyncio.Event(), asyncio.Event())
        second_off_done = asyncio.Event()
        off_count = 0

        async def ordered_off(call):
            nonlocal off_count
            if call.data["entity_id"] != head:
                await self.handle(call)
                return
            off_count += 1
            if off_count == 1:
                first_off_started.set()
                await release_first_off.wait()
                await self.handle(call)
                first_off_done.set()
            else:
                await self.handle(call)
                second_off_done.set()

        self.hass.services.async_register("climate", "set_hvac_mode", ordered_off)
        request = asyncio.create_task(runtime.api._async_request(
            "services/climate/set_hvac_mode", {"entity_id": head, "hvac_mode": "off"},
            time.monotonic() + 30, generation=runtime.controls.ownership_generation,
        ))
        try:
            await asyncio.wait_for(first_off_started.wait(), 1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertFalse(first_off_done.is_set())
            await runtime.controls.async_set_enabled(False)
            await asyncio.wait_for(second_off_done.wait(), 1)
            await self.settle_watchdog(runtime.controls)
            self.assertEqual(self.hass.states.get(head).state, "off")
            self.assertTrue(runtime.api.service_jobs([head]))
            self.assertTrue(runtime.controls.owned,
                            "A pending Off could outlive ownership and stop a later manual call")
            with self.assertRaises(HAError):
                await runtime.controls.async_set_enabled(True)
            release_first_off.set()
            await asyncio.wait_for(first_off_done.wait(), 1)
            await self.until(lambda: not runtime.api.service_jobs([head]))
            await runtime.controls.async_check()
            await self.until(lambda: not runtime.controls.owned)
        finally:
            release_first_off.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if first_off_started.is_set():
                await asyncio.wait_for(first_off_done.wait(), 1)
                await self.until(lambda: not runtime.api.service_jobs([head]))
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            await runtime.controls.async_set_enabled(False)
            await runtime.controls.async_check()

    async def test_queued_off_must_drain_before_ownership_release(self):
        runtime, head = await self.claimed_head()
        queued, release_dispatch = asyncio.Event(), asyncio.Event()
        original_dispatch = runtime.api._async_service_job

        async def delayed_dispatch(*args, **kwargs):
            queued.set()
            await release_dispatch.wait()
            return await original_dispatch(*args, **kwargs)

        with patch.object(runtime.api, "_async_service_job", new=delayed_dispatch):
            request = asyncio.create_task(runtime.api._async_request(
                "services/climate/set_hvac_mode",
                {"entity_id": head, "hvac_mode": "off"}, time.monotonic() + 30,
                generation=runtime.controls.ownership_generation,
            ))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertTrue(runtime.api.service_jobs([head]))
                await runtime.controls.async_set_enabled(False)
                await self.settle_watchdog(runtime.controls)
                self.assertTrue(runtime.controls.owned,
                                "An uncounted queued Off could stop the next ownership session")
                with self.assertRaises(HAError):
                    await runtime.controls.async_set_enabled(True)
                release_dispatch.set()
                await self.until(lambda: not runtime.api.service_jobs([head]))
                await runtime.controls.async_check()
                await self.until(lambda: not runtime.controls.owned)
            finally:
                release_dispatch.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                await self.until(lambda: not runtime.api.service_jobs([head]))
                await runtime.controls.async_set_enabled(False)
                await runtime.controls.async_check()

    async def obsolete_outer_off(self, *, repair=False):
        runtime, head = await self.claimed_head()
        queued, release_outer = asyncio.Event(), asyncio.Event()
        original_request = runtime.api._async_request
        lease = runtime.api.repair_gate.begin("fixture_owner", [head]) if repair else None

        async def delayed_outer(path, body, deadline, **kwargs):
            if path == "services/climate/set_hvac_mode" and body.get("entity_id") == head:
                queued.set()
                await release_outer.wait()
            return await original_request(path, body, deadline, **kwargs)

        with patch.object(runtime.api, "_async_request", new=delayed_outer):
            worker = asyncio.create_task(asyncio.to_thread(
                runtime.api.service, "climate", "set_hvac_mode", entity_id=head, hvac_mode="off"))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                self.assertFalse(runtime.api.service_jobs([head]),
                                 "The test must delay before service-job registration")
                if repair:
                    # A newly submitted current-epoch stop remains usable while
                    # positive commands are held by the integration repair.
                    await original_request(
                        "services/climate/set_hvac_mode", {"entity_id": head, "hvac_mode": "off"},
                        time.monotonic() + 30, generation=runtime.controls.ownership_generation,
                        repair_epoch=runtime.api.repair_gate.capture(head, activation=False),
                    )
                    self.assertTrue(runtime.api.repair_gate.held(head))
                    self.assertEqual(self.hass.states.get(head).state, "off")
                    self.assertTrue(runtime.api.repair_gate.release(lease))
                else:
                    await runtime.controls.async_set_enabled(False)
                    await self.settle_watchdog(runtime.controls)
                    self.assertFalse(runtime.controls.owned)
                    await self.claim(runtime)
                await original_request(
                    "services/climate/set_temperature",
                    {"entity_id": head, "hvac_mode": "cool", "temperature": 72},
                    time.monotonic() + 30, generation=runtime.controls.ownership_generation,
                    repair_epoch=runtime.api.repair_gate.capture(head),
                )
                self.assertEqual(self.hass.states.get(head).state, "cool")
                before = len(self.calls)
                release_outer.set()
                with self.assertRaises(HAError):
                    await worker
                self.assertEqual(len(self.calls), before,
                                 "An obsolete outer stop reached the HA service registry")
                self.assertEqual(self.hass.states.get(head).state, "cool")
            finally:
                release_outer.set()
                await asyncio.gather(worker, return_exceptions=True)
                if lease is not None:
                    runtime.api.repair_gate.release(lease)
                await runtime.controls.async_set_enabled(False)
                await runtime.controls.async_check()

    async def test_outer_queued_off_cannot_stop_a_new_ownership_session(self):
        await self.obsolete_outer_off()

    async def test_outer_off_from_repair_cannot_stop_a_post_repair_call(self):
        await self.obsolete_outer_off(repair=True)

    async def test_new_worker_off_after_release_cannot_stop_a_manual_head(self):
        runtime, head = await self.claimed_head()
        await runtime.controls.async_set_enabled(False)
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.owned)
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, "cool", {**old.attributes, "temperature": 69})
        before = len(self.calls)
        try:
            await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                    entity_id=head, hvac_mode="off")
        except HAError:
            pass  # Rejecting a stale worker stop is a permitted outcome.
        self.assertEqual(len(self.calls), before,
                         "A new worker stop captured a fresh epoch after ownership release")
        self.assertEqual(self.hass.states.get(head).state, "cool")
        self.assertEqual(self.hass.states.get(head).attributes["temperature"], 69)

    async def test_fresh_off_stays_allowed_while_disabled_but_still_owned(self):
        runtime, head = await self.claimed_head()
        await self.positive_request(runtime, head, 30)
        # Hold automatic scheduling so this tests the native worker stop itself.
        with patch.object(runtime.controls, "_schedule_check"):
            await runtime.controls.async_set_enabled(False)
            self.assertIn(head, runtime.controls.owned)
            await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                    entity_id=head, hvac_mode="off")
            self.assertEqual(self.hass.states.get(head).state, "off")
        await runtime.controls.async_check()
        self.assertFalse(runtime.controls.owned)

    async def test_unowned_pending_fan_cleanup_remains_allowed(self):
        runtime, _ = await self.claimed_head()
        await runtime.controls.async_set_enabled(False)
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.owned)
        # An old mapped fan is a durable cleanup obligation, even though it is
        # outside the current configuration/state-change subscription set.
        fan = "fan.fixture_previous_cleanup"
        self.hass.states.async_set(fan, "on", {"percentage": 60})
        runtime.controls.pending_fans = (fan,)
        await asyncio.to_thread(runtime.api.service, "fan", "turn_off", entity_id=fan)
        self.assertEqual(self.hass.states.get(fan).state, "off")
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.pending_fans)

    async def test_watchdog_off_must_finish_before_worker_releases_ownership(self):
        runtime, head = await self.claimed_head()
        # A manual call present at claim time has no controller-positive
        # receipt, but its watchdog stop still cannot outlive manual handoff.
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, "cool", dict(old.attributes))
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed_watchdog_off(call):
            if call.data["entity_id"] != head:
                await self.handle(call)
                return
            await self.handle(call)
            entered.set()
            try:
                await release.wait()
                await self.handle(call)
            finally:
                completed.set()

        self.hass.services.async_register("climate", "set_hvac_mode", delayed_watchdog_off)
        try:
            await runtime.controls.async_set_enabled(False)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.hass.states.get(head).state, "off")
            self.assertTrue(runtime.api.service_jobs([head]),
                            "Owner-scoped repair drain omitted a watchdog Off handler")
            other = next(entity for entity in runtime.config.heads.values() if entity != head)
            self.assertFalse(runtime.api.service_jobs([other]),
                             "Owner-scoped drain included an unrelated watchdog stop")
            await runtime.controls.async_service("input_boolean", "turn_off", {})
            self.assertTrue(runtime.controls.owned,
                            "Worker release ignored an unfinished watchdog Off handler")
            release.set()
            await asyncio.wait_for(completed.wait(), 1)
            await self.settle_watchdog(runtime.controls)
            await runtime.controls.async_check()
            self.assertFalse(runtime.controls.owned)
        finally:
            release.set()
            if entered.is_set():
                await asyncio.wait_for(completed.wait(), 1)
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            await self.settle_watchdog(runtime.controls)
            await runtime.controls.async_check()


if __name__ == "__main__":
    unittest.main()
