"""Explicit recovery of a saved cutoff whose dedicated relay is already On."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_power_guard as base
from tests import test_heater_power_guard as watts

power, B, WATTS = base.power, base.B, watts.WATTS


class RetainedPowerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = base.Clock()
        self.hass = watts.WattHass(self.clock)
        self.guards = []
        self.writes = []
        guard = self.new_guard()
        self.saved = {**guard._saved_state(), "latched": True, "switch_cut_owned": True,
                      "trip_wall": self.clock.wall - 600}
        self.store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: deepcopy(self.saved)),
                                     async_save=AsyncMock(side_effect=self.save))
        self.guard = guard
        await self.load(guard)

    async def asyncTearDown(self):
        for guard in self.guards:
            # These tests only authorize synthetic Preview acknowledgement.
            guard.live = False
            await guard.async_close()

    def new_guard(self, *, bindings=None):
        guard = power.HeaterPowerGuard(self.hass, bindings or watts.watt_bindings(),
            live=False, entry_id="retained-fixture", notify=lambda: None,
            clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        guard.recovery_controller_off = lambda: True
        self.guards.append(guard)
        return guard

    async def load(self, guard):
        with patch.dict(sys.modules, {"homeassistant.helpers.storage":
                SimpleNamespace(Store=lambda *args: self.store)}):
            await guard.async_load()

    async def save(self, data):
        self.saved = deepcopy(data)
        self.writes.append(deepcopy(data))

    def refresh(self):
        self.hass.refresh()

    async def rejected(self):
        writes = len(self.writes)
        with self.assertRaises(RuntimeError):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertFalse(self.guard._off_confirmed)
        self.assertIsNone(self.guard._manual_off_wall)
        self.assertEqual(len(self.writes), writes)
        self.assertEqual(self.hass.calls, [])

    async def test_already_on_claim_recovers_explicitly_in_preview_and_survives_reload(self):
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["retained_cut_recovery_pending"])
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        await self.rejected()  # Cached Off/low values predate this load.
        self.refresh()
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertFalse(self.saved["switch_cut_owned"])
        self.assertTrue(self.saved["latched"])  # Cooldown is not acknowledged away.
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard._off_confirmed)
        before = deepcopy(self.guard._saved_state())
        diagnostic = self.guard.diagnostics
        self.assertEqual(diagnostic["shutdown_confirmation"], "manual_recovery_verified")
        self.assertEqual(diagnostic["shutdown_evidence"], "corroborated_off")
        self.assertTrue(diagnostic["electrical_stop_confirmed"])
        self.assertTrue(diagnostic["shutdown_active"])
        self.assertEqual(diagnostic["shutdown_stage"], "idle")
        self.assertTrue(self.guard.shutdown_confirmed)
        # The display must not invent an Off command or alter control/storage.
        self.assertIsNone(self.guard._off_request_wall)
        self.assertEqual(self.guard._shutdown_confirmation, "unconfirmed")
        self.assertEqual(self.guard._saved_state(), before)
        self.assertEqual(len(self.writes), 1)
        await self.guard.async_close()
        restarted = self.new_guard()
        await self.load(restarted)
        restarted._observe()
        self.assertFalse(restarted.switch_cut_owned)
        self.assertFalse(restarted.diagnostics["retained_cut_recovery_pending"])
        self.assertTrue(restarted.blocked)
        self.assertFalse(restarted._off_confirmed)  # Restart does not reuse stop proof.
        self.assertEqual(restarted.diagnostics["shutdown_confirmation"], "unconfirmed")
        self.assertEqual(self.hass.calls, [])

    async def test_recovered_confirmation_requires_current_native_and_meter_evidence(self):
        self.refresh()
        # Good readings alone do not acknowledge the retained ownership claim.
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        for entity in (B.heater, WATTS):
            original = deepcopy(self.hass.data[entity])
            for case in ("stale", "restored", "assumed_state", "missing", "contradictory"):
                with self.subTest(entity=entity, case=case):
                    self.hass.data[entity] = deepcopy(original)
                    if case == "stale":
                        self.hass.data[entity]["last_reported"] = datetime.fromtimestamp(
                            self.clock.wall - 181, timezone.utc).isoformat()
                    elif case in ("restored", "assumed_state"):
                        self.hass.data[entity]["attributes"][case] = True
                    elif case == "missing":
                        self.hass.data.pop(entity)
                    else:
                        self.hass.set(entity, "heat" if entity == B.heater else 1500)
                    self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
                    self.assertTrue(self.guard.blocked)
            self.hass.data[entity] = original
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "manual_recovery_verified")
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(len(self.writes), 1)

    async def test_observed_contradiction_does_not_keep_manual_confirmation(self):
        self.refresh()
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.hass.set(WATTS, 1500)
        self.guard._observe()
        self.assertEqual(self.guard.diagnostics["shutdown_evidence"], "contradictory")
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
        self.assertFalse(self.guard.shutdown_confirmed)
        self.assertTrue(self.guard.blocked)
        # Later physical isolation can satisfy the stop gate, but it must not
        # erase the contradiction or revive an earlier manual-recovery label.
        self.hass.set(WATTS, 1)
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard.shutdown_confirmed)
        self.assertEqual(self.guard.diagnostics["shutdown_evidence"], "contradictory")
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
        self.assertEqual(self.hass.calls, [])

    async def test_new_off_command_replaces_manual_recovery_diagnostic(self):
        self.refresh()
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.guard.live = True  # Synthetic service registry only.
        self.assertTrue(await self.guard._off_ack(base.TEST_OFF_ACK_SECONDS))
        self.assertIsNone(self.guard._manual_off_wall)
        self.assertIsNotNone(self.guard._off_request_wall)
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertEqual(self.hass.calls, [("climate", "set_hvac_mode",
            {"entity_id": B.heater, "hvac_mode": "off"})])

    async def test_preview_observation_and_reload_alone_do_not_clear_saved_claim(self):
        self.refresh()
        self.guard._observe()
        await asyncio.sleep(0)
        self.assertEqual(self.writes, [])
        await self.guard.async_close()
        restarted = self.new_guard()
        await self.load(restarted)
        self.assertTrue(restarted.switch_cut_owned)
        self.assertTrue(restarted.diagnostics["retained_cut_recovery_pending"])
        with self.assertRaises(RuntimeError):
            await restarted.async_acknowledge_emergency()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.hass.calls, [])

    async def test_acknowledgement_does_not_shortcut_recent_trip_cooldown(self):
        self.saved["trip_wall"] = self.clock.wall
        self.saved.pop("supply_policy_version", None)  # Legacy cooldown survives migration.
        await self.load(self.guard)
        self.refresh()
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        # A current guard can subsequently run Live without any acknowledgement
        # command. Production options changes reload and verify Off anew.
        self.guard.live = True
        self.guard._observe()
        for elapsed in (61, 120, 180, 240, 299):
            self.clock.advance(elapsed - (self.clock.now - 1000))
            self.refresh()
            self.guard._observe()
            self.assertTrue(self.guard.blocked)
            self.assertFalse(self.guard.switch_cut_owned)
        self.clock.advance(2)
        self.refresh()
        self.guard._observe()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.calls, [])

    async def test_matching_legacy_current_meter_can_supply_post_restart_stop_proof(self):
        guard = self.new_guard(bindings=B)
        self.saved["binding_identity"] = guard._binding_identity()
        await self.load(guard)
        self.hass.set(B.heater_current, .05, {"unit_of_measurement": "A"})
        self.refresh()
        self.assertTrue(await guard.async_acknowledge_emergency())
        self.assertFalse(guard.switch_cut_owned)
        self.assertFalse(self.saved["switch_cut_owned"])
        self.assertEqual(self.hass.calls, [])

    async def test_old_or_untrusted_native_meter_and_relay_evidence_is_rejected(self):
        for entity in (B.heater, WATTS, B.heater_switch):
            for case in ("before_load", "stale", "future", "restored", "assumed_state", "missing"):
                with self.subTest(entity=entity, case=case):
                    self.refresh()
                    original = deepcopy(self.hass.data[entity])
                    if case in ("restored", "assumed_state"):
                        self.hass.data[entity]["attributes"][case] = True
                    elif case == "missing":
                        self.hass.data.pop(entity)
                    else:
                        stamp = {"before_load": self.guard._retained_cut_claim_after,
                                 "stale": self.clock.wall - 181, "future": self.clock.wall + 6}[case]
                        self.hass.data[entity]["last_reported"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                        if entity == B.heater_switch:
                            self.hass.data[entity]["attributes"]["device_report"]["reported_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                    await self.rejected()
                    self.hass.data[entity] = original

    async def test_unsafe_or_invalid_electrical_inputs_are_rejected(self):
        cases = ((B.battery, 40), (B.battery, 39), (B.battery, "unavailable"),
                 (B.output_power, "NaN"),
                 (WATTS, 100), (WATTS, 1500), (WATTS, "NaN"), (WATTS, "unavailable"),
                 (B.heater, "heat"), (B.heater_switch, "off"))
        for entity, value in cases:
            with self.subTest(entity=entity, value=value):
                self.refresh()
                original = deepcopy(self.hass.data[entity])
                self.hass.set(entity, value)
                await self.rejected()
                self.hass.data[entity] = original
        for entity in (B.output_power, B.battery, B.ac_output_switch):
            for flag in ("restored", "assumed_state"):
                with self.subTest(entity=entity, flag=flag):
                    self.refresh()
                    self.hass.data[entity]["attributes"][flag] = True
                    await self.rejected()
                    del self.hass.data[entity]["attributes"][flag]

    async def test_retained_acknowledgement_has_no_watt_ceiling_or_circuit_dependency(self):
        self.refresh()
        self.hass.set(B.output_power, 5000)
        self.hass.set(B.input_power, 5000)
        self.hass.set(B.circuit_power, "unavailable")
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.calls, [])

    async def test_pending_jobs_controller_activity_and_missing_storage_are_rejected(self):
        self.refresh()
        pending = asyncio.create_task(asyncio.Event().wait())
        try:
            for field in ("_action_task", "_save_task", "_commissioning_task", "_compensating_task"):
                with self.subTest(field=field):
                    setattr(self.guard, field, pending)
                    await self.rejected()
                    setattr(self.guard, field, None)
            for field in ("_positive_tasks", "_service_tasks", "_emergency_service_tasks"):
                with self.subTest(field=field):
                    getattr(self.guard, field).add(pending)
                    await self.rejected()
                    getattr(self.guard, field).clear()
            for field in ("_restoring", "_cutting", "_restore_intent", "_compensation_pending",
                          "_commissioning_preparing", "_closing"):
                with self.subTest(field=field):
                    setattr(self.guard, field, True)
                    await self.rejected()
                    setattr(self.guard, field, False)
            self.guard.pending_external_jobs = lambda: (pending,)
            await self.rejected()
            self.guard.pending_external_jobs = lambda: ()
            self.guard.recovery_controller_off = lambda: False
            await self.rejected()
            self.guard.recovery_controller_off = lambda: True
            self.guard._store = None
            await self.rejected()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_changed_bindings_and_missing_meter_cannot_use_retained_recovery(self):
        self.refresh()
        for changes in ({"heater_power": None}, {"heater_power": "sensor.other_watts"},
                        {"heater_switch": "switch.other_cutoff"}):
            original = self.guard.bindings
            self.guard.bindings = replace(original, **changes)
            await self.rejected()
            self.guard.bindings = original
        self.saved["binding_identity"]["heater"] = "climate.other_heater"
        changed = self.new_guard()
        await self.load(changed)
        self.refresh()
        with self.assertRaises(RuntimeError):
            await changed.async_acknowledge_emergency()
        self.assertTrue(changed.diagnostics["binding_identity_mismatch"])
        self.assertTrue(changed.diagnostics["manual_recovery_required"])
        self.assertEqual(self.writes, [])

    async def test_relay_reconnection_requires_new_native_and_meter_reports(self):
        self.refresh()
        self.hass.set(B.heater_switch, "off")
        self.hass.set(B.heater_switch, "on")
        await self.rejected()
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        await self.rejected()
        self.hass.set(WATTS, 1)
        self.assertTrue(await self.guard.async_acknowledge_emergency())

    async def test_failed_save_retains_claim_and_retry_requires_durable_success(self):
        self.refresh()
        self.store.async_save.side_effect = OSError("synthetic storage unavailable")
        await self.rejected()
        self.assertIsNotNone(self.guard.diagnostics["persistence_error"])
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
        self.assertTrue(self.saved["switch_cut_owned"])
        self.store.async_save.side_effect = self.save
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertIsNone(self.guard.diagnostics["persistence_error"])
        self.assertFalse(self.saved["switch_cut_owned"])

    async def test_pending_write_retains_ownership_and_caller_cancellation_does_not_cancel_save(self):
        self.refresh()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_save(data):
            entered.set()
            await release.wait()
            await self.save(data)

        self.store.async_save.side_effect = held_save
        caller = asyncio.create_task(self.guard.async_acknowledge_emergency())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertFalse(self.guard._off_confirmed)
        self.assertTrue(self.saved["switch_cut_owned"])
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        self.assertTrue(await asyncio.wait_for(self.guard._recovery_task, 1))
        self.assertFalse(self.saved["switch_cut_owned"])
        self.assertEqual(self.hass.calls, [])

    async def test_evidence_or_jobs_changing_during_save_restore_durable_inhibition(self):
        for case in ("expiry", "heat", "high_watts", "relay_cycle", "jobs", "controller", "bindings"):
            with self.subTest(case=case):
                self.refresh()
                original_bindings = self.guard.bindings
                original_states = deepcopy(self.hass.data)
                entered, release = asyncio.Event(), asyncio.Event()

                async def held_save(data):
                    await self.save(data)
                    if not data["switch_cut_owned"]:
                        entered.set()
                        await release.wait()

                self.store.async_save.side_effect = held_save
                caller = asyncio.create_task(self.guard.async_acknowledge_emergency())
                await asyncio.wait_for(entered.wait(), 1)
                if case == "expiry":
                    self.clock.advance(181)
                elif case == "heat":
                    self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
                elif case == "high_watts":
                    self.hass.set(WATTS, 1500)
                elif case == "relay_cycle":
                    self.hass.set(B.heater_switch, "off")
                    self.hass.set(B.heater_switch, "on")
                elif case == "jobs":
                    self.guard.pending_external_jobs = lambda: (object(),)
                elif case == "controller":
                    self.guard.recovery_controller_off = lambda: False
                else:
                    self.guard.bindings = replace(self.guard.bindings, heater_power="sensor.other_watts")
                release.set()
                with self.assertRaises(RuntimeError):
                    await caller
                self.assertTrue(self.guard.switch_cut_owned)
                self.assertTrue(self.saved["switch_cut_owned"])
                self.assertTrue(self.saved["latched"])
                self.assertFalse(self.guard._off_confirmed)
                self.assertEqual(self.hass.calls, [])
                self.guard.bindings = original_bindings
                self.hass.data.clear()
                self.hass.data.update(original_states)
                self.guard.pending_external_jobs = lambda: ()
                self.guard.recovery_controller_off = lambda: True

    async def test_read_only_physical_reply_recovers_unchanged_relay_without_cache_write(self):
        self.refresh()
        self.hass.data[B.heater_switch]['attributes'].pop('device_report')
        old = deepcopy(self.hass.data[B.heater_switch])
        self.guard.async_read_recovery_feedback = AsyncMock(return_value='on')
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.guard.async_read_recovery_feedback.assert_awaited_once_with(B.heater_switch)
        self.assertEqual(self.hass.data[B.heater_switch], old)
        self.assertEqual(self.hass.calls, [])
        self.assertFalse(self.saved['switch_cut_owned'])

    async def test_physical_read_failure_does_not_reuse_an_earlier_internal_reply(self):
        self.refresh()
        self.hass.data[B.heater_switch]['attributes'].pop('device_report')
        self.guard.async_read_recovery_feedback = AsyncMock(return_value='on')
        await self.guard._refresh_recovery_feedback()
        self.assertIn(B.heater_switch, self.guard._recovery_feedback)
        self.guard.async_read_recovery_feedback.side_effect = TimeoutError()
        await self.rejected()
        self.assertNotIn(B.heater_switch, self.guard._recovery_feedback)

    async def test_relay_transition_during_read_cannot_authenticate_older_on_reply(self):
        self.refresh()
        self.hass.data[B.heater_switch]['attributes'].pop('device_report')
        async def read(entity):
            self.hass.set(entity, 'off')
            self.hass.set(entity, 'on')
            self.hass.data[entity]['attributes'].pop('device_report')
            return 'on'
        self.guard.async_read_recovery_feedback = read
        await self.rejected()
        self.assertNotIn(B.heater_switch, self.guard._recovery_feedback)


if __name__ == "__main__":
    unittest.main()
