"""Exercise safe reloads through Home Assistant's actual config-entry lifecycle."""
import asyncio
from contextlib import contextmanager
import logging
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import CoreState
from homeassistant.config_entries import (
    ConfigEntries, ConfigEntry, ConfigEntryState, OperationNotAllowed, SOURCE_USER,
)

from custom_components import house_climate as integration
from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.sensor import Overview, RecoveryStatus
from tests_homeassistant import test_native as native_fixtures
from tests_homeassistant import test_native_controls as control_fixtures


class ReloadShutdownTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeTests.asyncTearDown
    handle = native_fixtures.NativeTests.handle
    until = native_fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime
    claim = control_fixtures.NativeControlTests.claim
    settle_watchdog = control_fixtures.NativeControlTests.settle_watchdog

    @contextmanager
    def entry_lifecycle(self, runtime):
        """Mock setup/platforms only; use HA's real reload and entry states."""
        entry = ConfigEntry(
            version=2, minor_version=1, domain=DOMAIN, title="House climate",
            source=SOURCE_USER, data={}, options={"target_f": 72},
            unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=(),
            state=ConfigEntryState.LOADED,
        )
        entry.runtime_data = runtime
        old_manager = self.hass.config_entries
        manager = ConfigEntries(self.hass, {})
        self.hass.config_entries = manager
        self.hass.config.components.add(DOMAIN)
        self.hass.data[DOMAIN] = runtime
        loader_integration = SimpleNamespace(
            domain=DOMAIN, async_get_component=AsyncMock(return_value=integration),
            logger=logging.getLogger("test.house_climate"),
        )
        try:
            with (
                patch.object(manager, "async_get_known_entry", return_value=entry),
                patch.object(manager, "async_setup", new=AsyncMock(return_value=True)) as setup,
                patch.object(manager, "async_unload_platforms", new=AsyncMock(return_value=True)),
                patch("homeassistant.loader.async_get_integration",
                      new=AsyncMock(return_value=loader_integration)),
                patch("custom_components.house_climate.runtime.SHUTDOWN_RECHECK_SECONDS", .02),
            ):
                yield entry, manager, setup
        finally:
            self.hass.config_entries = old_manager

    async def block_off_feedback(self, runtime, state="cool"):
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        head = runtime.config.heads["guest"]
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, state, dict(old.attributes))
        calls = asyncio.Event()

        async def delayed_off(call):
            if call.data["entity_id"] == head:
                self.calls.append((call.domain, call.service, dict(call.data)))
                calls.set()
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", delayed_off)
        return head, calls

    def acknowledge_off(self, head):
        old = self.hass.states.get(head)
        self.hass.states.async_set(head, "off", {
            **old.attributes, "hvac_action": "idle",
        })

    async def test_external_reload_waits_for_delayed_off_before_leaving_loaded_lifecycle(self):
        runtime, _ = await self.runtime(entry_id="delayed_external_reload")
        head, off_called = await self.block_off_feedback(runtime)
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            reload_task = asyncio.create_task(manager.async_reload(entry.entry_id))
            await asyncio.wait_for(off_called.wait(), 2)
            self.assertEqual(entry.state, ConfigEntryState.UNLOAD_IN_PROGRESS)
            self.assertFalse(reload_task.done())
            setup.assert_not_awaited()
            self.assertIn(head, runtime.controls.owned)
            self.acknowledge_off(head)
            self.assertTrue(await asyncio.wait_for(reload_task, 2))
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertFalse(runtime.controls.active)
            self.assertEqual(await runtime.controls.store.async_load(), {"devices": []})

    async def test_options_save_stays_loaded_through_wifi_outage_and_reloads_after_off(self):
        runtime, _ = await self.runtime(entry_id="offline_saved_options")
        head, off_called = await self.block_off_feedback(runtime, "unavailable")
        with self.entry_lifecycle(runtime) as (entry, _manager, setup):
            await integration.async_update_options(self.hass, entry)
            task = runtime._options_reload_task
            await asyncio.wait_for(off_called.wait(), 2)
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            self.assertTrue(runtime._configuration_reload_pending)
            self.assertIn("Configuration saved", Overview(entry).native_value)
            self.assertIn("waiting for devices", RecoveryStatus(entry).native_value)
            setup.assert_not_awaited()
            self.assertTrue(runtime.controls.active)
            self.assertIn(head, runtime.controls.owned)
            self.assertFalse(runtime.controls.enabled)
            with self.assertRaisesRegex(HAError, "stopping"):
                await asyncio.to_thread(
                    runtime.api.service, "climate", "set_temperature",
                    entity_id=head, hvac_mode="cool", temperature=65,
                )
            # A second save coalesces into the same drain, and the saved options
            # stay authoritative until HA's normal setup reads them.
            await integration.async_update_options(self.hass, entry)
            self.assertIs(runtime._options_reload_task, task)
            old = self.hass.states.get(head)
            self.hass.states.async_set(head, "cool", dict(old.attributes))
            await asyncio.sleep(.05)
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            setup.assert_not_awaited()
            self.acknowledge_off(head)
            await asyncio.wait_for(task, 2)
            self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
            self.assertEqual(entry.options["target_f"], 72)
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertFalse(runtime.controls.active)

    async def test_external_timeout_preserves_lease_and_exposes_has_nonrecoverable_failure(self):
        runtime, _ = await self.runtime(entry_id="timed_out_external_reload")
        head, _ = await self.block_off_feedback(runtime, "unavailable")
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            with patch("custom_components.house_climate.runtime.UNLOAD_ACK_TIMEOUT_SECONDS", .04):
                self.assertFalse(await manager.async_reload(entry.entry_id))
            self.assertEqual(entry.state, ConfigEntryState.FAILED_UNLOAD)
            self.assertFalse(entry.state.recoverable)
            setup.assert_not_awaited()
            self.assertIn(head, runtime.controls.owned)
            self.assertTrue(runtime.controls.active)
            self.assertTrue(self.hass.data[DOMAIN] is runtime)
            with self.assertRaises(OperationNotAllowed):
                await manager.async_reload(entry.entry_id)
            self.acknowledge_off(head)
            self.assertTrue(await runtime.async_stop())

    async def test_saved_reload_waits_for_inflight_activation_and_stops_its_late_result(self):
        runtime, _ = await self.runtime(entry_id="late_activation_reload")
        await self.claim(runtime)
        head = runtime.config.heads["guest"]
        started, finish = asyncio.Event(), asyncio.Event()

        async def late_activation(call):
            started.set()
            await finish.wait()
            await self.handle(call)

        self.hass.services.async_register("climate", "set_temperature", late_activation)
        command = asyncio.create_task(asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=head, hvac_mode="cool", temperature=65,
        ))
        await asyncio.wait_for(started.wait(), 2)
        with self.entry_lifecycle(runtime) as (entry, _manager, setup):
            await integration.async_update_options(self.hass, entry)
            task = runtime._options_reload_task
            await self.until(lambda: runtime.stop_event.is_set())
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            setup.assert_not_awaited()
            self.assertEqual(runtime.controls.inflight, 1)
            self.assertTrue(runtime.controls.owned)
            finish.set()
            await command
            await asyncio.wait_for(task, 2)
            self.assertEqual(self.hass.states.get(head).state, "off")
            self.assertFalse(runtime.controls.inflight)
            self.assertFalse(runtime.controls.owned)
            setup.assert_awaited_once()

    async def test_saved_reload_requires_shutdown_ownership_to_be_durably_cleared(self):
        runtime, _ = await self.runtime(entry_id="reload_save_failure")
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        save = runtime.controls.store.async_save
        persist_shutdown = False

        async def save_shutdown(data):
            if not data["devices"] and not persist_shutdown:
                raise OSError("disk temporarily unavailable")
            await save(data)

        with self.entry_lifecycle(runtime) as (entry, _manager, setup):
            with patch.object(runtime.controls.store, "async_save", side_effect=save_shutdown):
                await integration.async_update_options(self.hass, entry)
                task = runtime._options_reload_task
                await self.until(lambda: bool(runtime.controls.problem))
                self.assertEqual(entry.state, ConfigEntryState.LOADED)
                self.assertTrue(runtime.controls.owned)
                self.assertTrue(runtime.controls.active)
                setup.assert_not_awaited()
                persist_shutdown = True
                runtime.controls._schedule_check()
                await asyncio.wait_for(task, 2)
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertEqual(await runtime.controls.store.async_load(), {"devices": []})

    async def test_external_reload_cancels_pending_saved_reload_without_cancelling_itself(self):
        runtime, _ = await self.runtime(entry_id="superseded_saved_reload")
        head, off_called = await self.block_off_feedback(runtime, "unavailable")
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            await integration.async_update_options(self.hass, entry)
            pending = runtime._options_reload_task
            await asyncio.wait_for(off_called.wait(), 2)
            reload_task = asyncio.create_task(manager.async_reload(entry.entry_id))
            await self.until(lambda: pending.cancelled())
            self.acknowledge_off(head)
            self.assertTrue(await asyncio.wait_for(reload_task, 2))
            setup.assert_awaited_once()
            self.assertFalse(runtime.controls.owned)
            self.assertIsNone(runtime._options_reload_task)

    async def test_core_shutdown_does_not_start_a_saved_configuration_reload(self):
        runtime, _ = await self.runtime(entry_id="saved_reload_during_core_stop")
        with self.entry_lifecycle(runtime) as (entry, manager, setup):
            previous = self.hass.state
            self.hass.set_state(CoreState.stopping)
            try:
                await integration.async_update_options(self.hass, entry)
                task = runtime._options_reload_task
                await asyncio.wait_for(task, 2)
            finally:
                self.hass.set_state(previous)
            setup.assert_not_awaited()
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            self.assertIsNone(runtime._options_reload_task)


if __name__ == "__main__":
    unittest.main()
