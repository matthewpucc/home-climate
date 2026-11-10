"""Bounded, retained unloads through HA's real config-entry state machine."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import house_climate as integration
from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controller.config import Config, FallbackPolicy
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.power_guard import HeaterPowerGuard
from custom_components.house_climate.sensor import ControllerStatus, RecoveryStatus
from tests_homeassistant import test_lifecycle_policy as lifecycle
from tests_homeassistant import test_native as native
from tests_homeassistant import test_native_controls as controls
from tests_homeassistant import test_reload_shutdown as reloads


class UnloadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeTests.asyncSetUp
    asyncTearDown = native.NativeTests.asyncTearDown
    handle = lifecycle.LifecyclePolicyTests.handle
    until = native.NativeTests.until
    runtime = controls.NativeControlTests.runtime
    claim = controls.NativeControlTests.claim
    settle_watchdog = controls.NativeControlTests.settle_watchdog
    entry_lifecycle = reloads.ReloadShutdownTests.entry_lifecycle
    block_off_feedback = reloads.ReloadShutdownTests.block_off_feedback
    acknowledge_off = reloads.ReloadShutdownTests.acknowledge_off

    async def fallback_runtime(self, minimum_off=.18):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), fallback=FallbackPolicy(),
                      head_minimum_off_seconds=minimum_off, off_settle_seconds=.01)
        for entity in cfg.heads.values():
            state = self.hass.states.get(entity)
            self.hass.states.async_set(entity, "off", {
                **state.attributes, "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            })
        return await self.runtime(cfg=cfg)

    async def test_budget_includes_stop_and_feedback_without_transition_delay(self):
        runtime, _ = await self.runtime()
        self.assertEqual(runtime._unload_budget_seconds(), 120)
        runtime.config = replace(runtime.config, fallback=FallbackPolicy(),
                                 command_timeout_seconds=45, head_minimum_off_seconds=180,
                                 off_settle_seconds=30, head_activation_timeout_seconds=120)
        device_count = len(set((*runtime.config.climate_entities, *runtime.config.fans.values())))
        self.assertEqual(runtime._unload_budget_seconds(), 45 + 45 + 120 + 5 * device_count + 20)

    async def test_healthy_fallback_can_outlast_old_ack_budget_and_reload(self):
        runtime, _ = await self.fallback_runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            with patch("custom_components.house_climate.runtime.UNLOAD_ACK_TIMEOUT_SECONDS", .12):
                self.assertTrue(await asyncio.wait_for(manager.async_reload(entry.entry_id), 2))
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertIsNone(runtime.controls.handoff)
            for entity in runtime.config.heads.values():
                state = self.hass.states.get(entity)
                self.assertEqual(state.state, "heat_cool")
                self.assertEqual((state.attributes["target_temp_low"],
                                  state.attributes["target_temp_high"]), (68, 73))
            self.assertEqual(runtime._unload_phase, "complete")

    async def test_caller_cancellation_does_not_orphan_unload_or_cancel_actual_cleanup(self):
        runtime, _ = await self.runtime()
        head, off_called = await self.block_off_feedback(runtime)
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            request = asyncio.create_task(manager.async_reload(entry.entry_id))
            await asyncio.wait_for(off_called.wait(), 2)
            retained = runtime._unload_task
            request.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(request.done())
            self.assertFalse(retained.cancelled())
            self.assertEqual(entry.state, ConfigEntryState.UNLOAD_IN_PROGRESS)
            setup.assert_not_awaited()
            self.acknowledge_off(head)
            self.assertTrue(await asyncio.wait_for(request, 2))
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            setup.assert_awaited_once()
            self.assertIs(runtime._unload_task, retained)
            self.assertFalse(runtime.controls.owned)

    async def test_saved_options_reload_preserves_original_caller_identity(self):
        runtime, _ = await self.fallback_runtime(.03)
        await self.claim(runtime)
        with self.entry_lifecycle(runtime) as (entry, _manager, setup):
            await integration.async_update_options(self.hass, entry)
            request = runtime._options_reload_task
            await asyncio.wait_for(asyncio.shield(request), 2)
            self.assertFalse(request.cancelled())
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertIsNone(runtime._options_reload_task)

    async def test_predeadline_storage_stall_is_bounded_without_cancelling_or_forgetting_claim(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        entered, release = asyncio.Event(), asyncio.Event()
        save = runtime.controls.store.async_save

        async def gated_save(data):
            entered.set()
            await release.wait()
            await save(data)

        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            with (patch.object(runtime, "_unload_budget_seconds", return_value=.05),
                  patch.object(runtime.controls.store, "async_save", side_effect=gated_save)):
                request = asyncio.create_task(manager.async_reload(entry.entry_id))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.assertFalse(await asyncio.wait_for(request, .5))
                    self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)
                    self.assertTrue(runtime._unload_timed_out)
                    self.assertEqual(runtime._unload_phase, "ownership_storage")
                    runtime._async_report("stopped", {})
                    self.assertEqual(runtime.status["unload"]["phase"], "ownership_storage")
                    for sensor in (ControllerStatus(entry), RecoveryStatus(entry)):
                        self.assertTrue(sensor.extra_state_attributes["unload"]["timed_out"])
                        self.assertIn("sampled_at", sensor.extra_state_attributes["unload"])
                    self.assertFalse(runtime._unload_task.done())
                    self.assertIs(self.hass.data[DOMAIN], runtime)
                    self.assertTrue(runtime.controls.owned)
                    manager.async_unload_platforms.assert_not_awaited()
                    setup.assert_not_awaited()
                finally:
                    release.set()
                self.assertTrue(await asyncio.wait_for(asyncio.shield(runtime._unload_task), 2))
            manager.async_unload_platforms.assert_awaited_once()
            self.assertNotIn(DOMAIN, self.hass.data)
            self.assertFalse(runtime.controls.owned)
            # A late completion cannot pretend HA completed the timed-out call.
            self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)
            setup.assert_not_awaited()
            retained = runtime._unload_task
            self.assertTrue(await integration.async_unload_entry(self.hass, entry))
            self.assertIs(runtime._unload_task, retained)
            manager.async_unload_platforms.assert_awaited_once()

    async def test_platform_cleanup_is_in_retained_operation_through_cancellation(self):
        runtime, _ = await self.runtime()
        entered, release = asyncio.Event(), asyncio.Event()

        async def platforms(*_args):
            entered.set()
            await release.wait()
            return True

        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            manager.async_unload_platforms.side_effect = platforms
            request = asyncio.create_task(manager.async_reload(entry.entry_id))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                request.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(request.done())
                self.assertIs(self.hass.data[DOMAIN], runtime)
                setup.assert_not_awaited()
            finally:
                release.set()
            self.assertTrue(await asyncio.wait_for(request, 2))
            manager.async_unload_platforms.assert_awaited_once()
            setup.assert_awaited_once()
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)

    async def test_platform_timeout_retains_runtime_until_actual_cleanup_finishes(self):
        runtime, _ = await self.runtime()
        entered, release = asyncio.Event(), asyncio.Event()

        async def platforms(*_args):
            entered.set()
            await release.wait()
            return True

        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            manager.async_unload_platforms.side_effect = platforms
            with patch.object(runtime, "_unload_budget_seconds", return_value=.04):
                request = asyncio.create_task(manager.async_reload(entry.entry_id))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.assertFalse(await asyncio.wait_for(request, .5))
                    retained = runtime._unload_task
                    self.assertFalse(retained.done())
                    self.assertIs(self.hass.data[DOMAIN], runtime)
                    self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)
                    setup.assert_not_awaited()
                    # Another waiter joins this same pending operation; it
                    # cannot restart cleanup or manufacture a fresh time budget.
                    self.assertFalse(await integration.async_unload_entry(self.hass, entry))
                    self.assertIs(runtime._unload_task, retained)
                    manager.async_unload_platforms.assert_awaited_once()
                finally:
                    release.set()
                self.assertTrue(await asyncio.wait_for(asyncio.shield(retained), 2))
            self.assertNotIn(DOMAIN, self.hass.data)
            setup.assert_not_awaited()

    async def test_completed_blocked_attempt_can_recheck_new_evidence_without_overlapping(self):
        runtime, _ = await self.runtime()
        head, _ = await self.block_off_feedback(runtime, "unavailable")
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            with patch.object(runtime, "_unload_budget_seconds", return_value=.04):
                self.assertFalse(await manager.async_reload(entry.entry_id))
                prior = runtime._unload_task
                self.assertFalse(await asyncio.wait_for(asyncio.shield(prior), 2))
            self.assertIs(self.hass.data[DOMAIN], runtime)
            self.assertIn(head, runtime.controls.owned)
            self.acknowledge_off(head)
            # HA's FAILED_UNLOAD state is deliberately not rewritten here. A
            # subsequent supported hook invocation must nevertheless recheck
            # actual cleanup, not cache the prior False result forever.
            self.assertTrue(await integration.async_unload_entry(self.hass, entry))
            self.assertIsNot(runtime._unload_task, prior)
            self.assertFalse(runtime.controls.owned)
            manager.async_unload_platforms.assert_awaited_once()
            setup.assert_not_awaited()
            self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)

    async def test_cancelled_retained_operation_returns_false_instead_of_orphaning_ha(self):
        runtime, _ = await self.runtime()
        entered, release = asyncio.Event(), asyncio.Event()

        async def platforms(*_args):
            entered.set()
            await release.wait()
            return True

        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            manager.async_unload_platforms.side_effect = platforms
            request = asyncio.create_task(manager.async_reload(entry.entry_id))
            await asyncio.wait_for(entered.wait(), 2)
            runtime._unload_task.cancel()
            self.assertFalse(await asyncio.wait_for(request, 2))
            self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)
            self.assertIs(self.hass.data[DOMAIN], runtime)
            self.assertEqual(runtime._unload_phase, "cancelled")
            setup.assert_not_awaited()

    async def test_actual_late_activation_remains_owned_until_fresh_off(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        head = runtime.config.heads["guest"]
        started, release = asyncio.Event(), asyncio.Event()

        async def activation(call):
            # An optimistic state update is not completion of this service.
            await self.handle(call)
            started.set()
            await release.wait()
            await self.handle(call)

        self.hass.services.async_register("climate", "set_temperature", activation)
        command = asyncio.create_task(asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=head, hvac_mode="cool", temperature=65))
        await asyncio.wait_for(started.wait(), 2)
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            request = asyncio.create_task(manager.async_reload(entry.entry_id))
            try:
                await self.until(lambda: runtime.stop_event.is_set())
                await asyncio.sleep(.02)
                self.assertFalse(request.done())
                setup.assert_not_awaited()
                self.assertIn(head, runtime.controls.owned)
                self.assertTrue(runtime.controls.inflight)
                self.assertIs(self.hass.data[DOMAIN], runtime)
            finally:
                release.set()
            await command
            self.assertTrue(await asyncio.wait_for(request, 2))
            self.assertEqual(self.hass.states.get(head).state, "off")
            self.assertFalse(runtime.controls.inflight)
            self.assertFalse(runtime.controls.owned)
            setup.assert_awaited_once()

    async def test_shutdown_does_not_start_a_replacement_worker(self):
        previous = self.hass.state
        self.hass.set_state(CoreState.stopping)
        try:
            with patch.object(integration, "ControllerRuntime") as constructor:
                with self.assertRaisesRegex(ConfigEntryNotReady, "stopping"):
                    await integration.async_setup_entry(self.hass, SimpleNamespace())
                constructor.assert_not_called()
        finally:
            self.hass.set_state(previous)

    async def test_shutdown_during_config_read_is_rechecked_before_construction(self):
        previous = self.hass.state
        entry = SimpleNamespace(data={}, options={}, title="Synthetic", entry_id="stopping_config")

        async def config_read(*_args):
            self.hass.set_state(CoreState.stopping)
            return self.cfg

        try:
            with (patch.object(self.hass, "async_add_executor_job", side_effect=config_read),
                  patch.object(integration, "validate_actuators"),
                  patch.object(integration, "ControllerRuntime") as constructor):
                with self.assertRaisesRegex(ConfigEntryNotReady, "stopped while loading"):
                    await integration.async_setup_entry(self.hass, entry)
                constructor.assert_not_called()
        finally:
            self.hass.set_state(previous)

    async def test_delayed_start_callback_cannot_launch_worker_while_ha_stops(self):
        runtime, _ = await self.runtime()
        previous = self.hass.state
        self.hass.set_state(CoreState.stopping)
        try:
            runtime.async_start()
            self.assertIsNone(runtime.thread)
            self.assertIsNone(runtime.finished)
        finally:
            self.hass.set_state(previous)

    async def test_late_actual_guard_service_can_finish_within_original_unload_budget(self):
        runtime, _ = await self.runtime()
        bindings = example_bindings().power
        guard = HeaterPowerGuard(self.hass, bindings, live=False,
                                 entry_id="unload_guard", notify=lambda: None)
        runtime.power_guard = guard
        started, release = asyncio.Event(), asyncio.Event()

        async def switch_off(call):
            started.set()
            await release.wait()
            self.hass.states.async_set(bindings.ac_output_switch, "off")

        self.hass.services.async_register("switch", "turn_off", switch_off)
        actual = self.hass.async_create_background_task(self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": bindings.ac_output_switch}, blocking=True),
            "Synthetic pending guard Off", eager_start=False)
        # This is the guard's actual outstanding emergency service registry;
        # the real handler remains pending even if states already report Off.
        guard._emergency_service_tasks.add(actual)
        actual.add_done_callback(guard._emergency_service_tasks.discard)
        await asyncio.wait_for(started.wait(), 2)
        self.hass.states.async_set(bindings.ac_output_switch, "off")
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            with patch.object(runtime, "_unload_budget_seconds", return_value=2):
                request = asyncio.create_task(manager.async_reload(entry.entry_id))
                try:
                    await self.until(lambda: runtime._unload_phase == "heater_guard")
                    self.assertFalse(request.done())
                    self.assertIs(self.hass.data[DOMAIN], runtime)
                    self.assertFalse(guard._closed)
                    setup.assert_not_awaited()
                finally:
                    release.set()
                await actual
                runtime._shutdown_progress.set()
                self.assertTrue(await asyncio.wait_for(request, 2))
            self.assertTrue(guard._closed)
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            setup.assert_awaited_once()
