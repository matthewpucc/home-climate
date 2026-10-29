"""Validate deliberate relay commissioning through HA's service registry."""
import asyncio
from dataclasses import replace
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

import custom_components.house_climate as integration
from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.house_policy import house_configuration
from custom_components.house_climate.runtime import ControllerRuntime
from custom_components.house_climate import power_guard as power


class RelayCommissioningServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.config = house_configuration(bindings=example_bindings())
        self.guard = SimpleNamespace(async_commission_relay=AsyncMock())
        self.runtime = SimpleNamespace(
            config=self.config, live=True, stop_event=threading.Event(),
            power_guard=self.guard, async_setup_controls=AsyncMock(),
            async_stop=AsyncMock(return_value=True), async_start=AsyncMock(),
        )
        self.entry = SimpleNamespace(
            entry_id="commissioning-controller", data={"live": True}, options={},
            title="Home Climate", async_on_unload=Mock(), add_update_listener=Mock(),
        )
        self.old_entries = self.hass.config_entries
        self.hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        with (patch.object(integration, "entry_configuration", return_value=self.config),
              patch.object(integration, "validate_actuators"),
              patch.object(integration, "ControllerRuntime", return_value=self.runtime),
              patch.object(integration, "async_at_started", return_value=lambda: None)):
            self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))

    async def asyncTearDown(self):
        self.hass.config_entries = self.old_entries
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def invoke(self, data=None):
        return await self.hass.services.async_call(
            DOMAIN, "commission_heater_relay", {} if data is None else data, blocking=True,
        )

    async def test_registers_without_starting_a_test_and_requires_explicit_true(self):
        self.assertTrue(self.hass.services.has_service(DOMAIN, "commission_heater_relay"))
        self.guard.async_commission_relay.assert_not_awaited()
        for data in ({}, {"confirm": False}, {"confirm": 1}, {"confirm": "true"},
                     {"confirm": None}, {"confirm": True, "bypass": True}):
            with self.subTest(data=data), self.assertRaises(vol.Invalid):
                await self.invoke(data)
        self.guard.async_commission_relay.assert_not_awaited()
        await self.invoke({"confirm": True})
        self.guard.async_commission_relay.assert_awaited_once_with(confirm=True)

    async def test_preview_stopping_missing_guard_and_stale_runtime_reject_before_dispatch(self):
        self.runtime.live = False
        with self.assertRaisesRegex(HomeAssistantError, "Enable live"):
            await self.invoke({"confirm": True})
        self.runtime.live = True
        self.runtime.stop_event.set()
        with self.assertRaisesRegex(HomeAssistantError, "unavailable"):
            await self.invoke({"confirm": True})
        self.runtime.stop_event.clear()
        self.runtime.power_guard = None
        with self.assertRaisesRegex(HomeAssistantError, "unavailable"):
            await self.invoke({"confirm": True})
        self.runtime.power_guard = self.guard
        self.hass.data[DOMAIN] = object()
        with self.assertRaisesRegex(HomeAssistantError, "unavailable"):
            await self.invoke({"confirm": True})
        self.guard.async_commission_relay.assert_not_awaited()

    async def test_guard_failure_and_repeat_rejection_reach_user_without_retry(self):
        self.guard.async_commission_relay.side_effect = [None, RuntimeError("Commissioning is already active")]
        await self.invoke({"confirm": True})
        with self.assertRaisesRegex(HomeAssistantError, "already active"):
            await self.invoke({"confirm": True})
        self.assertEqual(self.guard.async_commission_relay.await_count, 2)

    async def test_caller_cancellation_does_not_retry_the_action(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def wait_for_cut(**kwargs):
            started.set()
            await finish.wait()

        self.guard.async_commission_relay.side_effect = wait_for_cut
        call = asyncio.create_task(self.invoke({"confirm": True}))
        await asyncio.wait_for(started.wait(), 1)
        call.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await call
        finally:
            finish.set()
        await self.hass.async_block_till_done()
        self.assertEqual(self.guard.async_commission_relay.await_count, 1)

    async def test_successful_unload_removes_action_but_deferred_unload_preserves_it(self):
        self.runtime.async_stop.return_value = False
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.assertTrue(self.hass.services.has_service(DOMAIN, "commission_heater_relay"))
        self.runtime.async_stop.return_value = True
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(self.hass.services.has_service(DOMAIN, "commission_heater_relay"))
        self.assertNotIn(DOMAIN, self.hass.data)


class RelayCommissioningOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.config = house_configuration(bindings=example_bindings())
        self.runtime = ControllerRuntime(self.hass, self.config, True, house_policy=True,
                                         power_bindings=example_bindings().power)
        self.controls = NativeControls(self.hass, self.config, True, "relay-test", lambda: None)
        self.controls.active = True
        self.runtime.controls = self.runtime.api.controls = self.controls

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_only_an_idle_disabled_native_controller_qualifies(self):
        self.assertTrue(self.runtime._commissioning_controller_off())
        for name, value in (
            ("enabled", True), ("owned", ("climate.synthetic_head",)),
            ("pending_fans", ("fan.synthetic_transfer",)), ("active", False),
            ("live", False), ("recovering", True), ("_closing", True),
            ("_claiming", True), ("inflight", 1), ("service_inflight", 1),
            ("auxiliary_inflight", 1),
        ):
            old = getattr(self.controls, name)
            with self.subTest(name=name):
                setattr(self.controls, name, value)
                self.assertFalse(self.runtime._commissioning_controller_off())
            setattr(self.controls, name, old)
        self.runtime.stop_event.set()
        self.assertFalse(self.runtime._commissioning_controller_off())
        self.runtime.stop_event.clear()
        self.runtime._configuration_reload_pending = True
        self.assertFalse(self.runtime._commissioning_controller_off())
        self.runtime._configuration_reload_pending = False
        self.runtime.live = False
        self.assertFalse(self.runtime._commissioning_controller_off())
        self.runtime.live = True
        self.runtime.controls = None
        self.assertFalse(self.runtime._commissioning_controller_off())

    async def test_pending_and_uncertain_device_jobs_block_maintenance(self):
        with patch.object(self.runtime.api, "service_jobs", return_value=(object(),)):
            self.assertFalse(self.runtime._commissioning_controller_off())
        with patch.object(self.runtime.api, "uncertain_service_jobs", return_value=["climate.synthetic_head"]):
            self.assertFalse(self.runtime._commissioning_controller_off())
        self.assertTrue(self.runtime._commissioning_controller_off())

    async def test_active_test_prevents_controller_enable_without_claiming_devices(self):
        self.controls.power_guard = SimpleNamespace(commissioning_active=True)
        with self.assertRaisesRegex(HAError, "commissioning must finish"):
            await self.controls.async_set_enabled(True)
        self.assertFalse(self.controls.enabled)
        self.assertEqual(self.controls.owned, ())
        self.assertEqual(self.controls.ownership_generation, 0)
        await self.controls.async_set_enabled(False)
        self.assertFalse(self.controls.enabled)

    async def test_failed_test_with_actual_cleanup_pending_still_blocks_enable(self):
        self.controls.power_guard = SimpleNamespace(commissioning_active=False, commissioning_busy=True)
        with self.assertRaisesRegex(HAError, "commissioning must finish"):
            await self.controls.async_set_enabled(True)
        self.assertFalse(self.controls.enabled)
        self.assertEqual(self.controls.ownership_generation, 0)


class NativeGuardCommissioningTests(unittest.IsolatedAsyncioTestCase):
    """Use actual HA events/services/storage for the complete dedicated path."""

    invoke = RelayCommissioningServiceTests.invoke

    async def asyncSetUp(self):
        await RelayCommissioningServiceTests.asyncSetUp(self)
        self.bindings = replace(example_bindings().power, heater_current=None,
                                heater_power="sensor.auxiliary_dedicated_power")
        self.monotonic = [1000.0]
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="native-commissioning",
            notify=lambda: None, clock=lambda: self.monotonic[0],
        )
        self.guard.commissioning_controller_off = lambda: True
        self.runtime.power_guard = self.guard
        self.calls = []
        self.ignore_off = False
        self.restore_heating = False

        async def handle(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            if call.domain == "switch":
                value = "on" if call.service == "turn_on" else "off"
                self.hass.states.async_set(self.bindings.heater_switch, value, force_update=True)
                self.hass.states.async_set(self.bindings.heater,
                                           "unavailable" if value == "off" else "off",
                                           {"hvac_action": "off"}, force_update=True)
                self.hass.states.async_set(self.bindings.heater_power,
                                           "0" if value == "off" else "1",
                                           {"unit_of_measurement": "W"}, force_update=True)
            elif not self.ignore_off:
                self.hass.states.async_set(self.bindings.heater,
                                           "heat" if self.restore_heating else "off",
                                           {"hvac_action": "heating" if self.restore_heating else "off"},
                                           force_update=True)
                self.hass.states.async_set(self.bindings.heater_power,
                                           "1400" if self.restore_heating else "1",
                                           {"unit_of_measurement": "W"}, force_update=True)

        self.service_handler = handle
        for domain, names in (("climate", ("set_hvac_mode",)),
                              ("switch", ("turn_off", "turn_on"))):
            for name in names:
                self.hass.services.async_register(domain, name, handle)
        for entity, value, unit in (
            (self.bindings.circuit_power, "5000", "W"),
            (self.bindings.input_power, "130", "W"),
            (self.bindings.output_power, "150", "W"),
            (self.bindings.battery, "90", "%"),
            (self.bindings.wall_phase_a_voltage, "123", "V"),
            (self.bindings.wall_phase_b_voltage, "124", "V"),
            (self.bindings.heater_power, "1", "W"),
        ):
            self.hass.states.async_set(entity, value, {"unit_of_measurement": unit})
        self.hass.states.async_set(self.bindings.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(self.bindings.heater_switch, "on")
        await self.guard.async_load()
        self.guard.async_start()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.blocked)

    async def asyncTearDown(self):
        await self.guard.async_close()
        await RelayCommissioningServiceTests.asyncTearDown(self)

    async def qualify_recovery(self):
        # No simulated source timestamps: readings remain real HA reports. Move
        # only the policy clock to qualify its unchanged sixty/five-minute holds.
        self.guard._observe()
        self.monotonic[0] += 301
        self.guard._observe()
        await self.hass.async_block_till_done()

    async def test_action_uses_actual_owned_cut_persistence_cooldown_and_verified_restore(self):
        await self.invoke({"confirm": True})
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard.commissioning_active)
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertEqual(self.calls, [("switch", "turn_off", {"entity_id": self.bindings.heater_switch})])
        saved = await self.guard._store.async_load()
        self.assertTrue(saved["switch_cut_owned"])
        self.assertTrue(saved["commissioning"]["active"])
        self.guard._observe()
        self.monotonic[0] += 59
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))
        self.monotonic[0] += 242
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(self.bindings.heater).state, "off")
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.commissioning_active)
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "passed")
        self.assertEqual(sum(call[1] == "turn_on" for call in self.calls), 1)
        self.assertTrue(all(call[2].get("hvac_mode", "off") == "off" for call in self.calls))
        self.assertTrue(all(call[2]["entity_id"] in
                            {self.bindings.heater, self.bindings.heater_switch} for call in self.calls))

    async def test_real_heating_or_unavailable_meter_prevents_any_cut(self):
        self.hass.states.async_set(self.bindings.heater, "heat", {"hvac_action": "heating"})
        with self.assertRaisesRegex(HomeAssistantError, "Fresh native Off"):
            await self.invoke({"confirm": True})
        self.hass.states.async_set(self.bindings.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(self.bindings.heater_power, "unavailable", {"unit_of_measurement": "W"})
        with self.assertRaises(HomeAssistantError):
            await self.invoke({"confirm": True})
        self.assertEqual(self.calls, [])

    async def test_duplicate_invocation_and_manual_restore_do_not_authorize_another_on(self):
        await self.invoke({"confirm": True})
        with self.assertRaises(HomeAssistantError):
            await self.invoke({"confirm": True})
        self.hass.states.async_set(self.bindings.heater_switch, "on")
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        # Ordinary protection may recut a manually restored relay while native
        # feedback is still unavailable. The interrupted test must never use
        # any such new claim as permission for another automatic On.
        await self.qualify_recovery()
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))

    async def test_missing_fresh_native_off_during_restore_recuts_without_retry(self):
        await self.invoke({"confirm": True})
        self.ignore_off = True
        with patch.object(power, "RESTORE_ACK_SECONDS", .035):
            await self.qualify_recovery()
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "failed")
        self.assertEqual(sum(call[1] == "turn_on" for call in self.calls), 1)
        await self.qualify_recovery()
        self.assertEqual(sum(call[1] == "turn_on" for call in self.calls), 1)

    async def test_unload_interrupts_instead_of_rearming_the_maintenance_attempt(self):
        await self.invoke({"confirm": True})
        self.assertTrue(await self.guard.async_close())
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "interrupted")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))

    async def report_during_cut_save(self, relay_state):
        original_save = self.guard._store.async_save
        second_save, release = asyncio.Event(), asyncio.Event()
        held_cut = False

        async def gated_save(data):
            nonlocal held_cut
            if (not held_cut and data.get("switch_cut_owned") is True
                    and data.get("commissioning", {}).get("status") == "waiting_for_recovery"):
                held_cut = True
                second_save.set()
                await release.wait()
            await original_save(data)

        with patch.object(self.guard._store, "async_save", side_effect=gated_save):
            action = asyncio.create_task(self.invoke({"confirm": True}))
            action.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            try:
                await asyncio.wait_for(second_save.wait(), 1)
                self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
                self.hass.states.async_set(self.bindings.heater_switch, relay_state, force_update=True)
                # Process the actual event/check without waiting for the held
                # service/storage job itself to complete.
                await asyncio.sleep(.02)
                if relay_state == "off":
                    self.assertTrue(self.guard.commissioning_active)
                else:
                    self.assertFalse(self.guard.commissioning_active)
                    self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
            finally:
                release.set()
            if relay_state == "off":
                await action
            else:
                with self.assertRaises(HomeAssistantError):
                    await action
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))

    async def test_benign_off_force_update_during_post_cut_save_preserves_test(self):
        await self.report_during_cut_save("off")
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "waiting_for_recovery")

    async def test_manual_on_during_post_cut_save_interrupts_test(self):
        await self.report_during_cut_save("on")

    async def test_unavailable_relay_during_post_cut_save_interrupts_test(self):
        await self.report_during_cut_save("unavailable")

    async def test_restart_reads_actual_persisted_test_as_manual_recovery(self):
        await self.invoke({"confirm": True})
        await self.hass.async_block_till_done()
        # Model a lost process without invoking orderly unload, which itself
        # marks interruption. The replacement reads the actual HA storage file.
        previous = self.guard
        previous._closed = True
        for remove in previous._unsubscribers:
            remove()
        previous._unsubscribers.clear()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="native-commissioning",
            notify=lambda: None, clock=lambda: self.monotonic[0],
        )
        self.guard.commissioning_controller_off = lambda: True
        self.runtime.power_guard = self.guard
        await self.guard.async_load()
        self.guard.async_start()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.guard.diagnostics["commissioning"]["status"], "interrupted")
        await self.qualify_recovery()
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))

    async def test_failed_restore_status_does_not_allow_enable_while_late_on_is_pending(self):
        await self.invoke({"confirm": True})
        started, release = asyncio.Event(), asyncio.Event()

        async def late_on(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            self.hass.states.async_set(self.bindings.heater_switch, "on", force_update=True)

        self.hass.services.async_register("switch", "turn_on", late_on)
        with (patch.object(power, "RESTORE_ACK_SECONDS", .035),
              patch.object(power, "SERVICE_TIMEOUT_SECONDS", .02)):
            self.guard._observe()
            self.monotonic[0] += 301
            self.guard._observe()
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(.09)
                self.assertFalse(self.guard.commissioning_active)
                self.assertTrue(self.guard.commissioning_busy)
                controls = NativeControls(self.hass, self.config, True, "late-commissioning", lambda: None)
                controls.active = True
                controls.power_guard = self.guard
                with self.assertRaisesRegex(HAError, "commissioning must finish"):
                    await controls.async_set_enabled(True)
                self.assertFalse(controls.enabled)
            finally:
                release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])

    async def test_cancelled_service_request_finishes_cut_and_requires_manual_recovery(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_cut(call):
            started.set()
            await release.wait()
            await self.service_handler(call)

        self.hass.services.async_register("switch", "turn_off", slow_cut)
        with self.assertNoLogs("asyncio", level="ERROR"):
            action = asyncio.create_task(self.invoke({"confirm": True}))
            try:
                await asyncio.wait_for(started.wait(), 1)
                action.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await action
            finally:
                release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.bindings.heater_switch).state, "off")
        self.assertFalse(self.guard.commissioning_active)
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(any(call[1] == "turn_on" for call in self.calls))

    async def test_queued_commissioning_job_blocks_enable_before_physical_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.guard._commission_relay

        async def queued(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        with patch.object(self.guard, "_commission_relay", side_effect=queued):
            action = asyncio.create_task(self.invoke({"confirm": True}))
            action.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(self.guard.commissioning_busy)
                self.assertEqual(self.calls, [])
                controls = NativeControls(self.hass, self.config, True, "queued-test", lambda: None)
                controls.active = True
                controls.power_guard = self.guard
                with self.assertRaisesRegex(HAError, "commissioning must finish"):
                    await controls.async_set_enabled(True)
                self.assertFalse(controls.enabled)
            finally:
                release.set()
            await action
