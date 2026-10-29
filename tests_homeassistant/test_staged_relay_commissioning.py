"""Exercise supervised watt-meter stages using real HA state-change events."""
import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import patch

from custom_components.house_climate import power_guard as power
from tests_homeassistant import test_relay_commissioning_service as native


class StagedRelayCommissioningTests(unittest.IsolatedAsyncioTestCase):
    invoke = native.NativeGuardCommissioningTests.invoke

    async def asyncSetUp(self):
        await native.NativeGuardCommissioningTests.asyncSetUp(self)
        self.source_wall = datetime.now(timezone.utc).timestamp()
        self.guard._wall_clock = lambda: self.source_wall
        self.native_ready = False
        self.publish_on_watts = True
        self.publish_off_watts = True
        self.fail_native_service = False
        self.ignore_native_ack = False
        self.on_seen, self.native_off_seen = asyncio.Event(), asyncio.Event()
        self.off_source_stamp = None
        self.timeouts = [patch.object(power, "RESTORE_ACK_SECONDS", .08)]
        for patcher in self.timeouts:
            patcher.start()
        self.original_service = self.service_handler

        async def handle(call):
            if call.domain == "switch" and call.service == "turn_on":
                self.calls.append((call.domain, call.service, dict(call.data)))
                self.report(self.bindings.heater_switch, "on")
                self.report(self.bindings.heater, "unavailable", {"hvac_action": "off"})
                if self.publish_on_watts:
                    self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
                self.on_seen.set()
            elif call.domain == "switch":
                self.calls.append((call.domain, call.service, dict(call.data)))
                self.report(self.bindings.heater_switch, "off")
                self.report(self.bindings.heater, "unavailable", {"hvac_action": "off"})
                self.report(self.bindings.heater_power, "0", {"unit_of_measurement": "W"})
            elif call.domain == "climate":
                self.calls.append((call.domain, call.service, dict(call.data)))
                self.native_off_seen.set()
                self.off_source_stamp = self.source_wall
                if self.fail_native_service:
                    raise RuntimeError("Synthetic native transport failure")
                if self.native_ready and not self.ignore_native_ack:
                    self.report(self.bindings.heater, "off", {"hvac_action": "off"})
                    if self.publish_off_watts:
                        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
            else:
                await self.original_service(call)

        self.staged_service = handle
        for domain, services in (("switch", ("turn_on", "turn_off")),
                                 ("climate", ("set_hvac_mode",))):
            for service in services:
                self.hass.services.async_register(domain, service, handle)
        await self.external_reports()

    async def asyncTearDown(self):
        await native.NativeGuardCommissioningTests.asyncTearDown(self)
        for patcher in self.timeouts:
            patcher.stop()

    def report(self, entity, value, attrs=None, *, timestamp=None):
        if attrs is None:
            previous = self.hass.states.get(entity)
            attrs = previous.attributes if previous is not None else {}
        if timestamp is None:
            self.source_wall = datetime.fromtimestamp(self.source_wall + .001, timezone.utc).timestamp()
            timestamp = self.source_wall
        self.hass.states.async_set(entity, value, attrs, force_update=True, timestamp=timestamp)

    async def external_reports(self):
        for entity in (self.bindings.input_power, self.bindings.output_power, self.bindings.battery,
                       self.bindings.wall_phase_a_voltage, self.bindings.wall_phase_b_voltage):
            old = self.hass.states.get(entity)
            self.report(entity, old.state, old.attributes)
        await asyncio.sleep(0)

    async def tick(self, seconds, *, watts="1", native=None):
        self.monotonic[0] += seconds
        self.source_wall = datetime.fromtimestamp(self.source_wall + seconds, timezone.utc).timestamp()
        await self.external_reports()
        if watts is not None:
            self.report(self.bindings.heater_power, watts, {"unit_of_measurement": "W"})
        if native is not None:
            self.report(self.bindings.heater, native, {"hvac_action": "off"})
        self.guard._observe()
        await asyncio.sleep(.025)

    async def wait_until(self, predicate, timeout=1):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.01)

    async def begin_restore(self):
        await self.invoke({"confirm": True})
        self.guard._observe()
        # Move only the cooldown policy clock here. Actual source observations
        # remain continuous; later stage/source clocks advance together.
        self.monotonic[0] += 301
        self.guard._observe()
        await asyncio.wait_for(self.on_seen.wait(), 1)
        await asyncio.sleep(.025)

    async def assert_failed_off(self):
        try:
            await self.wait_until(lambda: not self.guard.commissioning_active)
        except TimeoutError:
            self.fail(f"Commissioning did not fail: {self.guard.diagnostics}; calls={self.calls}")
        await self.wait_until(lambda: not self.guard.commissioning_busy)
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(sum(service == "turn_on" for _, service, _ in self.calls), 1)

    async def test_unavailable_native_can_reconnect_later_while_fresh_low_watts_are_supervised(self):
        await self.begin_restore()
        await asyncio.sleep(.12)
        self.assertTrue(self.guard.commissioning_active)
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "on")
        self.assertFalse(self.native_off_seen.is_set())
        for _ in range(9):
            await self.tick(10)
        self.assertTrue(self.guard.commissioning_active)
        self.assertFalse(self.native_off_seen.is_set())
        self.native_ready = True
        await self.tick(1, native="off")
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"]["status"] == "passed")
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "on")
        self.assertTrue(self.native_off_seen.is_set())
        self.assertTrue(all(data.get("hvac_mode", "off") == "off" for _, _, data in self.calls))

    async def test_native_readiness_cannot_replace_delayed_post_on_watt_acquisition(self):
        self.publish_on_watts = False
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, watts=None, native="off")
        self.assertTrue(self.guard.commissioning_active)
        self.assertFalse(self.native_off_seen.is_set())
        await self.tick(9)
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"]["status"] == "passed")

    async def test_missing_post_on_watts_expires_acquisition_without_waiting_for_native(self):
        self.publish_on_watts = False
        await self.begin_restore()
        await self.tick(31, watts=None)
        await self.assert_failed_off()
        self.assertFalse(self.native_off_seen.is_set())

    async def test_duplicate_watt_timestamp_does_not_renew_report_gap_watchdog(self):
        await self.begin_restore()
        old = self.hass.states.get(self.bindings.heater_power)
        stamp = old.last_reported.timestamp()
        await self.tick(20, watts=None)
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"}, timestamp=stamp)
        await asyncio.sleep(.025)
        await self.tick(11, watts=None)
        await self.assert_failed_off()

    async def test_high_then_low_same_loop_is_a_sticky_failure(self):
        await self.begin_restore()
        self.report(self.bindings.heater_power, "1400", {"unit_of_measurement": "W"})
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.assert_failed_off()

    async def test_unavailable_then_low_same_loop_is_a_sticky_failure(self):
        await self.begin_restore()
        self.report(self.bindings.heater_power, "unavailable", {"unit_of_measurement": "W"})
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.assert_failed_off()

    async def test_restored_low_then_trusted_low_does_not_hide_the_interruption(self):
        await self.begin_restore()
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W", "restored": True})
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.assert_failed_off()

    async def test_out_of_order_report_then_new_low_is_a_sticky_failure(self):
        await self.begin_restore()
        initial = self.hass.states.get(self.bindings.heater_power).last_reported.timestamp()
        await self.tick(10)
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"}, timestamp=initial)
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.assert_failed_off()

    async def test_post_off_low_watts_may_arrive_after_native_ack(self):
        self.publish_off_watts = False
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, watts=None, native="off")
        await asyncio.wait_for(self.native_off_seen.wait(), 1)
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"].get("phase") == "meter_confirmation")
        self.assertTrue(self.guard.commissioning_active)
        await self.tick(20, watts=None)
        self.assertTrue(self.guard.commissioning_active)
        await self.tick(1)
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"]["status"] == "passed")
        meter = self.hass.states.get(self.bindings.heater_power)
        self.assertGreater(meter.last_reported.timestamp(), self.off_source_stamp)

    async def test_post_off_meter_deadline_does_not_accept_pre_command_low_value(self):
        self.publish_off_watts = False
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, watts=None, native="off")
        await asyncio.wait_for(self.native_off_seen.wait(), 1)
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"].get("phase") == "meter_confirmation")
        await self.tick(31, watts=None)
        await self.assert_failed_off()

    async def test_native_service_failure_recuts_without_starting_heat_or_shared_ac(self):
        self.fail_native_service = True
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, native="off")
        await self.assert_failed_off()
        self.assertTrue(all(data["entity_id"] in
                            {self.bindings.heater, self.bindings.heater_switch} for _, _, data in self.calls))

    async def test_native_off_still_requires_a_new_acknowledgement(self):
        self.ignore_native_ack = True
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, native="heat")
        await self.assert_failed_off()

    async def test_comfort_cannot_start_while_waiting_for_native_reconnection(self):
        await self.begin_restore()
        controls = native.NativeControls(self.hass, self.config, True, "staged-comfort", lambda: None)
        controls.active = True
        controls.power_guard = self.guard
        with self.assertRaisesRegex(native.HAError, "commissioning must finish"):
            await controls.async_set_enabled(True)
        self.assertFalse(controls.enabled)
        self.assertEqual(controls.owned, ())

    async def test_unload_during_native_wait_recuts_and_requires_manual_recovery(self):
        await self.begin_restore()
        await self.guard.async_close()
        await self.assert_failed_off()

    async def test_cancelled_restoration_during_native_wait_recuts(self):
        await self.begin_restore()
        self.guard._action_task.cancel()
        await self.assert_failed_off()

    async def test_restart_during_native_wait_cannot_resume_the_test(self):
        await self.begin_restore()
        saved = await self.guard._store.async_load()
        self.assertTrue(saved["commissioning"]["active"])
        previous = self.guard
        previous._closed = True
        for remove in previous._unsubscribers:
            remove()
        previous._unsubscribers.clear()
        # The lost process cannot dispatch more actions; the replacement sees
        # the actual persisted in-progress test, not manufactured sensor data.
        previous._store = None
        previous._action_task.cancel()
        await asyncio.gather(previous._action_task, return_exceptions=True)
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="native-commissioning",
            notify=lambda: None, clock=lambda: self.monotonic[0],
            wall_clock=lambda: self.source_wall,
        )
        self.guard.commissioning_controller_off = lambda: True
        self.runtime.power_guard = self.guard
        await self.guard.async_load()
        self.guard.async_start()
        await asyncio.sleep(.025)
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        before = sum(service == "turn_on" for _, service, _ in self.calls)
        await self.tick(301)
        self.assertEqual(sum(service == "turn_on" for _, service, _ in self.calls), before)

    async def test_low_report_between_on_request_and_on_feedback_does_not_qualify(self):
        original = self.staged_service

        async def meter_first(call):
            if call.domain == "switch" and call.service == "turn_on":
                self.calls.append((call.domain, call.service, dict(call.data)))
                self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
                self.report(self.bindings.heater_switch, "on")
                self.report(self.bindings.heater, "unavailable", {"hvac_action": "off"})
                self.on_seen.set()
            else:
                await original(call)

        self.hass.services.async_register("switch", "turn_on", meter_first)
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, watts=None, native="off")
        self.assertFalse(self.native_off_seen.is_set())
        await self.tick(1)
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"]["status"] == "passed")

    async def test_native_reconnect_deadline_is_absolute_despite_fresh_low_reports(self):
        # Isolate the native stage's own deadline from the production overall
        # bound, which can expire first after the ordinary five-minute cooldown.
        with patch.object(power, "COMMISSIONING_MAX_SECONDS", 900):
            await self.begin_restore()
            for _ in range(30):
                if not self.guard.commissioning_active:
                    break
                await self.tick(10, native="unavailable")
            await self.assert_failed_off()
        self.assertIn("Native reconnect", self.guard.diagnostics["commissioning"]["reason"])
        self.assertFalse(self.native_off_seen.is_set())

    async def test_overall_deadline_clips_the_post_off_meter_window(self):
        self.publish_off_watts = False
        await self.begin_restore()
        for _ in range(28):
            await self.tick(10)
        self.native_ready = True
        await self.tick(10, watts=None, native="off")
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"].get("phase") == "meter_confirmation")
        await self.tick(10, watts=None)
        await self.assert_failed_off()

    async def lose_native_off_after_ack(self, mode, action):
        self.publish_off_watts = False
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, watts=None, native="off")
        await self.wait_until(lambda: self.guard.diagnostics["commissioning"].get("phase") == "meter_confirmation")
        self.report(self.bindings.heater, mode, {"hvac_action": action})
        self.report(self.bindings.heater, "off", {"hvac_action": "off"})
        self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
        await self.assert_failed_off()

    async def test_heat_idle_then_off_after_ack_does_not_hide_loss_of_stop_confirmation(self):
        await self.lose_native_off_after_ack("heat", "idle")

    async def test_unavailable_then_off_after_ack_does_not_hide_loss_of_stop_confirmation(self):
        await self.lose_native_off_after_ack("unavailable", "off")

    async def test_off_and_watts_before_actual_service_dispatch_cannot_acknowledge_it(self):
        queued, release = asyncio.Event(), asyncio.Event()
        create = self.hass.async_create_task
        self.ignore_native_ack = True

        def hold_dispatch(coro, *args, **kwargs):
            if getattr(getattr(coro, "cr_code", None), "co_name", None) == "dispatch":
                async def held():
                    try:
                        queued.set()
                        await release.wait()
                        return await coro
                    finally:
                        coro.close()
                return create(held(), *args, **kwargs)
            return create(coro, *args, **kwargs)

        with patch.object(self.hass, "async_create_task", side_effect=hold_dispatch):
            try:
                await self.begin_restore()
                self.native_ready = True
                await self.tick(1, native="off")
                await asyncio.wait_for(queued.wait(), 1)
                self.report(self.bindings.heater, "off", {"hvac_action": "off"})
                self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
                await asyncio.sleep(.01)
                self.assertFalse(self.native_off_seen.is_set())
                self.assertTrue(self.guard.commissioning_active)
                self.assertIsNone(self.guard._supervised_restore.get("off_clock"))
            finally:
                release.set()
            await self.assert_failed_off()
        self.assertTrue(self.native_off_seen.is_set())

    async def test_late_failed_native_service_overrides_fresh_off_and_watts_feedback(self):
        release = asyncio.Event()

        async def delayed_failure(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            self.native_off_seen.set()
            self.report(self.bindings.heater, "off", {"hvac_action": "off"})
            self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
            await release.wait()
            raise RuntimeError("Synthetic late native transport failure")

        self.hass.services.async_register("climate", "set_hvac_mode", delayed_failure)
        try:
            await self.begin_restore()
            self.native_ready = True
            await self.tick(1, native="off")
            await self.wait_until(lambda: self.guard.diagnostics["commissioning"].get("phase") == "meter_confirmation")
            self.assertTrue(self.guard.commissioning_active)
            self.assertTrue(self.guard._service_tasks)
        finally:
            release.set()
        await self.assert_failed_off()

    async def test_fresh_native_off_after_ack_deadline_cannot_pass_overdue_fallback(self):
        late_reported = asyncio.Event()

        async def blocked_native_off(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            self.native_off_seen.set()
            # Model an integration blocking HA's loop. The source reports are
            # genuinely produced after dispatch, but well beyond the .08-second
            # native acknowledgement deadline used by this test fixture.
            time.sleep(.20)
            self.report(self.bindings.heater, "off", {"hvac_action": "off"})
            self.report(self.bindings.heater_power, "1", {"unit_of_measurement": "W"})
            late_reported.set()

        self.hass.services.async_register("climate", "set_hvac_mode", blocked_native_off)
        await self.begin_restore()
        self.native_ready = True
        await self.tick(1, native="off")
        await self.assert_failed_off()
        self.assertTrue(late_reported.is_set())
        self.assertNotEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
