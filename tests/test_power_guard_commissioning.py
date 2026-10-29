"""Explicit dedicated-relay commissioning, using only synthetic equipment."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch
import unittest

from tests import test_power_guard as fixtures
from tests import test_heater_power_guard as watts


power = fixtures.power
B = fixtures.B
WATTS = watts.WATTS


class CommissioningHass(watts.WattHass):
    def __init__(self, clock):
        super().__init__(clock)
        self.post_off_watts = 1
        self.persistence_snapshots = []
        self.saved_before_cut = None
        self.hang_cut = False
        self.cut_entered = asyncio.Event()
        self.release_cut = asyncio.Event()
        self.post_off_current = None
        self.startup_watts = 1

    async def service(self, domain, service, body, blocking):
        if domain == "switch" and service == "turn_off":
            if self.saved_before_cut is None:
                self.saved_before_cut = deepcopy(self.persistence_snapshots)
            self.cut_entered.set()
            if self.hang_cut:
                await self.release_cut.wait()
        await super().service(domain, service, body, blocking)
        if domain == "switch" and service == "turn_off" and not self.ignore_relay_off:
            self.set(WATTS, 0, {"unit_of_measurement": "W"})
        if domain == "switch" and service == "turn_on" and not self.ignore_switch_on_feedback:
            self.set(WATTS, self.startup_watts, {"unit_of_measurement": "W"})
        if domain == "climate" and not self.ignore_climate and self.post_off_current is not None:
            self.set(B.heater_current, self.post_off_current, {"unit_of_measurement": "A"})


class RelayCommissioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = fixtures.Clock()
        self.hass = CommissioningHass(self.clock)
        self.bindings = watts.watt_bindings()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="commission-fixture",
            notify=lambda: None, clock=lambda: self.clock.now,
            wall_clock=lambda: self.clock.wall)
        self.guard.commissioning_controller_off = lambda: True
        self.controller_off = True
        self.guard.commissioning_controller_off = lambda: self.controller_off

        async def save(data):
            self.hass.persistence_snapshots.append(deepcopy(data))

        self.guard._store = type("MemoryStore", (), {})()
        self.guard._store.async_save = AsyncMock(side_effect=save)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for patcher in self.patchers:
            patcher.start()
        self.guard._observe()
        self.assertFalse(self.guard.blocked)

    async def asyncTearDown(self):
        self.hass.release.set()
        self.hass.release_switch_on.set()
        self.hass.release_cut.set()
        await self.guard.async_close()
        await asyncio.sleep(.01)
        for patcher in self.patchers:
            patcher.stop()

    async def settle(self):
        await asyncio.sleep(.09)

    def refresh_external(self):
        """No fabricated packets from the de-energized dedicated meter."""
        for entity in (B.input_power, B.output_power, B.battery,
                       B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            old = self.hass.data[entity]
            self.hass.set(entity, old["state"], old["attributes"])

    async def observe_for(self, seconds):
        target = self.clock.now + seconds
        while self.clock.now < target:
            self.clock.advance(min(30, target - self.clock.now))
            self.refresh_external()
            self.guard._observe()
            await asyncio.sleep(0)

    async def start(self):
        result = await self.guard.async_commission_relay(confirm=True)
        await self.settle()
        return result

    def assert_only_dedicated_equipment(self):
        self.assertTrue(all(call[2]["entity_id"] in {B.heater, B.heater_switch}
                            for call in self.hass.calls))
        self.assertTrue(all(call[2].get("hvac_mode", "off") == "off"
                            for call in self.hass.calls))

    async def test_explicit_boolean_confirmation_is_required_before_any_effect(self):
        for confirm in (False, None, 0, 1, "true"):
            with self.subTest(confirm=confirm):
                with self.assertRaises((RuntimeError, ValueError)):
                    await self.guard.async_commission_relay(confirm=confirm)
                self.assertEqual(self.hass.calls, [])
                self.assertEqual(self.hass.persistence_snapshots, [])
                self.assertFalse(self.guard.diagnostics["latched"])

    async def test_missing_confirmation_does_not_default_to_consent(self):
        with self.assertRaises((RuntimeError, ValueError, TypeError)):
            await self.guard.async_commission_relay()
        self.assertEqual(self.hass.calls, [])

    async def test_controller_off_capability_is_fail_closed_until_wired(self):
        fresh = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="not-wired",
            notify=lambda: None, clock=lambda: self.clock.now,
            wall_clock=lambda: self.clock.wall)
        try:
            fresh._observe()
            with self.assertRaises(RuntimeError):
                await fresh.async_commission_relay(confirm=True)
            self.assertEqual(self.hass.calls, [])
        finally:
            await fresh.async_close()

    async def test_live_and_disabled_comfort_controller_are_both_required(self):
        for live, controller_off in ((False, True), (True, False)):
            with self.subTest(live=live, controller_off=controller_off):
                self.guard.live = live
                self.controller_off = controller_off
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
        self.guard.live = self.controller_off = True

    async def test_heating_or_unknown_native_state_cannot_enter_commissioning(self):
        for state, action in (("heat", "heating"), ("heat", "idle"),
                              ("unavailable", None), ("unknown", None)):
            with self.subTest(state=state, action=action):
                self.hass.set(B.heater, state, {"hvac_action": action})
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])

    async def test_low_power_and_trustworthy_native_off_are_independent_entry_checks(self):
        for entity, value, attrs in (
                (WATTS, "100", {"unit_of_measurement": "W"}),
                (WATTS, "unavailable", {"unit_of_measurement": "W"}),
                (WATTS, "0", {"unit_of_measurement": "W", "restored": True}),
                (B.heater, "off", {"hvac_action": "off", "assumed_state": True}),
                (B.heater, "off", {"hvac_action": "off", "restored": True})):
            with self.subTest(entity=entity, value=value, attrs=attrs):
                saved = deepcopy(self.hass.data)
                self.hass.set(entity, value, attrs)
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
                self.hass.data.clear()
                self.hass.data.update(saved)

    async def test_stale_native_or_meter_report_is_not_an_entry_proof(self):
        for entity in (B.heater, WATTS):
            with self.subTest(entity=entity):
                saved = deepcopy(self.hass.data[entity])
                self.hass.data[entity]["last_reported"] = datetime.fromtimestamp(
                    self.clock.wall - 181, timezone.utc).isoformat()
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
                self.hass.data[entity] = saved

    async def test_restored_or_unavailable_relay_cannot_authorize_a_test_cut(self):
        for state, attrs in (("off", {}), ("unavailable", {}),
                              ("on", {"restored": True}),
                              ("on", {"assumed_state": True})):
            with self.subTest(state=state, attrs=attrs):
                self.hass.set(B.heater_switch, state, attrs)
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])

    async def test_battery_input_output_and_phase_voltages_remain_authoritative(self):
        for entity, value in ((B.battery, "40"), (B.input_power, "1300"),
                               (B.output_power, "1600"),
                               (B.wall_phase_a_voltage, "unavailable")):
            with self.subTest(entity=entity, value=value):
                saved = deepcopy(self.hass.data[entity])
                self.hass.set(entity, value)
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
                self.hass.data[entity] = saved

    async def test_assumed_or_restored_authoritative_external_values_cannot_enter_a_test(self):
        for entity in (B.input_power, B.output_power, B.battery):
            for flag in ("restored", "assumed_state"):
                with self.subTest(entity=entity, flag=flag):
                    saved = deepcopy(self.hass.data[entity])
                    self.hass.set(entity, saved["state"], {**saved["attributes"], flag: True})
                    with self.assertRaises(RuntimeError):
                        await self.guard.async_commission_relay(confirm=True)
                    self.assertEqual(self.hass.calls, [])
                    self.assertEqual(self.hass.persistence_snapshots, [])
                    self.hass.data[entity] = saved

    async def test_assumed_or_restored_external_values_interrupt_waiting_and_cannot_resume(self):
        for entity in (B.input_power, B.output_power, B.battery):
            for flag in ("restored", "assumed_state"):
                with self.subTest(entity=entity, flag=flag):
                    clock = fixtures.Clock()
                    hass = CommissioningHass(clock)
                    guard = power.HeaterPowerGuard(
                        hass, self.bindings, live=True, entry_id="external-trust-fixture",
                        notify=lambda: None, clock=lambda: clock.now,
                        wall_clock=lambda: clock.wall)
                    guard.commissioning_controller_off = lambda: True

                    async def save(data):
                        hass.persistence_snapshots.append(deepcopy(data))

                    guard._store = type("MemoryStore", (), {})()
                    guard._store.async_save = AsyncMock(side_effect=save)
                    try:
                        guard._observe()
                        await guard.async_commission_relay(confirm=True)
                        before = deepcopy(hass.data[entity])
                        hass.set(entity, before["state"], {**before["attributes"], flag: True})
                        guard._observe()
                        await self.settle()
                        self.assertFalse(guard.commissioning_active)
                        self.assertTrue(guard.diagnostics["manual_recovery_required"])
                        hass.set(entity, before["state"], before["attributes"])
                        for _ in range(12):
                            clock.advance(30)
                            for source in (B.input_power, B.output_power, B.battery,
                                           B.wall_phase_a_voltage, B.wall_phase_b_voltage):
                                old = hass.data[source]
                                hass.set(source, old["state"], old["attributes"])
                            guard._observe()
                            await asyncio.sleep(0)
                        await self.settle()
                        self.assertNotIn("turn_on", [c[1] for c in hass.calls])
                        self.assertTrue(all(c[2]["entity_id"] in {B.heater, B.heater_switch}
                                            for c in hass.calls))
                    finally:
                        await guard.async_close()

    async def test_assumed_or_restored_advisory_circuit_does_not_interrupt_waiting(self):
        await self.start()
        for flag in ("restored", "assumed_state"):
            with self.subTest(flag=flag):
                self.hass.set(B.circuit_power, "5000", {"unit_of_measurement": "W", flag: True})
                self.guard._observe()
                await asyncio.sleep(0)
                self.assertTrue(self.guard.commissioning_active)
                self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
                self.assertEqual(self.guard.diagnostics["wall_power_budget"]["qualified_sources"], [])

    async def test_advisory_circuit_is_not_a_new_commissioning_interlock(self):
        self.hass.set(B.circuit_power, "5000")
        await self.start()
        self.assertTrue(self.guard.commissioning_active)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assert_only_dedicated_equipment()

    async def test_direct_path_or_no_selected_meter_cannot_start(self):
        for bindings in (replace(self.bindings, heater_power=None),
                         replace(self.bindings, heater_switch=None,
                                 emergency_ac_switch="switch.fixture_shared_ac")):
            with self.subTest(bindings=bindings):
                original = self.guard.bindings
                self.guard.bindings = bindings
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
                self.guard.bindings = original

    async def test_enabled_shared_ac_fallback_is_rejected_without_changing_it(self):
        self.guard.bindings = replace(
            self.bindings, emergency_ac_switch="switch.fixture_shared_ac",
            emergency_ac_fallback_enabled=True)
        with self.assertRaises(RuntimeError):
            await self.guard.async_commission_relay(confirm=True)
        self.assertTrue(self.guard.bindings.emergency_ac_fallback_enabled)
        self.assertEqual(self.hass.calls, [])

    async def test_existing_latches_claims_cleanup_and_jobs_cannot_be_overridden(self):
        for attribute, value in (("_latched", True), ("_cut_owned", True),
                                  ("_manual_recovery_required", True),
                                  ("_identity_mismatch", True),
                                  ("_compensation_pending", True),
                                  ("_persistence_error", "synthetic storage error")):
            with self.subTest(attribute=attribute):
                old = getattr(self.guard, attribute)
                setattr(self.guard, attribute, value)
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])
                setattr(self.guard, attribute, old)
        self.guard.pending_external_jobs = lambda: (object(),)
        with self.assertRaises(RuntimeError):
            await self.guard.async_commission_relay(confirm=True)
        self.assertEqual(self.hass.calls, [])
        self.guard.pending_external_jobs = lambda: ()

    async def test_persists_deliberate_test_intent_before_the_actual_dedicated_cut(self):
        await self.start()
        self.assertTrue(self.hass.saved_before_cut)
        intent = self.hass.saved_before_cut[-1]
        self.assertTrue(intent["commissioning"]["active"])
        self.assertEqual(intent["commissioning"]["status"], "preparing")
        self.assertTrue(intent["latched"])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.data[B.input_power]["state"], "130")
        self.assertEqual(self.hass.data[B.output_power]["state"], "150")
        self.assert_only_dedicated_equipment()

    async def test_persistence_failure_prevents_cut_dispatch(self):
        self.guard._store.async_save.side_effect = OSError("synthetic disk failure")
        with self.assertRaises(RuntimeError):
            await self.guard.async_commission_relay(confirm=True)
        await self.settle()
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertTrue(self.guard.blocked)

    async def test_repeated_invocation_does_not_cut_twice_or_reset_the_cooldown(self):
        await self.start()
        tripped = self.guard._tripped_at
        calls = list(self.hass.calls)
        with self.assertRaises(RuntimeError):
            await self.guard.async_commission_relay(confirm=True)
        self.assertEqual(self.hass.calls, calls)
        self.assertEqual(self.guard._tripped_at, tripped)

    async def test_real_isolation_survives_meter_expiry_then_recovers_once_after_normal_cooldown(self):
        await self.start()
        await self.observe_for(299)
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        await self.observe_for(2)
        await self.settle()
        self.assertEqual(sum(c[1] == "turn_on" for c in self.hass.calls), 1)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.commissioning_active)
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_only_dedicated_equipment()

    async def test_failed_fresh_native_ack_recuts_and_requires_manual_recovery_without_retry(self):
        await self.start()
        self.hass.ignore_climate = True
        self.hass.restore_to_heat = True
        await self.observe_for(301)
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(sum(c[1] == "turn_on" for c in self.hass.calls), 1)
        await self.observe_for(360)
        await self.settle()
        self.assertEqual(sum(c[1] == "turn_on" for c in self.hass.calls), 1)
        self.assert_only_dedicated_equipment()

    async def test_fresh_high_watts_after_native_off_prevent_a_successful_result(self):
        await self.start()
        self.hass.post_off_watts = 1400
        await self.observe_for(301)
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_only_dedicated_equipment()

    async def test_manual_relay_restoration_interrupts_test_without_automatic_resurrection(self):
        await self.start()
        self.hass.set(B.heater_switch, "on")
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        self.hass.set(WATTS, 1)
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_unavailable_relay_interrupts_the_commission_instead_of_resuming_later(self):
        await self.start()
        self.hass.set(B.heater_switch, "unavailable")
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_controller_activation_during_isolation_cancels_the_restore_permission(self):
        await self.start()
        self.controller_off = False
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.controller_off = True
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_selected_amp_meter_also_rejects_assumed_and_restored_low_values(self):
        self.guard.bindings = B
        self.guard.entity_ids = B.entity_ids
        self.hass.data[B.heater_current] = fixtures.measurements(self.clock)[B.heater_current]
        for flag in ("restored", "assumed_state"):
            with self.subTest(flag=flag):
                self.hass.set(B.heater_current, ".05",
                              {"unit_of_measurement": "A", flag: True})
                with self.assertRaises(RuntimeError):
                    await self.guard.async_commission_relay(confirm=True)
                self.assertEqual(self.hass.calls, [])

    async def test_existing_amp_meter_can_qualify_a_complete_test_without_watts_binding(self):
        self.guard.bindings = B
        self.guard.entity_ids = B.entity_ids
        self.hass.data[B.heater_current] = fixtures.measurements(self.clock)[B.heater_current]
        self.hass.post_off_current = .05
        await self.start()
        await self.observe_for(301)
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assertFalse(self.guard.blocked)
        self.assert_only_dedicated_equipment()

    async def test_changed_conditions_during_intent_save_are_rechecked_before_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_save(data):
            entered.set()
            await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.hass.set(B.input_power, "unavailable")
        release.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(start, .5)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertNotIn("turn_off", [c[1] for c in self.hass.calls])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")

    async def test_healthy_observation_during_initial_save_cannot_dispatch_an_early_shutdown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_save(data):
            entered.set()
            await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.guard._observe()
        await asyncio.sleep(.005)
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.hass.persistence_snapshots, [])
        self.assertTrue(self.guard.commissioning_active)
        release.set()
        await asyncio.wait_for(start, .5)
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assert_only_dedicated_equipment()

    async def test_trusted_off_report_during_post_cut_save_preserves_active_test(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_post_cut_save(data):
            if data["commissioning"]["status"] == "waiting_for_recovery" and not entered.is_set():
                entered.set()
                await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_post_cut_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        self.assertTrue(self.guard.commissioning_active)
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        release.set()
        await asyncio.wait_for(start, .5)
        await self.settle()
        self.assertTrue(self.guard.commissioning_active)
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "waiting_for_recovery")
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_manual_on_during_post_cut_save_interrupts_without_future_restore(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_post_cut_save(data):
            if data["commissioning"]["status"] == "waiting_for_recovery" and not entered.is_set():
                entered.set()
                await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_post_cut_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.hass.set(B.heater_switch, "on")
        self.guard._observe()
        release.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(start, .5)
        await self.settle()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_cancellation_during_intent_save_cannot_late_save_a_resumable_test(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_save(data):
            entered.set()
            await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        start.cancel()
        release.set()
        try:
            await asyncio.wait_for(start, .5)
        except asyncio.CancelledError:
            pass
        await self.settle()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertEqual(self.hass.persistence_snapshots[-1], self.guard._saved_state())

    async def test_cancelled_caller_cannot_start_a_child_queued_before_active_reservation(self):
        queued, release = asyncio.Event(), asyncio.Event()
        original = self.hass.async_create_task

        def queued_create_task(coroutine, *, eager_start=False):
            if coroutine.cr_code.co_name != "_commission_relay":
                return original(coroutine, eager_start=eager_start)

            async def gated_child():
                try:
                    await release.wait()
                    return await coroutine
                finally:
                    coroutine.close()

            queued.set()
            return asyncio.create_task(gated_child())

        with (patch.object(self.hass, "async_create_task", queued_create_task),
              patch.object(asyncio.get_running_loop(), "call_exception_handler") as loop_error):
            caller = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
            await asyncio.wait_for(queued.wait(), .5)
            caller.cancel()
            try:
                await caller
            except asyncio.CancelledError:
                pass
            release.set()
            await self.settle()
            loop_error.assert_not_called()
        self.assertEqual(self.hass.calls, [])
        self.assertFalse(self.guard.commissioning_active)
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")

    async def test_manual_relay_cycle_during_storage_wait_cannot_hide_behind_final_on(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_save(data):
            entered.set()
            await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        self.hass.set(B.heater_switch, "on")
        self.guard._observe()
        release.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(start, .5)
        await self.settle()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_unconfirmed_dedicated_cut_fails_without_a_shared_ac_attempt(self):
        self.hass.ignore_relay_off = True
        with self.assertRaises(RuntimeError):
            await self.guard.async_commission_relay(confirm=True)
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assert_only_dedicated_equipment()
    async def test_unload_after_cut_interrupts_test_and_never_restores_power(self):
        await self.start()
        self.assertTrue(await self.guard.async_close())
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")

    async def test_observation_gap_interrupts_commissioning_and_never_restores_later(self):
        await self.start()
        self.clock.advance(181)
        self.refresh_external()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assert_only_dedicated_equipment()

    async def test_wall_clock_reversal_interrupts_commissioning_without_resetting_permission(self):
        await self.start()
        self.clock.wall -= 2
        self.refresh_external()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assert_only_dedicated_equipment()

    async def test_whole_test_expires_when_fresh_safe_input_never_reaches_rearm_margin(self):
        await self.start()
        self.hass.set(B.input_power, "1300")  # <1476W trip, but >85% rearm budget.
        await self.observe_for(599)
        await self.settle()
        self.assertTrue(self.guard.commissioning_active)
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertEqual(self.guard.diagnostics["wall_power_budget"]["qualified_sources"], [])
        await self.observe_for(2)
        await self.settle()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.guard.diagnostics["commissioning"]["maximum_seconds"], 600)
        self.hass.set(B.input_power, "130")
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assert_only_dedicated_equipment()

    async def test_restore_cannot_pass_if_whole_test_expires_after_fresh_off_proof(self):
        await self.start()
        original = self.guard._off_ack
        proofs = []

        async def late_verified_off(seconds, *, require_meter=False):
            verified = await original(seconds, require_meter=require_meter)
            if verified:
                # Simulate a delayed completion while sources continue to
                # provide current, healthy reports. Expiry, not stale data,
                # must prevent the final recovery clear.
                self.clock.advance(301)
                self.hass.refresh()
                proofs.append((power.evaluate_power(
                    self.hass.data, self.bindings, self.clock.wall).safe_to_rearm,
                    self.guard._heater_off(fresh=True),
                    self.guard._heater_meter_stopped() is True))
            return verified

        with patch.object(self.guard, "_off_ack", late_verified_off):
            await self.observe_for(301)
            await self.settle()
        self.assertEqual(proofs, [(True, True, True)])
        self.assertEqual(sum(c[1] == "turn_on" for c in self.hass.calls), 1)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.commissioning_active)
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_only_dedicated_equipment()

    async def test_cancelled_actual_store_write_is_not_a_committed_test_intent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._store.async_save.side_effect

        async def suspended_save(data):
            entered.set()
            await release.wait()
            await original(data)

        self.guard._store.async_save.side_effect = suspended_save
        start = asyncio.create_task(self.guard.async_commission_relay(confirm=True))
        await asyncio.wait_for(entered.wait(), .5)
        self.guard._save_task.cancel()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(start, .5)
        release.set()
        await self.settle()
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])

    async def test_restart_of_saved_active_test_requires_manual_recovery(self):
        await self.start()
        saved = deepcopy(self.guard._saved_state())
        restarted = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="commission-fixture",
            notify=lambda: None, clock=lambda: self.clock.now,
            wall_clock=lambda: self.clock.wall)
        restarted.commissioning_controller_off = lambda: True
        store = type("RestartStore", (), {})()
        store.async_load = AsyncMock(return_value=saved)
        store.async_save = AsyncMock()
        package = ModuleType("homeassistant")
        helpers = ModuleType("homeassistant.helpers")
        storage = ModuleType("homeassistant.helpers.storage")
        storage.Store = lambda *args: store
        with patch.dict(sys.modules, {"homeassistant": package,
                                      "homeassistant.helpers": helpers,
                                      "homeassistant.helpers.storage": storage}):
            await restarted.async_load()
        self.guard = restarted
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.commissioning_active)
        await self.observe_for(360)
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assert_only_dedicated_equipment()
