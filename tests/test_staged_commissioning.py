"""Supervised recovery with a slow native integration and live watt reports."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_power_guard as fixtures
from tests import test_power_guard_commissioning as commissioning
from tests import test_heater_power_guard as watts


power = fixtures.power
B = fixtures.B
WATTS = watts.WATTS


class SlowNativeHass(commissioning.CommissioningHass):
    def __init__(self, clock):
        super().__init__(clock)
        self.boot_started = None
        self.native_delay = 218
        self.first_watt_delay = 0
        self.meter_behavior = "fresh"
        self.native_reported = False
        self.latest_meter_report = None
        self.off_commands = []
        self.post_off_meter_delay = 0
        self.last_off_command = None
        self.native_after_ack = None
        self.changed_after_ack = False
        self.frame_hook = None
        self.hold_off_completion = False
        self.release_off_completion = asyncio.Event()

    async def service(self, domain, service, body, blocking):
        if domain == "switch" and service == "turn_on":
            self.calls.append((domain, service, dict(body)))
            self.set(B.heater_switch, "on")
            self.set(B.heater, "unavailable", {})
            self.boot_started = self.clock.now
            self.native_reported = False
            if not self.first_watt_delay:
                self.publish_meter(1)
            return
        if domain == "climate" and self.boot_started is not None:
            self.off_commands.append(self.clock.now)
            self.last_off_command = self.clock.now
            if not self.native_reported:
                self.calls.append((domain, service, dict(body)))
                return
            if self.post_off_meter_delay:
                before = self.post_off_watts
                self.post_off_watts = None
                try:
                    result = await super().service(domain, service, body, blocking)
                finally:
                    self.post_off_watts = before
            else:
                result = await super().service(domain, service, body, blocking)
            if self.hold_off_completion:
                await self.release_off_completion.wait()
            return result
        return await super().service(domain, service, body, blocking)

    def publish_meter(self, value):
        self.set(WATTS, value, {"unit_of_measurement": "W"})
        self.latest_meter_report = deepcopy(self.data[WATTS])

    def frame(self):
        self.clock.advance(5)
        for entity in (B.input_power, B.output_power, B.battery,
                       B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            old = self.data[entity]
            self.set(entity, old["state"], old["attributes"])
        elapsed = self.clock.now - self.boot_started
        if elapsed >= self.first_watt_delay:
            if self.meter_behavior == "high" and 10 <= elapsed < 15:
                self.publish_meter(1400)
            elif self.meter_behavior == "unavailable" and 10 <= elapsed < 15:
                self.publish_meter("unavailable")
            elif self.meter_behavior == "duplicate" and elapsed >= 10:
                # Repeated export of the same HA packet is no new sample.
                pass
            elif self.meter_behavior == "out_of_order" and 10 <= elapsed < 15:
                previous = deepcopy(self.latest_meter_report)
                self.publish_meter(1)
                previous["last_reported"] = datetime.fromtimestamp(
                    self.clock.wall - 6, timezone.utc).isoformat()
                self.data[WATTS] = previous
            elif self.post_off_meter_delay and self.native_reported and self.last_off_command is None:
                # No packet may slip between queued Off request and physical
                # service dispatch in this deliberate post-request silence case.
                pass
            elif (self.last_off_command is None
                  or self.clock.now - self.last_off_command >= self.post_off_meter_delay):
                self.publish_meter(1)
        if elapsed >= self.native_delay and not self.native_reported:
            self.native_reported = True
            self.set(B.heater, "off", {"hvac_action": "off"})
        if (self.native_after_ack is not None and self.last_off_command is not None
                and self.native_reported and not self.changed_after_ack):
            self.changed_after_ack = True
            state, attrs = self.native_after_ack
            self.set(B.heater, state, attrs)
        if self.frame_hook is not None:
            self.frame_hook(self)


class StagedCommissioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = fixtures.Clock()
        self.hass = SlowNativeHass(self.clock)
        self.bindings = watts.watt_bindings()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="staged-fixture",
            notify=lambda: None, clock=lambda: self.clock.now,
            wall_clock=lambda: self.clock.wall)
        self.guard.commissioning_controller_off = lambda: True

        async def save(data):
            self.hass.persistence_snapshots.append(deepcopy(data))

        self.guard._store = type("MemoryStore", (), {})()
        self.guard._store.async_save = AsyncMock(side_effect=save)
        self.real_sleep = asyncio.sleep
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for name, seconds in (("COMMISSIONING_METER_ACQUIRE_SECONDS", 30),
                              ("COMMISSIONING_REPORT_GAP_SECONDS", 30),
                              ("COMMISSIONING_RECONNECT_SECONDS", 300),
                              ("COMMISSIONING_METER_CONFIRM_SECONDS", 30)):
            self.patchers.append(patch.object(power, name, seconds, create=True))
        for patcher in self.patchers:
            patcher.start()
        self.guard._observe()
        self.assertFalse(self.guard.blocked)

    async def asyncTearDown(self):
        self.hass.release_off_completion.set()
        await self.guard.async_close()
        await self.real_sleep(.01)
        for patcher in self.patchers:
            patcher.stop()

    def fresh_external(self):
        for entity in (B.input_power, B.output_power, B.battery,
                       B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            old = self.hass.data[entity]
            self.hass.set(entity, old["state"], old["attributes"])

    async def prepare_restore(self):
        await self.guard.async_commission_relay(confirm=True)
        await self.age_cut_to_restore()

    async def age_cut_to_restore(self):
        for _ in range(10):
            self.clock.advance(30)
            self.fresh_external()
            if self.clock.now - self.guard._tripped_at < 300:
                self.guard._observe()
            await self.real_sleep(0)
        self.clock.advance(1)
        self.fresh_external()

    async def run_restore(self):
        async def driven_sleep(delay):
            if (delay > 0 and asyncio.current_task() is self.guard._action_task
                    and self.hass.boot_started is not None):
                self.hass.frame()
                self.guard._observe()
                # Leave enough real event-loop time that the old combined
                # .04-second fixture deadline cannot swallow218fake seconds.
                await self.real_sleep(.002)
            else:
                await self.real_sleep(0)

        with patch.object(power.asyncio, "sleep", driven_sleep):
            self.guard._observe()
            for _ in range(2000):
                task = self.guard._action_task
                if task is not None and task.done():
                    task.result()
                    break
                await self.real_sleep(.001)
            else:
                self.fail(f"Recovery did not finish: {self.guard.diagnostics}")
        await self.real_sleep(.01)

    def assert_no_heat_or_shared_ac(self):
        self.assertTrue(all(call[2]["entity_id"] in {B.heater, B.heater_switch}
                            for call in self.hass.calls))
        self.assertTrue(all(call[2].get("hvac_mode", "off") == "off"
                            for call in self.hass.calls))

    async def test_native_reconnect_after_218_seconds_can_pass_with_continuous_low_watts(self):
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertTrue(self.hass.off_commands)
        self.assert_no_heat_or_shared_ac()

    async def test_first_watt_packet_can_arrive_after_20_seconds_without_fabricated_zero(self):
        self.hass.first_watt_delay = 20
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_no_heat_or_shared_ac()

    async def test_no_first_watt_packet_by_30_seconds_recuts_and_requires_recovery(self):
        self.hass.first_watt_delay = 35
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assert_no_heat_or_shared_ac()

    async def test_duplicate_watt_exports_do_not_sustain_energized_wait(self):
        self.hass.meter_behavior = "duplicate"
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertLess(self.clock.now - self.hass.boot_started, 60)
        self.assert_no_heat_or_shared_ac()

    async def test_out_of_order_watt_packet_is_not_repaired_by_later_fresh_low_packet(self):
        self.hass.meter_behavior = "out_of_order"
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assert_no_heat_or_shared_ac()

    async def test_intermediate_high_watts_during_native_reconnect_recuts_immediately(self):
        self.hass.meter_behavior = "high"
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertLess(self.clock.now - self.hass.boot_started, 20)
        self.assert_no_heat_or_shared_ac()

    async def test_intermediate_unavailable_meter_cannot_be_hidden_by_later_low_watts(self):
        self.hass.meter_behavior = "unavailable"
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assert_no_heat_or_shared_ac()

    async def test_reconnect_phase_has_a_300_second_limit_even_if_low_watts_keep_reporting(self):
        self.hass.native_delay = 310
        # Isolate the phase deadline from the independently tested600s limit.
        with patch.object(power, "COMMISSIONING_MAX_SECONDS", 900):
            await self.prepare_restore()
            await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertGreaterEqual(self.clock.now - self.hass.boot_started, 300)
        self.assertLess(self.clock.now - self.hass.boot_started, 310)
        self.assert_no_heat_or_shared_ac()

    async def test_native_off_still_needs_a_new_low_watt_packet_within_30_seconds(self):
        self.hass.native_delay = 20
        self.hass.post_off_meter_delay = 35
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assert_no_heat_or_shared_ac()

    async def native_change_after_ack(self, state, attributes):
        self.hass.native_delay = 20
        self.hass.post_off_meter_delay = 20
        self.hass.native_after_ack = (state, attributes)
        await self.prepare_restore()
        await self.run_restore()
        self.assertTrue(self.hass.changed_after_ack)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_no_heat_or_shared_ac()

    async def test_heat_idle_after_native_ack_cannot_pass_post_off_meter_confirmation(self):
        await self.native_change_after_ack("heat", {"hvac_action": "idle"})

    async def test_native_unavailable_after_ack_cannot_pass_post_off_meter_confirmation(self):
        await self.native_change_after_ack("unavailable", {})

    async def test_restored_native_off_after_ack_cannot_pass_post_off_meter_confirmation(self):
        await self.native_change_after_ack("off", {"hvac_action": "off", "restored": True})

    async def test_assumed_native_off_after_ack_cannot_pass_post_off_meter_confirmation(self):
        await self.native_change_after_ack("off", {"hvac_action": "off", "assumed_state": True})

    async def test_global_600_seconds_caps_combined_otherwise_valid_reconnect_and_meter_phases(self):
        self.hass.first_watt_delay = 20
        self.hass.native_delay = 285
        self.hass.post_off_meter_delay = 20
        await self.prepare_restore()
        started = self.guard._commissioning_started_clock
        await self.run_restore()
        self.assertGreaterEqual(self.clock.now - started, 600)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_no_heat_or_shared_ac()

    async def cancel_during_reconnect(self):
        captured = []

        def cancel_after_fresh_low_reports(hass):
            if not captured and hass.clock.now - hass.boot_started >= 10:
                captured.append(deepcopy(self.guard._saved_state()))
                self.guard._action_task.cancel()

        self.hass.frame_hook = cancel_after_fresh_low_reports
        await self.prepare_restore()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_restore()
        await self.real_sleep(.02)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assert_no_heat_or_shared_ac()
        return captured[0]

    async def test_cancelling_energized_native_reconnect_wait_recuts_without_heat_or_shared_ac(self):
        await self.cancel_during_reconnect()
        self.assertFalse(self.guard.commissioning_active)
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")

    async def test_restart_of_energized_saved_intent_never_resumes_the_staged_wait(self):
        saved = await self.cancel_during_reconnect()
        self.assertTrue(saved["restoring"])
        self.assertTrue(saved["commissioning"]["active"])
        before = sum(call[1] == "turn_on" for call in self.hass.calls)
        restarted = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="staged-fixture",
            notify=lambda: None, clock=lambda: self.clock.now,
            wall_clock=lambda: self.clock.wall)
        restarted.commissioning_controller_off = lambda: True
        store = type("RestartStore", (), {})()
        store.async_load = AsyncMock(return_value=saved)
        store.async_save = AsyncMock()
        storage = ModuleType("homeassistant.helpers.storage")
        storage.Store = lambda *args: store
        with patch.dict(sys.modules, {"homeassistant": ModuleType("homeassistant"),
                                      "homeassistant.helpers": ModuleType("homeassistant.helpers"),
                                      "homeassistant.helpers.storage": storage}):
            await restarted.async_load()
        self.guard = restarted
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.commissioning_active)
        for _ in range(12):
            self.clock.advance(30)
            self.fresh_external()
            self.guard._observe()
            await self.real_sleep(0)
        self.assertEqual(sum(call[1] == "turn_on" for call in self.hass.calls), before)
        self.assert_no_heat_or_shared_ac()

    async def test_unattended_watt_recovery_keeps_the_short_legacy_acknowledgement_path(self):
        self.hass.ignore_climate = True
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.output_power, "2000")
        self.guard._observe()
        await self.real_sleep(.09)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertFalse(self.guard.commissioning_active)
        self.hass.ignore_climate = False
        self.hass.set(B.output_power, "150")
        await self.age_cut_to_restore()
        await self.run_restore()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "idle")
        self.assertLess(self.clock.now - self.hass.boot_started, self.hass.native_delay)
        self.assert_no_heat_or_shared_ac()

    async def late_good_packet(self, phase, lateness):
        """Poll just before a deadline, then deliver good data after it."""
        if phase == "startup":
            self.hass.first_watt_delay = 1000
        elif phase == "post_off":
            self.hass.native_delay = 0
            self.hass.hold_off_completion = True
        original_frame = self.hass.frame
        deliveries = []
        base = None

        def deadline_frame():
            nonlocal base
            watch = self.guard._supervised_restore
            if phase == "post_off" and not self.hass.off_commands:
                original_frame()
                return
            if len(deliveries) >= 2:
                original_frame()
                return
            if base is None:
                base = watch[{"startup": "requested_clock", "gap": "seen_clock",
                              "post_off": "off_clock"}[phase]]
            target = base + (29 if not deliveries else 30 + lateness)
            self.clock.advance(target - self.clock.now)
            for entity in (B.input_power, B.output_power, B.battery,
                           B.wall_phase_a_voltage, B.wall_phase_b_voltage):
                old = self.hass.data[entity]
                self.hass.data[entity] = watts.report(
                    self.clock, old["state"], old["attributes"]["unit_of_measurement"])
            # Startup/gap deliberately retain the old packet until the late
            # delivery. Post-Off already has continuous healthy watts but the
            # actual service is held until its confirmation deadline expires.
            if deliveries or phase == "post_off":
                self.hass.data[WATTS] = watts.report(self.clock, 1)
                self.hass.latest_meter_report = deepcopy(self.hass.data[WATTS])
                self.hass.first_watt_delay = 0
            if deliveries and phase == "post_off":
                self.hass.release_off_completion.set()
            deliveries.append((self.clock.now - base, self.clock.now - watch["checked_clock"]))

        self.hass.frame = deadline_frame
        await self.prepare_restore()
        await self.run_restore()
        self.assertEqual(len(deliveries), 2)
        self.assertAlmostEqual(deliveries[-1][0], 30 + lateness)
        self.assertLess(deliveries[-1][1], 2)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assert_no_heat_or_shared_ac()

    async def test_first_good_watt_packet_at_30_second_boundary_is_too_late(self):
        await self.late_good_packet("startup", 0)

    async def test_first_good_watt_packet_at_30_point_1_cannot_heal_startup_expiry(self):
        await self.late_good_packet("startup", .1)

    async def test_new_good_watt_packet_at_30_second_report_gap_cannot_heal_it(self):
        await self.late_good_packet("gap", 0)

    async def test_new_good_watt_packet_after_30_point_1_report_gap_cannot_heal_it(self):
        await self.late_good_packet("gap", .1)

    async def test_post_off_completion_at_30_second_boundary_cannot_pass(self):
        await self.late_good_packet("post_off", 0)

    async def test_post_off_completion_after_30_point_1_cannot_pass(self):
        await self.late_good_packet("post_off", .1)

