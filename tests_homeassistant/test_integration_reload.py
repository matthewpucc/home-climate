"""Automatic recovery uses the heat-pump entity's real config-entry ownership."""
import asyncio
import threading
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.house_climate.recovery import RecoveryManager
from tests_homeassistant import test_native as fixtures


class IntegrationReloadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    asyncTearDownBase = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.owner = self.entry("mitsubishi", "heat_pump_owner")
        self.head = self.cfg.heads["living"]
        self.register(self.head, self.owner)
        self.clock = Mock(return_value=0)
        self.runtime = SimpleNamespace(
            config=self.cfg, live=True, controls=SimpleNamespace(enabled=True),
            stop_event=threading.Event(), _async_recovery_changed=Mock(),
        )
        self.manager = RecoveryManager(self.hass, self.runtime, clock=self.clock)
        self.reload = AsyncMock(return_value=True)
        self.reload_patch = patch.object(ConfigEntries, "async_reload", self.reload)
        self.reload_patch.start()
        self.hass.states.async_set(self.head, "unavailable", {})

    async def asyncTearDown(self):
        await self.manager.async_close()
        self.reload_patch.stop()
        await self.asyncTearDownBase()

    def entry(self, domain, entry_id, state=ConfigEntryState.LOADED):
        entry = ConfigEntry(
            version=1, minor_version=1, domain=domain, entry_id=entry_id,
            title="Heat-pump bridge", data={}, options={}, source="user",
            unique_id=None, discovery_keys=MappingProxyType({}), state=state,
            subentries_data=None,
        )
        # Register real entries without loading external integration code.
        self.hass.config_entries._entries[entry.entry_id] = entry
        return entry

    def register(self, entity, owner):
        domain, object_id = entity.split(".", 1)
        return er.async_get(self.hass).async_get_or_create(
            domain, owner.domain, entity, suggested_object_id=object_id,
            config_entry=owner,
        )

    def status(self, *entities, kind="recovery", reason="Device unavailable; off requested"):
        return {
            "event": "live", "phase": "stopping",
            "recovery_details": {
                entity: {"kind": kind, "reason": reason} for entity in entities
            },
        }

    async def failed_for(self, seconds=120, *entities):
        status = self.status(*(entities or (self.head,)))
        self.manager.observe(status)
        self.clock.return_value += seconds
        self.manager.observe(status)
        if self.manager._task is not None:
            await self.manager._task
        return status

    async def test_sustained_failure_reloads_owning_entry_once_without_actuator_writes(self):
        self.manager.observe(self.status(self.head))
        self.clock.return_value = 119
        self.manager.observe(self.status(self.head))
        self.reload.assert_not_awaited()
        self.clock.return_value = 120
        self.manager.observe(self.status(self.head))
        await self.manager._task
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get(self.head).state, "unavailable")
        detail = self.manager.diagnostics["reload_details"][self.owner.entry_id]
        self.assertEqual(detail["entities"], [self.head])
        self.assertEqual(detail["attempts"], 1)
        self.assertIn("Reload completed", detail["last_result"])
        self.assertIsNotNone(detail["next_retry_at"])
        self.runtime._async_recovery_changed.assert_called_once()

    async def test_cooldown_backoff_and_three_attempt_budget(self):
        status = await self.failed_for()
        self.clock.return_value = 1019
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 1)
        self.clock.return_value = 1020
        self.manager.observe(status)
        await self.manager._task
        self.assertEqual(self.reload.await_count, 2)
        self.clock.return_value = 2819
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 2)
        self.clock.return_value = 2820
        self.manager.observe(status)
        await self.manager._task
        self.assertEqual(self.reload.await_count, 3)
        self.clock.return_value = 100_000
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 3)
        self.assertIn("paused after three", self.manager.diagnostics["reload_status"])

    async def test_known_cooling_only_capability_does_not_trigger_a_reload(self):
        self.hass.states.async_set(self.head, "off", {"hvac_modes": ["off", "cool"]})
        status = self.status(self.head, kind="capability",
                             reason="Requested heat unavailable; HA modes: off, cool")
        self.manager.observe(status)
        self.clock.return_value = 100_000
        self.manager.observe(status)
        self.reload.assert_not_awaited()
        self.assertEqual(self.manager.diagnostics["reload_status"], "Idle")

    async def test_command_timeout_on_available_entity_can_trigger_reload(self):
        self.hass.states.async_set(self.head, "off", {"hvac_modes": ["off", "cool"]})
        status = self.status(self.head, reason="Command acknowledgement timed out")
        self.manager.observe(status)
        self.clock.return_value = 120
        self.manager.observe(status)
        await self.manager._task
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def test_shadow_disabled_and_stopping_controllers_do_not_reload(self):
        for change in (
            lambda: setattr(self.runtime, "live", False),
            lambda: setattr(self.runtime.controls, "enabled", False),
            lambda: self.runtime.stop_event.set(),
        ):
            self.runtime.live = True
            self.runtime.controls.enabled = True
            self.runtime.stop_event.clear()
            change()
            self.manager.observe(self.status(self.head))
            self.clock.return_value += 1000
            self.manager.observe(self.status(self.head))
            self.reload.assert_not_awaited()

    async def test_healthy_report_resets_incident_but_preserves_cooldown(self):
        status = await self.failed_for()
        self.hass.states.async_set(self.head, "off", {"hvac_modes": ["off", "cool"]})
        self.clock.return_value = 200
        self.manager.observe({"event": "live", "recovery_details": {}})
        self.assertNotIn(self.head, self.manager._first_seen)
        self.hass.states.async_set(self.head, "unavailable", {})
        self.manager.observe(status)
        self.clock.return_value = 320
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 1)
        self.assertIn("cooldown", self.manager.diagnostics["reload_status"])
        self.clock.return_value = 1020
        self.manager.observe(status)
        await self.manager._task
        self.assertEqual(self.reload.await_count, 2)
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)

    async def test_fault_report_without_plan_is_not_healthy_confirmation(self):
        status = await self.failed_for()
        self.manager.observe({"event": "fault", "error": "Unrelated configuration error"})
        self.assertIn(self.head, self.manager._first_seen)
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)
        self.clock.return_value = 1020
        self.manager.observe(status)
        await self.manager._task
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 2)

    async def test_same_entry_heads_are_one_reload_even_if_other_head_is_running(self):
        guest = self.cfg.heads["guest"]
        self.register(guest, self.owner)
        self.hass.states.async_set(guest, "cool", {"hvac_modes": ["off", "cool", "heat"]})
        await self.failed_for()
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        detail = self.manager.diagnostics["reload_details"][self.owner.entry_id]
        self.assertEqual(set(detail["affected_heads"]), {guest, self.head})
        self.assertEqual(self.hass.states.get(guest).state, "cool")
        self.assertEqual(self.calls, [])

    async def test_unregistered_self_owned_or_busy_entry_is_not_reloaded(self):
        unknown = self.cfg.heads["guest"]
        own = self.entry("house_climate", "controller")
        self.register(unknown, own)
        busy = self.cfg.heads["bonus"]
        busy_owner = self.entry("mitsubishi", "busy", ConfigEntryState.SETUP_IN_PROGRESS)
        self.register(busy, busy_owner)
        unregistered = self.cfg.heads["primary"]
        status = self.status(unknown, busy, unregistered)
        self.manager.observe(status)
        self.clock.return_value = 120
        self.manager.observe(status)
        self.reload.assert_not_awaited()
        self.assertIn("No reloadable", self.manager.diagnostics["reload_details"][unknown]["reason"])
        self.assertIn("setup_in_progress", self.manager.diagnostics["reload_details"]["busy"]["reason"])
        self.assertIn("No reloadable", self.manager.diagnostics["reload_details"][unregistered]["reason"])

    async def test_reload_failures_are_bounded_and_reported(self):
        self.reload.side_effect = RuntimeError("Bridge is unavailable")
        with self.assertLogs("custom_components.house_climate.recovery", level="WARNING"):
            status = await self.failed_for()
        detail = self.manager.diagnostics["reload_details"][self.owner.entry_id]
        self.assertIn("Bridge is unavailable", detail["last_result"])
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 1)

    async def test_close_cancels_reload_and_prevents_later_attempts(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_reload(_entry_id):
            started.set()
            await finish.wait()
            return True

        self.reload.side_effect = slow_reload
        status = self.status(self.head)
        self.manager.observe(status)
        self.clock.return_value = 120
        self.manager.observe(status)
        await started.wait()
        await self.manager.async_close()
        self.clock.return_value = 100_000
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 1)
        self.runtime._async_recovery_changed.assert_not_called()

    async def test_two_failed_owners_are_reloaded_serially(self):
        other_head = self.cfg.heads["guest"]
        other_owner = self.entry("mitsubishi", "other_bridge")
        self.register(other_head, other_owner)
        self.hass.states.async_set(other_head, "unavailable", {})
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_reload(_entry_id):
            started.set()
            await finish.wait()
            return True

        self.reload.side_effect = slow_reload
        status = self.status(self.head, other_head)
        self.manager.observe(status)
        self.clock.return_value = 120
        self.manager.observe(status)
        await started.wait()
        self.manager.observe(status)
        self.assertEqual(self.reload.await_count, 1)
        finish.set()
        await self.manager._task
        self.manager.observe(status)
        await self.manager._task
        self.assertEqual(self.reload.await_count, 2)
        self.assertEqual({call.args[0] for call in self.reload.await_args_list},
                         {self.owner.entry_id, other_owner.entry_id})

    async def test_legacy_helper_must_be_enabled(self):
        self.runtime.controls = None
        self.hass.states.async_set(self.cfg.entity("enabled"), "off", {})
        await self.failed_for()
        self.reload.assert_not_awaited()
        self.hass.states.async_set(self.cfg.entity("enabled"), "on", {})
        await self.failed_for()
        self.reload.assert_awaited_once_with(self.owner.entry_id)
