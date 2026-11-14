"""One-shot attended recovery from a real retained/exhausted cutoff state."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.house_climate import power_guard as power
from tests_homeassistant import test_staged_relay_commissioning as staged


class RetainedSupervisedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    invoke = staged.StagedRelayCommissioningTests.invoke
    report = staged.StagedRelayCommissioningTests.report
    tick = staged.StagedRelayCommissioningTests.tick
    external_reports = staged.StagedRelayCommissioningTests.external_reports
    wait_until = staged.StagedRelayCommissioningTests.wait_until
    asyncTearDown = staged.StagedRelayCommissioningTests.asyncTearDown

    async def asyncSetUp(self):
        await staged.StagedRelayCommissioningTests.asyncSetUp(self)
        self.guard._latched = self.guard._blocked = self.guard._cut_owned = True
        self.guard._manual_recovery_required = True
        self.guard._restore_attempts = 3
        self.guard._tripped_at = self.monotonic[0] - 600
        self.guard._trip_wall = self.source_wall - 600
        self.guard._manual_recovery_wall = self.source_wall - 1
        self.guard.async_read_recovery_feedback = AsyncMock(return_value="off")
        self.report(self.bindings.heater_switch, "off")
        self.report(self.bindings.heater, "unavailable")
        self.report(self.bindings.heater_power, "0", {"unit_of_measurement": "W"},
                    timestamp=self.source_wall - 1000)
        await self.hass.async_block_till_done()
        # Stable/cooldown policy is separately tested; do not spend a minute
        # wall-clock time per test. All energized supervision clocks stay real.
        self.timeouts.append(patch.object(power, "SAFE_LOAD_SECONDS", 0))
        self.timeouts[-1].start()
        self.calls.clear()

    def launch(self, **overrides):
        args = dict(confirm=True, operator_present=True, local_cutoff_available=True)
        args.update(overrides)
        return asyncio.create_task(self.guard.async_supervised_recovery(**args))

    async def begin(self):
        task = self.launch()
        await asyncio.wait_for(self.on_seen.wait(), 1)
        await asyncio.sleep(.025)
        self.assertTrue(self.guard.supervised_recovery_active, self.guard.diagnostics)
        self.assertTrue(self.guard.blocked)
        return task

    async def finish_failure(self, task):
        self.assertFalse(await asyncio.wait_for(task, 2))
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertEqual(self.guard._restore_attempts, 3)
        self.assertTrue(self.guard._saved_state()["supervised_recovery"]["used"])
        with self.assertRaises(RuntimeError):
            await self.launch()
        self.assertEqual(sum(service == "turn_on" for _, service, _ in self.calls), 1)

    async def test_retained_isolated_session_waits_for_minute_meter_then_native_off(self):
        self.publish_on_watts = False
        task = await self.begin()
        # Keep observing supply while still blind; a full cadence is allowed.
        await self.tick(20, watts=None)
        await self.tick(20, watts=None)
        self.assertTrue(self.guard.supervised_recovery_active)
        await self.tick(20, watts="1")
        self.assertTrue(self.guard.supervised_recovery_active)
        self.native_ready = True
        await self.tick(1, watts="1", native="off")
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.guard.diagnostics["supervised_recovery"]["status"], "passed")
        self.assertEqual(self.guard.diagnostics["supervised_recovery"]["ordinary_attempts"], 3)
        self.assertTrue(all(data.get("hvac_mode", "off") == "off" for _, _, data in self.calls))

    async def test_late_low_packet_cannot_erase_acquisition_timeout(self):
        self.publish_on_watts = False
        task = await self.begin()
        await self.tick(20, watts=None)
        await self.tick(20, watts=None)
        await self.tick(20, watts=None)
        await self.tick(11, watts="0")
        await self.finish_failure(task)

    async def test_high_then_good_report_is_terminal_and_cuts_immediately(self):
        task = await self.begin()
        self.report(self.bindings.heater_power, "1400", {"unit_of_measurement": "W"})
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.finish_failure(task)

    async def test_stale_same_timestamp_cannot_extend_supervision(self):
        task = await self.begin()
        await self.tick(20, watts=None)
        await self.tick(20, watts=None)
        await self.tick(20, watts=None)
        await self.tick(11, watts=None)
        await self.finish_failure(task)

    async def test_three_confirmations_and_concurrent_call_are_required(self):
        for field in ("confirm", "operator_present", "local_cutoff_available"):
            with self.assertRaises(RuntimeError):
                await self.launch(**{field: False})
        task = await self.begin()
        with self.assertRaises(RuntimeError):
            await self.launch()
        self.guard.commissioning_controller_off = lambda: False
        await self.tick(1)
        await self.finish_failure(task)

    async def test_restart_cannot_resume_or_replenish_used_session(self):
        task = await self.begin()
        snapshot = self.guard._saved_state()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.wait_until(lambda: not self.guard.commissioning_busy)
        clone = power.HeaterPowerGuard(self.hass, self.bindings, live=True,
            entry_id="restarted-supervised", notify=lambda: None)
        with patch("homeassistant.helpers.storage.Store") as store:
            store.return_value.async_load = AsyncMock(return_value=snapshot)
            store.return_value.async_save = AsyncMock()
            await clone.async_load()
            self.assertTrue(clone._supervised_recovery["used"])
            self.assertFalse(clone.supervised_recovery_active)
            self.assertTrue(clone._manual_recovery_required)
            self.assertEqual(clone._restore_attempts, 3)
            with self.assertRaises(RuntimeError):
                await clone.async_supervised_recovery(confirm=True, operator_present=True,
                                                       local_cutoff_available=True)
            await self.hass.async_block_till_done()

    async def test_failed_persistence_consumes_attempt_without_dispatch(self):
        self.guard._store.async_save = AsyncMock(side_effect=OSError("synthetic storage failure"))
        self.assertFalse(await self.launch())
        self.assertFalse(self.calls)
        self.assertEqual(self.guard._restore_attempts, 3)
        self.assertTrue(self.guard._supervised_recovery["used"])

    async def test_observer_pause_is_not_relaxed_to_meter_cadence(self):
        task = await self.begin()
        await self.tick(31, watts="1")
        await self.finish_failure(task)
        self.assertIn("observation deadline", self.guard.diagnostics["supervised_recovery"]["reason"])

    async def test_post_off_late_low_packet_cannot_erase_deadline(self):
        task = await self.begin()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_off(call):
            self.report(self.bindings.heater, "off", {"hvac_action": "off"})
            entered.set()
            await release.wait()
        self.hass.services.async_register("climate", "set_hvac_mode", slow_off)
        self.native_ready = True
        await self.tick(1, native="off")
        await entered.wait()
        # Keep valid cadence throughout. Pending actual handler completion
        # prevents early success, isolating the post-Off confirmation deadline.
        for _ in range(3):
            await self.tick(20, watts="1")
        await self.tick(11, watts="0")
        release.set()
        await self.finish_failure(task)
        self.assertIn("Post-command", self.guard.diagnostics["supervised_recovery"]["reason"])

    async def test_no_native_ack_still_cuts_despite_low_watts(self):
        self.ignore_native_ack = True
        task = await self.begin()
        self.native_ready = True
        await self.tick(1, native="off")
        await self.finish_failure(task)

    async def test_controller_enable_during_persistence_prevents_on(self):
        original = self.guard._persist_restore_intent
        async def changed():
            result = await original()
            self.guard.commissioning_controller_off = lambda: False
            return result
        self.guard._persist_restore_intent = changed
        self.assertFalse(await self.launch())
        self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
        self.assertEqual(self.guard._restore_attempts, 3)

    async def test_fresh_session_stability_and_existing_cooldown_both_required(self):
        self.guard._tripped_at = self.monotonic[0] - 270
        with patch.object(power, "SAFE_LOAD_SECONDS", 60):
            task = self.launch()
            await self.wait_until(lambda: self.guard.diagnostics["supervised_recovery"].get("phase") == "waiting_for_recovery")
            for _ in range(2):
                await self.tick(20, watts=None)
                await asyncio.sleep(.11)
            self.assertFalse(self.on_seen.is_set())
            await self.tick(21, watts=None)
            await asyncio.wait_for(self.on_seen.wait(), 1)
            self.guard.commissioning_controller_off = lambda: False
            await self.tick(1)
            await self.finish_failure(task)

    async def test_pending_native_off_does_not_delay_high_load_cut(self):
        task = await self.begin()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(call):
            entered.set()
            await release.wait()
        self.hass.services.async_register("climate", "set_hvac_mode", blocked)
        self.native_ready = True
        await self.tick(1, native="off")
        await entered.wait()
        self.report(self.bindings.heater_power, "1400", {"unit_of_measurement": "W"})
        try:
            await self.wait_until(lambda: self.hass.states.get(self.bindings.heater_switch).state == "off")
        finally:
            release.set()
        await self.finish_failure(task)

    async def test_late_uncancellable_on_is_compensated_after_failed_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_on(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            self.report(self.bindings.heater_switch, "on")
        self.hass.services.async_register("switch", "turn_on", delayed_on)
        task = self.launch()
        await entered.wait()
        self.assertFalse(await asyncio.wait_for(task, 2))
        cuts_before = sum(service == "turn_off" for _, service, _ in self.calls)
        self.assertGreater(cuts_before, 0)
        release.set()
        await self.wait_until(lambda: not self.guard._positive_tasks and not self.guard._compensation_pending)
        self.assertGreater(sum(service == "turn_off" for _, service, _ in self.calls), cuts_before)
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertEqual(self.guard._restore_attempts, 3)
        self.assertTrue(self.guard._supervised_recovery["used"])

    async def test_on_handler_latency_does_not_extend_first_meter_deadline(self):
        self.publish_on_watts = False
        async def slow_result(call):
            await self.staged_service(call)
            await asyncio.sleep(0)  # Deliver the On event before its handler returns.
            self.monotonic[0] += 5
            self.source_wall += 5
        self.hass.services.async_register("switch", "turn_on", slow_result)
        task = await self.begin()
        watch = self.guard._supervised_restore
        self.assertLess(watch["on_clock"] - watch["requested_clock"], .1)
        for _ in range(3):
            await self.tick(20, watts=None)
        await self.tick(6, watts="0")
        await self.finish_failure(task)

    async def test_adverse_then_good_supply_event_during_preparation_is_terminal(self):
        with patch.object(power, "SAFE_LOAD_SECONDS", 60):
            task = self.launch()
            await self.wait_until(lambda: self.guard.diagnostics["supervised_recovery"].get("phase") == "waiting_for_recovery")
            self.report(self.bindings.battery, "30", {"unit_of_measurement": "%"})
            self.report(self.bindings.battery, "90", {"unit_of_measurement": "%"})
            self.assertFalse(await asyncio.wait_for(task, 2))
            self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
            self.assertEqual(self.guard._restore_attempts, 3)

    async def test_long_read_or_intent_stall_cannot_reset_preparation_observer(self):
        async def delayed_read(entity):
            self.monotonic[0] += 31
            self.source_wall += 31
            return "off"
        self.guard.async_read_recovery_feedback = delayed_read
        self.assertFalse(await self.launch())
        self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
        self.assertIn("observation deadline", self.guard.diagnostics["supervised_recovery"]["reason"])

    async def test_long_restore_intent_stall_cannot_reset_observation_clock(self):
        original = self.guard._persist_restore_intent
        async def delayed_intent():
            result = await original()
            self.monotonic[0] += 31
            self.source_wall += 31
            return result
        self.guard._persist_restore_intent = delayed_intent
        self.assertFalse(await self.launch())
        self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
        self.assertIn("observation deadline", self.guard.diagnostics["supervised_recovery"]["reason"])

    async def test_cancellation_during_preparation_consumes_grant_without_on(self):
        with patch.object(power, "SAFE_LOAD_SECONDS", 60):
            task = self.launch()
            await self.wait_until(lambda: self.guard.diagnostics["supervised_recovery"].get("phase") == "waiting_for_recovery")
            snapshot = self.guard._saved_state()
            self.assertTrue(snapshot["supervised_recovery"]["used"])
            self.assertTrue(snapshot["manual_recovery_required"])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.wait_until(lambda: not self.guard.commissioning_busy)
            self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
            self.assertEqual(self.guard._restore_attempts, 3)
            with self.assertRaises(RuntimeError):
                await self.launch()

    async def test_restored_external_event_cannot_hide_behind_trusted_snapshot(self):
        with patch.object(power, "SAFE_LOAD_SECONDS", 60):
            task = self.launch()
            await self.wait_until(lambda: self.guard.diagnostics["supervised_recovery"].get("phase") == "waiting_for_recovery")
            self.report(self.bindings.output_power, "150", {"unit_of_measurement": "W", "restored": True})
            self.report(self.bindings.output_power, "150", {"unit_of_measurement": "W"})
            self.assertFalse(await asyncio.wait_for(task, 2))
            self.assertNotIn("turn_on", [service for _, service, _ in self.calls])
            self.assertEqual(self.guard.diagnostics["supervised_recovery"]["status"], "failed")
            self.assertTrue(self.guard.diagnostics["supervised_recovery"]["reason"])
