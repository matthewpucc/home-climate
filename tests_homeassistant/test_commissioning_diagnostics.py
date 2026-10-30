"""Publish commissioning results on the existing real HA diagnostic entity."""
import json
import logging
import tempfile
from datetime import timedelta
from types import MappingProxyType
from types import SimpleNamespace
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.house_climate.const import DOMAIN, SIGNAL_STATUS
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate import power_guard as power
from custom_components.house_climate.sensor import HeaterPowerStatus


ORIGINAL_GUARD_KEYS = {
    "blocked", "reason", "latched", "switch_cut_owned", "power_cut_confirmed",
    "restoring", "restore_attempts", "power_restore_inflight", "power_restore_cleanup_pending",
    "persistence_error", "live", "heater_cutoff_configured", "heater_current_configured",
    "heater_power_configured", "heater_meter_kind", "electrical_stop_confirmed", "circuit_enforced",
    "shutdown_stage", "shutdown_confirmation", "aggregate_stop", "wall_power_budget",
    "emergency_ac_enabled", "emergency_ac_latched", "emergency_ac_status", "emergency_ac_attempts",
    "emergency_ac_inflight", "emergency_notification_error", "manual_recovery_required",
    "binding_identity_mismatch",
}
COMMISSIONING_KEYS = {
    "active", "status", "reason", "started_at", "finished_at", "restore_window_seconds",
    "maximum_seconds", "phase", "staged_watts", "meter_acquire_seconds", "meter_report_gap_seconds",
    "native_reconnect_seconds", "post_off_meter_seconds", "phase_started_at", "session_deadline_at",
}


class CommissioningDiagnosticPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.service_calls = []
        self.hass.bus.async_listen("call_service", self.service_calls.append)
        self.guard = power.HeaterPowerGuard(
            self.hass, example_bindings().power, live=False,
            entry_id="diagnostic-controller", notify=lambda: None,
        )
        self.runtime = SimpleNamespace(power_guard=self.guard)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.entry = ConfigEntry(
            version=3, minor_version=1, domain=DOMAIN, entry_id="diagnostic-controller",
            title="Home Climate", source="user", data={}, options={}, unique_id=None,
            discovery_keys=MappingProxyType({}), subentries_data=(),
        )
        self.entry.runtime_data = self.runtime
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        self.sensor = HeaterPowerStatus(self.entry)
        self.sensor.entity_id = "sensor.commissioning_heater_power_guard"
        self.platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__), domain="sensor",
            platform_name=DOMAIN, platform=None, scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        self.platform.config_entry = self.entry
        await self.platform.async_add_entities([self.sensor])
        await self.hass.async_block_till_done()

    async def asyncTearDown(self):
        await self.platform.async_reset()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def publish(self):
        async_dispatcher_send(self.hass, SIGNAL_STATUS)
        await self.hass.async_block_till_done()
        return self.hass.states.get(self.sensor.entity_id)

    async def test_active_commissioning_is_published_without_new_entity_or_control_actions(self):
        self.guard._commissioning.update(
            active=True, status="restoring", phase="native_reconnect",
            reason="Fresh low watts supervised while native HA control reconnects",
            started_at=1793300000.0, phase_started_at=1793300300.0,
            session_deadline_at=1793300600.0,
        )
        before_ids = set(self.hass.states.async_entity_ids())
        state = await self.publish()
        self.assertEqual(state.attributes["commissioning"]["status"], "restoring")
        self.assertEqual(state.attributes["commissioning"]["phase"], "native_reconnect")
        self.assertEqual(state.attributes["commissioning"]["started_at"], 1793300000.0)
        self.assertEqual(state.state, "Preview only; live commands are not permitted")
        self.assertEqual(self.sensor.unique_id, "diagnostic-controller_heater_power_guard")
        self.assertEqual(set(self.hass.states.async_entity_ids()), before_ids)
        self.assertEqual(self.service_calls, [])

    async def test_terminal_results_publish_without_replacing_the_diagnostic_entity(self):
        registry = er.async_get(self.hass)
        original_registry_id = registry.async_get(self.sensor.entity_id).id
        for status in ("passed", "failed", "interrupted"):
            with self.subTest(status=status):
                self.guard._commissioning.update(
                    active=False, status=status, reason=f"Synthetic {status} result",
                    started_at=1793300000.0, finished_at=1793300450.0,
                )
                state = await self.publish()
                self.assertEqual(state.attributes["commissioning"]["status"], status)
                self.assertIs(state.attributes["commissioning"]["active"], False)
                self.assertEqual(state.attributes["commissioning"]["finished_at"], 1793300450.0)
                self.assertEqual(registry.async_get(self.sensor.entity_id).id, original_registry_id)
        self.assertEqual(self.service_calls, [])

    async def restart_with_saved_result(self, status, **metadata):
        await self.guard.async_load()
        self.guard._commissioning.update({"active": False, "status": status,
                                         "reason": f"Synthetic persisted {status}", **metadata})
        await self.guard._store.async_save(self.guard._saved_state())
        self.guard = power.HeaterPowerGuard(
            self.hass, example_bindings().power, live=False,
            entry_id="diagnostic-controller", notify=lambda: None,
        )
        await self.guard.async_load()
        self.runtime.power_guard = self.guard
        return await self.publish()

    async def test_saved_terminal_timestamps_survive_real_storage_and_runtime_recreation(self):
        for status in ("passed", "failed", "interrupted"):
            with self.subTest(status=status):
                state = await self.restart_with_saved_result(
                    status, started_at=1793300000.25, finished_at=1793300460.75,
                )
                result = state.attributes["commissioning"]
                self.assertEqual(result["status"], status)
                self.assertEqual(result["started_at"], 1793300000.25)
                self.assertEqual(result["finished_at"], 1793300460.75)
                self.assertIs(state.attributes["manual_recovery_required"], False)
                self.assertIs(state.attributes["latched"], False)
        self.assertEqual(self.service_calls, [])

    async def test_bad_saved_metadata_is_null_and_extra_history_is_not_published(self):
        state = await self.restart_with_saved_result(
            "passed", started_at="not a timestamp", finished_at={"unexpected": "object"},
            reason="R" * 10000, raw_history=["not part of the diagnostic contract"] * 100,
            session_deadline_at="invalid saved timer",
        )
        result = state.attributes["commissioning"]
        self.assertEqual(result["status"], "passed")
        self.assertIsNone(result["started_at"])
        self.assertIsNone(result["finished_at"])
        self.assertLessEqual(len(result["reason"]), 240)
        self.assertNotIn("raw_history", result)
        self.assertNotIn("session_deadline_at", result)
        self.assertTrue(set(result) <= COMMISSIONING_KEYS)
        self.assertIs(state.attributes["manual_recovery_required"], False)
        self.assertEqual(self.service_calls, [])

    def fresh_electricals(self):
        bindings = self.guard.bindings
        for entity, value, unit in (
            (bindings.circuit_power, "140", "W"), (bindings.input_power, "130", "W"),
            (bindings.output_power, "150", "W"), (bindings.battery, "90", "%"),
            (bindings.heater_current, ".05", "A"),
            (bindings.wall_phase_a_voltage, "123", "V"),
            (bindings.wall_phase_b_voltage, "124", "V"),
        ):
            self.hass.states.async_set(entity, value, {"unit_of_measurement": unit})
        self.hass.states.async_set(bindings.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(bindings.heater_switch, "on")
        self.guard._observe()

    async def test_inactive_shutdown_projection_leaves_stored_evidence_untouched(self):
        self.fresh_electricals()
        old_evidence = SimpleNamespace(as_dict=lambda: {"status": "unknown", "reason": "Old report expired"})
        self.guard._shutdown_stage = "dyson_off"
        self.guard._shutdown_confirmation = "state_only"
        self.guard._aggregate_outcome = old_evidence
        state = await self.publish()
        self.assertIs(state.attributes["shutdown_active"], False)
        self.assertEqual(state.attributes["shutdown_stage"], "idle")
        self.assertEqual(state.attributes["shutdown_confirmation"], "inactive")
        self.assertIsNone(state.attributes["aggregate_stop"])
        self.assertIs(state.attributes["electrical_stop_confirmed"], False)
        self.assertEqual(self.guard._shutdown_stage, "dyson_off")
        self.assertEqual(self.guard._shutdown_confirmation, "state_only")
        self.assertIs(self.guard._aggregate_outcome, old_evidence)
        self.assertEqual(self.service_calls, [])

    async def test_active_failed_shutdown_keeps_its_unknown_confirmation_visible(self):
        self.fresh_electricals()
        self.guard._latched = True
        self.guard._shutdown_stage = "dedicated_off"
        self.guard._shutdown_confirmation = "unconfirmed"
        self.guard._aggregate_outcome = SimpleNamespace(
            as_dict=lambda: {"status": "unknown", "reason": "Fresh electrical evidence is missing"})
        state = await self.publish()
        self.assertIs(state.attributes["shutdown_active"], True)
        self.assertEqual(state.attributes["shutdown_stage"], "dedicated_off")
        self.assertEqual(state.attributes["shutdown_confirmation"], "unconfirmed")
        self.assertEqual(state.attributes["aggregate_stop"]["status"], "unknown")
        self.assertIs(state.attributes["latched"], True)
        self.assertEqual(self.service_calls, [])

    async def test_projection_preserves_existing_keys_and_keeps_result_size_bounded(self):
        self.guard._commissioning.update(active=True, status="restoring", phase="meter_confirmation")
        self.guard._commissioning_failed("Synthetic failure: " + "R" * 10000)
        state = await self.publish()
        attrs = self.sensor.extra_state_attributes
        self.assertEqual(set(attrs), ORIGINAL_GUARD_KEYS | {"shutdown_active", "commissioning"})
        result = state.attributes["commissioning"]
        self.assertTrue(set(result) <= COMMISSIONING_KEYS)
        self.assertLessEqual(len(result["reason"]), 240)
        self.assertLess(len(json.dumps(result, allow_nan=False).encode()), 2048)
        self.assertLess(len(json.dumps(dict(state.attributes), allow_nan=False).encode()), 8192)
        self.assertEqual(self.service_calls, [])
