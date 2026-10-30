"""Shutdown display lifecycle and retained commissioning metadata only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch

from house_climate.aggregate_stop import AggregateStopConfig
from tests import test_heater_power_guard as watts
from tests import test_power_guard as fixtures
from tests import test_power_guard_commissioning as commissioning


power = fixtures.power
B = fixtures.B
WATTS = watts.WATTS


class CommissioningDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = fixtures.Clock()
        self.hass = commissioning.CommissioningHass(self.clock)
        self.bindings = replace(watts.watt_bindings(), aggregate_stop_confirmation=AggregateStopConfig(
            minimum_drop_w=900, residual_ceiling_w=160, noise_margin_w=20,
            settle_seconds=30, timeout_seconds=180, max_age_seconds=90,
            minimum_samples=3, relative_drop_enabled=True))
        self.guard = self.new_guard()
        self.store = type("MemoryStore", (), {})()
        self.store.async_save = AsyncMock()
        self.guard._store = self.store
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for patcher in self.patchers:
            patcher.start()
        self.guard._observe()

    def new_guard(self):
        guard = power.HeaterPowerGuard(self.hass, self.bindings, live=True,
            entry_id="diagnostic-fixture", notify=lambda: None,
            clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        guard.commissioning_controller_off = lambda: True
        return guard

    async def asyncTearDown(self):
        self.hass.release.set()
        self.hass.release_switch_on.set()
        self.hass.release_cut.set()
        await self.guard.async_close()
        await asyncio.sleep(.01)
        for patcher in self.patchers:
            patcher.stop()

    def refresh(self):
        for entity, observed in list(self.hass.data.items()):
            self.hass.set(entity, observed["state"], observed["attributes"])

    async def complete_test(self):
        await self.guard.async_commission_relay(confirm=True)
        for _ in range(10):
            self.clock.advance(30)
            for entity in (B.input_power, B.output_power, B.battery,
                           B.wall_phase_a_voltage, B.wall_phase_b_voltage):
                observed = self.hass.data[entity]
                self.hass.set(entity, observed["state"], observed["attributes"])
            self.guard._observe()
            await asyncio.sleep(0)
        await asyncio.sleep(.09)
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assertFalse(self.guard.blocked)

    async def load_saved(self, saved):
        restarted = self.new_guard()
        store = type("RestartStore", (), {})()
        store.async_load = AsyncMock(return_value=deepcopy(saved))
        store.async_save = AsyncMock()
        storage = ModuleType("homeassistant.helpers.storage")
        storage.Store = lambda *args: store
        with patch.dict(sys.modules, {"homeassistant": ModuleType("homeassistant"),
                "homeassistant.helpers": ModuleType("homeassistant.helpers"),
                "homeassistant.helpers.storage": storage}):
            await restarted.async_load()
        return restarted

    async def test_success_is_idle_after_old_aggregate_evidence_times_out(self):
        await self.complete_test()
        terminal = deepcopy(self.guard.diagnostics["commissioning"])
        self.assertEqual(self.guard._shutdown_stage, "dyson_off")
        evidence = self.guard._aggregate_evidence
        self.assertIsNotNone(evidence)
        for _ in range(7):
            self.clock.advance(30)
            self.refresh()
            self.guard._observe()
            await asyncio.sleep(0)
        self.assertEqual(self.guard._aggregate_outcome.reason, "Aggregate confirmation timed out")
        self.assertIs(self.guard._aggregate_evidence, evidence)
        before = deepcopy(evidence.__dict__)
        status = self.guard.diagnostics
        self.assertFalse(status["shutdown_active"])
        self.assertEqual(status["shutdown_stage"], "idle")
        self.assertEqual(status["shutdown_confirmation"], "inactive")
        self.assertIsNone(status["aggregate_stop"])
        self.assertIs(status["electrical_stop_confirmed"], False)
        self.assertEqual(status["commissioning"], terminal)
        self.assertEqual(evidence.__dict__, before)
        self.assertFalse(self.guard.blocked)
        self.assertEqual(sum(call[1] == "turn_on" for call in self.hass.calls), 1)

    async def test_failed_shutdown_retains_unknown_evidence_and_manual_hold(self):
        await self.guard.async_commission_relay(confirm=True)
        self.guard._commissioning_failed("Synthetic shutdown verification failure")
        self.guard._aggregate_evidence = self.bindings.aggregate_stop_confirmation.create_evidence()
        self.guard._aggregate_evidence.begin(self.clock.wall, 150, self.clock.wall)
        self.clock.advance(181)
        self.guard._observe_aggregate_stop()
        self.guard._shutdown_stage = "dedicated_off"
        status = self.guard.diagnostics
        self.assertTrue(status["shutdown_active"])
        self.assertEqual(status["shutdown_stage"], "dedicated_off")
        self.assertEqual(status["aggregate_stop"]["status"], "unknown")
        self.assertEqual(status["aggregate_stop"]["reason"], "Aggregate confirmation timed out")
        self.assertEqual(status["commissioning"]["status"], "failed")
        self.assertTrue(status["manual_recovery_required"])
        self.assertTrue(self.guard.blocked)

    async def test_in_progress_native_stop_retains_its_evidence_without_a_trip(self):
        self.assertTrue(await self.guard._off_ack(.025))
        self.assertFalse(self.guard._latched)
        status = self.guard.diagnostics
        self.assertTrue(status["shutdown_active"])
        self.assertEqual(status["shutdown_stage"], "dyson_off")
        self.assertEqual(status["shutdown_confirmation"], "state_only")
        self.assertIsNotNone(status["aggregate_stop"])

    async def test_stale_blocked_startup_is_not_projected_as_inactive(self):
        self.hass.data[WATTS] = watts.report(self.clock, 0, age=181)
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["shutdown_active"])
        self.assertTrue(self.guard.blocked)
        self.assertIn("not current", self.guard.diagnostics["reason"])

    async def test_actual_pending_command_remains_an_active_context(self):
        self.guard._shutdown_stage = "dyson_off"
        task = asyncio.create_task(asyncio.Event().wait())
        self.guard._service_tasks.add(task)
        try:
            self.assertTrue(self.guard.diagnostics["shutdown_active"])
            self.assertEqual(self.guard.diagnostics["shutdown_stage"], "dyson_off")
        finally:
            self.guard._service_tasks.discard(task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_current_recovery_and_failure_contexts_are_not_hidden(self):
        self.guard._shutdown_stage = "dedicated_off"
        for flag in ("_latched", "_cut_owned", "_restoring", "_cutting", "_restore_intent",
                     "_compensation_pending", "_manual_recovery_required", "_emergency_ac_latched",
                     "_shutdown_contradicted"):
            with self.subTest(flag=flag):
                setattr(self.guard, flag, True)
                try:
                    status = self.guard.diagnostics
                    self.assertTrue(status["shutdown_active"])
                    self.assertEqual(status["shutdown_stage"], "dedicated_off")
                    self.assertEqual(status["shutdown_confirmation"], "unconfirmed")
                    self.assertTrue(getattr(self.guard, flag))
                finally:
                    setattr(self.guard, flag, False)

    async def test_terminal_result_timestamps_survive_restart_without_resuming(self):
        for result in ("passed", "failed", "interrupted"):
            with self.subTest(result=result):
                saved = self.guard._saved_state()
                saved["commissioning"] = {"active": False, "status": result,
                    "reason": "Synthetic prior result", "started_at": self.clock.wall - 400,
                    "finished_at": self.clock.wall - 20}
                restarted = await self.load_saved(saved)
                try:
                    status = restarted.diagnostics
                    self.assertEqual(status["commissioning"]["status"], result)
                    self.assertEqual(status["commissioning"]["started_at"], saved["commissioning"]["started_at"])
                    self.assertEqual(status["commissioning"]["finished_at"], saved["commissioning"]["finished_at"])
                    self.assertFalse(restarted.commissioning_active)
                    self.assertIsNone(restarted._commissioning_started_clock)
                    self.assertFalse(status["manual_recovery_required"])
                    self.assertEqual(self.hass.calls, [])
                finally:
                    await restarted.async_close()

    async def test_invalid_terminal_timestamps_are_null_without_new_control_latches(self):
        invalid_values = (None, True, False, -1, float("nan"), float("inf"),
                          float("-inf"), "1800000000", "bad", [], {}, 10 ** 400)
        for invalid in invalid_values:
            with self.subTest(invalid=repr(invalid)):
                saved = self.guard._saved_state()
                saved["commissioning"] = {"active": False, "status": "passed",
                    "started_at": invalid, "finished_at": invalid}
                restarted = await self.load_saved(saved)
                try:
                    status = restarted.diagnostics
                    self.assertIsNone(status["commissioning"]["started_at"])
                    self.assertIsNone(status["commissioning"]["finished_at"])
                    self.assertEqual(status["commissioning"]["status"], "passed")
                    self.assertFalse(status["latched"])
                    self.assertFalse(status["manual_recovery_required"])
                    self.assertFalse(restarted.commissioning_active)
                finally:
                    await restarted.async_close()

    async def test_restored_metadata_is_whitelisted_and_zero_timestamps_are_valid(self):
        saved = self.guard._saved_state()
        saved["commissioning"] = {"active": False, "status": "passed",
            "reason": "x" * 1000, "started_at": 0, "finished_at": 0,
            "restore_window_seconds": 999999, "maximum_seconds": 999999,
            "phase": "native_reconnect", "phase_started_at": 123,
            "session_deadline_at": 9999999999, "unbounded": ["x"] * 1000}
        restarted = await self.load_saved(saved)
        try:
            result = restarted.diagnostics["commissioning"]
            self.assertEqual(result["started_at"], 0)
            self.assertEqual(result["finished_at"], 0)
            self.assertEqual(len(result["reason"]), 240)
            self.assertEqual(result["restore_window_seconds"], power.RESTORE_ACK_SECONDS)
            self.assertEqual(result["maximum_seconds"], power.COMMISSIONING_MAX_SECONDS)
            self.assertFalse({"phase", "phase_started_at", "session_deadline_at", "unbounded"} & result.keys())
            self.assertIsNone(restarted._commissioning_started_clock)
            self.assertFalse(result["active"])
        finally:
            await restarted.async_close()

    async def test_interrupted_saved_active_test_keeps_original_restart_safety_policy(self):
        saved = self.guard._saved_state()
        saved["commissioning"] = {"active": True, "status": "restoring",
            "started_at": self.clock.wall - 100, "finished_at": None}
        restarted = await self.load_saved(saved)
        try:
            status = restarted.diagnostics
            self.assertEqual(status["commissioning"]["status"], "interrupted")
            self.assertEqual(status["commissioning"]["started_at"], self.clock.wall - 100)
            self.assertEqual(status["commissioning"]["finished_at"], self.clock.wall)
            self.assertTrue(status["latched"])
            self.assertTrue(status["manual_recovery_required"])
            self.assertFalse(restarted.commissioning_active)
        finally:
            await restarted.async_close()

    async def test_invalid_saved_status_still_requires_manual_recovery(self):
        saved = self.guard._saved_state()
        saved["commissioning"] = {"active": False, "status": "unrecognized",
            "started_at": 1, "finished_at": 2}
        restarted = await self.load_saved(saved)
        try:
            status = restarted.diagnostics
            self.assertEqual(status["commissioning"]["status"], "interrupted")
            self.assertTrue(status["manual_recovery_required"])
            self.assertFalse(restarted.commissioning_active)
        finally:
            await restarted.async_close()
