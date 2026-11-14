"""Guarded recovery and the native watchdog must not race their own Off jobs."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.house_climate.controls import NativeControls
from tests.test_controller import config
from tests_homeassistant import test_heater_power_guard as fixture


class RestoreWatchdogTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real HA state/service fixture without inheriting unrelated tests.
    asyncSetUp = fixture.NativeHeaterWattTests.asyncSetUp
    asyncTearDown = fixture.NativeHeaterWattTests.asyncTearDown
    put = fixture.NativeHeaterWattTests.put
    handle = fixture.NativeHeaterWattTests.handle
    start = fixture.NativeHeaterWattTests.start
    refresh = fixture.NativeHeaterWattTests.refresh
    write_snapshot = fixture.NativeHeaterWattTests.write_snapshot
    advance_isolated = fixture.NativeHeaterWattTests.advance_isolated
    store_claim = fixture.NativeHeaterWattTests.store_claim

    async def prepare(self):
        self.put(self.b.heater_switch, "off")
        self.put(self.b.heater, "unavailable")
        self.put(self.b.heater_power, 0, {"unit_of_measurement": "W"}, timestamp=self.origin - 1000)
        await self.start(saved=self.store_claim(owned=True))
        self.guard.recovery_controller_off = lambda: True
        self.guard.async_read_recovery_feedback = AsyncMock(return_value="off")
        self.write_snapshot(1, omit=(self.b.heater_power, self.b.heater, self.b.heater_switch))
        self.assertTrue(await self.guard.async_approve_restore(confirm=True))
        await self.hass.async_block_till_done()
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertIsNone(self.guard._reading.heater_w)
        self.calls.clear()

    async def wired_restore(self, *, native_delay=0, on_delay=0, missing_watts=False, missing_native=False, already_latched=False):
        await self.prepare()
        controls = NativeControls(self.hass, replace(config(), heater_entity=self.b.heater),
                                  True, "restore-watchdog", lambda: None)
        controls.active = controls.enabled = controls.enabled_intent = True
        controls.owned = (self.b.heater,)
        controls.power_guard = self.guard
        controls.store.async_save = AsyncMock()
        controls.heartbeat_monotonic = 1000
        controls.auxiliary_authorized_at = 0  # No valid heating authorization.
        controls.auxiliary_heartbeat_blocked = already_latched
        self.guard.pending_external_jobs = lambda: controls.stop_jobs({self.b.heater})
        # Same watchdog scheduling edge as ControllerRuntime's guard callback.
        self.guard.notify = controls._schedule_check
        timer = SimpleNamespace(monotonic=lambda: 1000 + self.guard._clock())
        owner = self

        class FixtureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(owner.wall, tz or timezone.utc)

        self.report_meter_after_off = not missing_watts
        self.ignore_climate = missing_native
        before_dispatch, powered = [], []
        original_save = self.guard._persist_restore_intent

        async def save_with_watchdog_yield():
            ok = await original_save()
            # An asynchronous storage write lets the production-scheduled check run.
            await asyncio.sleep(0)
            before_dispatch.append((controls._auxiliary_could_heat(),
                                    controls.auxiliary_heartbeat_blocked,
                                    len(controls.stop_jobs({self.b.heater}))))
            return ok

        async def handler(call):
            if call.domain == "switch" and call.service == "turn_on":
                # In-flight On must NOT be represented as isolation, even while
                # the state cache still says Off. The guard owns bounded Off.
                self.assertFalse(self.guard.recovery_isolation_confirmed)
                self.assertFalse(self.guard.power_cut_confirmed)
                self.assertTrue(self.guard.recovery_native_off_managed)
                await controls.async_check()
                if on_delay:
                    await asyncio.sleep(on_delay)
            if call.domain == "climate" and native_delay:
                await asyncio.sleep(native_delay)
            await self.handle(call)
            if call.domain == "switch" and call.service == "turn_on":
                powered.append(True)
                await controls.async_check()
                self.assertFalse(self.guard.recovery_isolation_confirmed)
                self.assertFalse(self.guard.shutdown_confirmed)
                self.assertEqual(controls.observations.counts["off_acknowledged"], 0)
                self.assertFalse(controls.stop_jobs({self.b.heater}))

        self.guard._persist_restore_intent = save_with_watchdog_yield
        for domain, services in (("climate", ("set_hvac_mode",)),
                                 ("switch", ("turn_on", "turn_off"))):
            for service in services:
                self.hass.services.async_register(domain, service, handler)
        try:
            with patch("custom_components.house_climate.controls.time", timer), \
                 patch("custom_components.house_climate.controls.datetime", FixtureDateTime):
                await self.advance_isolated(62)
                self.assertEqual(before_dispatch, [(True, already_latched, 0)])
                self.assertEqual(powered, [True])
                # The expired heartbeat is still tracked; deferral grants no lease.
                self.assertEqual(controls.observations.counts["authorization_expired"], 0 if already_latched else 1)
                self.assertEqual(controls.auxiliary_authorized_at, 0)
                if missing_watts or missing_native:
                    self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
                    self.assertTrue(self.guard.blocked)
                    self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
                else:
                    self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
                    self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
                    self.assertFalse(self.guard.blocked)
                    self.assertEqual(self.hass.states.get(self.b.heater_power).state, "5.5")
                    self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported.timestamp(),
                                       self.origin + 61)
                self.assertFalse(self.guard.recovery_native_off_managed)
        finally:
            controls.active = False
            self.guard.notify = lambda: None
            await self.hass.async_block_till_done()

    async def test_watchdog_does_not_cancel_restore_during_intent_save(self):
        await self.wired_restore(native_delay=.001)

    async def test_already_latched_watchdog_defers_without_fake_acknowledgement(self):
        await self.wired_restore(native_delay=.001, already_latched=True)

    async def test_watchdog_defers_off_through_delayed_on_and_native_reconnect(self):
        await self.wired_restore(on_delay=.005, native_delay=.005)

    async def test_watchdog_cannot_replace_missing_post_on_watts_with_isolation(self):
        await self.wired_restore(native_delay=.001, missing_watts=True)

    async def test_missing_native_off_still_recuts_after_recovery_deadline(self):
        await self.wired_restore(native_delay=.001, missing_native=True)

    async def test_unrelated_pending_off_still_blocks_before_relay_dispatch(self):
        await self.prepare()
        entered, release, job_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.guard._persist_restore_intent

        async def paused_save():
            ok = await original()
            entered.set()
            await release.wait()
            return ok

        self.guard._persist_restore_intent = paused_save
        self.write_snapshot(62, omit=(self.b.heater_power, self.b.heater, self.b.heater_switch))
        self.guard._observe()
        await asyncio.wait_for(entered.wait(), 1)
        job = asyncio.create_task(job_release.wait())
        self.guard.pending_external_jobs = lambda: (job,) if not job.done() else ()
        release.set()
        await asyncio.sleep(.01)
        job_release.set()
        await job
        await self.hass.async_block_till_done()
        self.assertNotIn("turn_on", [c[1] for c in self.calls])
        self.assertEqual(self.guard.diagnostics["restore_attempts"], 1)
        await self.advance_isolated(122)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])

    async def test_predispatch_isolation_rejects_unsafe_or_unverified_evidence(self):
        await self.prepare()
        self.guard._restoring = True
        self.assertTrue(self.guard.recovery_isolation_confirmed)
        self.assertFalse(self.guard.power_cut_confirmed)
        for flag in ("_restore_on_requested", "_identity_mismatch", "_shutdown_contradicted",
                     "_manual_recovery_required", "_compensation_pending"):
            with patch.object(self.guard, flag, True):
                self.assertFalse(self.guard.recovery_isolation_confirmed, flag)
        for attrs in ({"restored": True}, {"assumed_state": True}):
            self.put(self.b.heater_switch, "off", attrs)
            self.assertFalse(self.guard.recovery_isolation_confirmed)
        self.put(self.b.heater_switch, "off")
        with patch.object(self.guard, "_positive_tasks", {object()}):
            self.assertFalse(self.guard.recovery_isolation_confirmed)
        with patch.object(self.guard, "_saved_binding_identity", {}):
            self.assertFalse(self.guard.recovery_isolation_confirmed)
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"}, timestamp=self.wall + .001)
        self.assertFalse(self.guard.recovery_isolation_confirmed)
        self.put(self.b.heater_power, "unavailable")
        self.guard._recovery_feedback.clear()
        self.write_snapshot(182, omit=(self.b.heater_power, self.b.heater, self.b.heater_switch))
        self.assertFalse(self.guard.recovery_isolation_confirmed)
        self.guard._restoring = False

    async def test_managed_off_ends_at_deadline_fault_or_uncertain_work(self):
        await self.prepare()
        task = asyncio.create_task(asyncio.sleep(1))
        self.guard._action_task = task
        self.guard._restoring = self.guard._restore_on_requested = True
        self.guard._restore_deadline = time.monotonic() + 1
        try:
            self.assertTrue(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "live", False):
                self.assertFalse(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "_action_task", SimpleNamespace(done=lambda: True)):
                self.assertFalse(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "_restore_deadline", time.monotonic() - 1):
                self.assertFalse(self.guard.recovery_native_off_managed)
            for flag in ("_closing", "_closed", "_restore_interrupted", "_identity_mismatch",
                         "_manual_recovery_required", "_shutdown_contradicted", "_compensation_pending"):
                with patch.object(self.guard, flag, True):
                    self.assertFalse(self.guard.recovery_native_off_managed, flag)
            with patch.object(self.guard, "pending_external_jobs", lambda: (object(),)):
                self.assertFalse(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "_restore_approved_until", self.guard._clock() - 1):
                self.assertFalse(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "_invalid_positive", {object()}):
                self.assertFalse(self.guard.recovery_native_off_managed)
            with patch.object(self.guard, "_persistence_error", "cannot save"):
                self.assertFalse(self.guard.recovery_native_off_managed)
            self.guard._off_request_wall = self.wall - 1
            self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
            self.assertFalse(self.guard.recovery_native_off_managed)
            self.put(self.b.heater_power, "unavailable")
            self.put(self.b.battery, 40, {"unit_of_measurement": "%"})
            self.assertFalse(self.guard.recovery_native_off_managed)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.guard._action_task = None
            self.guard._restoring = self.guard._restore_on_requested = False
            self.guard._restore_deadline = None

    async def test_controller_shutdown_does_not_defer_its_stop(self):
        await self.prepare()
        controls = NativeControls(self.hass, replace(config(), heater_entity=self.b.heater),
                                  True, "stopping-watchdog", lambda: None)
        controls.owned = (self.b.heater,)
        controls.power_guard = SimpleNamespace(recovery_native_off_managed=True,
                                               emergency_fallback_enabled=False, power_cut_confirmed=False)
        controls.enabled = False
        await controls._off(self.b.heater)
        self.assertEqual([c[1] for c in self.calls], ["set_hvac_mode"])
        self.assertEqual(controls.observations.counts["off_requested"], 1)

    async def test_coordination_is_not_general_shutdown_proof_or_stop_suppression(self):
        await self.prepare()
        controls = NativeControls(self.hass, replace(config(), heater_entity=self.b.heater),
                                  True, "watchdog-scope", lambda: None)
        controls.active = controls.enabled = controls.enabled_intent = True
        controls.owned = (self.b.heater,)
        controls.heartbeat_monotonic = time.monotonic()
        controls.power_guard = SimpleNamespace(
            recovery_isolation_confirmed=True, recovery_native_off_managed=True,
            emergency_fallback_enabled=False, power_cut_confirmed=False)
        self.assertTrue(controls._guarded_recovery_watchdog())
        self.assertFalse(controls._is_off(self.b.heater))
        controls.auxiliary_heartbeat_blocked = True
        self.assertFalse(controls._ack_auxiliary_heartbeat_off())
        for field, value in (("active", False), ("enabled", False), ("recovering", True),
                             ("_closing", True), ("_claiming", True), ("takeover_pending", True),
                             ("handoff", {}), ("auxiliary_manual", True),
                             ("pending_auxiliaries", (self.b.heater,)), ("owned", ()),
                             ("heartbeat_monotonic", time.monotonic() - 10000)):
            with patch.object(controls, field, value):
                self.assertFalse(controls._guarded_recovery_watchdog(), field)
        # Generic explicit/cleanup stops remain real jobs even with a healthy
        # controller and a guard claiming to own routine recovery work.
        await controls._off(self.b.heater)
        self.assertEqual([c[1] for c in self.calls], ["set_hvac_mode"])
        self.assertEqual(controls.observations.counts["off_requested"], 1)

    async def test_powered_recovery_does_not_defer_unsafe_probe_stop(self):
        await self.prepare()
        controls = NativeControls(self.hass, replace(config(), heater_entity=self.b.heater),
                                  True, "unsafe-probe-watchdog", lambda: None)
        controls.active = controls.enabled = controls.enabled_intent = True
        controls.owned = (self.b.heater,)
        controls.heartbeat_monotonic = controls.auxiliary_authorized_at = time.monotonic()
        controls.power_guard = SimpleNamespace(
            recovery_isolation_confirmed=False, recovery_native_off_managed=True,
            emergency_fallback_enabled=False, power_cut_confirmed=False)
        controls.probes_safe = lambda _: False
        try:
            await controls.async_check()
            self.assertEqual([c[1] for c in self.calls], ["set_hvac_mode"])
            self.assertTrue(controls._auxiliary_probe_stop_pending)
        finally:
            controls.active = False
            await self.hass.async_block_till_done()
