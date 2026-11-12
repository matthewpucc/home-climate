"""Keep live transition evidence without overflowing HA Recorder attributes."""
import json
import tempfile
from types import SimpleNamespace
import unittest

from homeassistant.core import Event, HomeAssistant, State, EVENT_STATE_CHANGED
from homeassistant.components.recorder.db_schema import StateAttributes, MAX_STATE_ATTRS_BYTES

from custom_components.house_climate.controller.diagnostics import (
    HeadTransitions, HEAD_TRANSITION_LIMIT, bounded_head_history,
)
from custom_components.house_climate.diagnostics import async_get_config_entry_diagnostics
from custom_components.house_climate.sensor import ControllerStatus, RecoveryStatus


class RecorderStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.heads = {f"room_{i}": f"climate.synthetic_head_{i}" for i in range(4)}
        evidence = HeadTransitions(self.heads.values())
        observed = {"state": "off", "last_reported": "2026-01-02T09:00:00+00:00",
                    "attributes": {"temperature": 21, "current_temperature": 22,
                                   "hvac_modes": ["off", "heat", "cool", "heat_cool", "fan_only", "dry"]}}
        for head in self.heads.values():
            for _ in range(HEAD_TRANSITION_LIMIT):
                evidence.record(head, "command_acknowledged", observed,
                                command_id=1, service="set_hvac_mode", data={"hvac_mode": "off"})
        self.status = {"event": "live", "phase": "running", "mode": "cool",
                       "head_transition_history": evidence.snapshot(),
                       "head_stop_reasons": {head: {"no_demand": True} for head in self.heads.values()},
                       "heartbeat": {"state": "confirmed", "blocked": False},
                       "fault_count": 0}
        self.runtime = SimpleNamespace(status=self.status, config=SimpleNamespace(heads=self.heads))
        self.entry = SimpleNamespace(entry_id="recorder_fixture", runtime_data=self.runtime)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_real_recorder_filters_before_size_check_preserving_live_attributes(self):
        for cls in (ControllerStatus, RecoveryStatus):
            entity = cls(self.entry)
            entity.hass = self.hass
            entity.entity_id = "sensor.synthetic_" + cls.__name__.lower()
            entity.platform = SimpleNamespace(platform_name="house_climate", config_entry=None)
            # Use HA's real lifecycle to construct the class-derived StateInfo.
            await entity.async_internal_added_to_hass()
            attrs = entity.extra_state_attributes
            self.assertEqual(attrs["head_transition_history"], self.status["head_transition_history"])
            self.assertGreater(len(json.dumps(attrs).encode()), MAX_STATE_ATTRS_BYTES)
            state = State(entity.entity_id, "live", attrs, state_info=entity._state_info)
            event = Event(EVENT_STATE_CHANGED, {"entity_id": entity.entity_id, "new_state": state})
            serialized = StateAttributes.shared_attrs_bytes_from_event(event, None)
            self.assertLess(len(serialized), MAX_STATE_ATTRS_BYTES)
            recorded = json.loads(serialized)
            self.assertNotIn("head_transition_history", recorded)
            self.assertEqual(recorded["phase"], "running")
            self.assertEqual(recorded["head_stop_reasons"], self.status["head_stop_reasons"])
            self.assertFalse(recorded["heartbeat"]["blocked"])
            summary = recorded["head_transition_summary"]
            self.assertEqual(len(summary), 4)
            for head, rows in self.status["head_transition_history"].items():
                self.assertEqual(summary[head]["retained_event_count"], 16)
                self.assertEqual(summary[head]["last_sequence"], rows[-1]["sequence"])
                self.assertEqual(summary[head]["last_event"], rows[-1]["event"])
            # Recorder rejection in the old path loses all attributes, not just history.
            event.data["new_state"] = State(entity.entity_id, "live", attrs)
            with self.assertLogs("homeassistant.components.recorder.db_schema", level="WARNING"):
                self.assertEqual(StateAttributes.shared_attrs_bytes_from_event(event, None), b"{}")
            await entity.async_internal_will_remove_from_hass()

    async def test_download_is_bounded_configured_safe_copy_and_reset_is_not_backfilled(self):
        head = next(iter(self.heads.values()))
        row = self.status["head_transition_history"][head][-1]
        row.update(secret="never-export", configuration={"token": "never-export"})
        row["data"]["token"] = "never-export"
        self.status["head_transition_history"][head] = [row] * 40
        self.status["head_transition_history"]["climate.unconfigured"] = [row]
        result = await async_get_config_entry_diagnostics(self.hass, self.entry)
        download = result["head_transitions"]
        self.assertEqual(download["persistence"], "process_local")
        self.assertEqual(download["history_limit_per_head"], 16)
        self.assertNotIn("climate.unconfigured", download["recent_events"])
        self.assertEqual(len(download["recent_events"][head]), 16)
        self.assertNotIn("never-export", json.dumps(download))
        download["recent_events"][head][-1]["data"]["hvac_mode"] = "changed"
        self.assertEqual(row["data"]["hvac_mode"], "off")
        self.runtime.status = {}
        restarted = await async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertEqual(restarted["head_transitions"]["recent_events"], {})

    async def test_malformed_history_and_minimal_runtime_do_not_break_diagnostics(self):
        for history in (None, 12, [], "bad", {next(iter(self.heads.values())): None},
                        {next(iter(self.heads.values())): [None, "bad"]}):
            self.assertEqual(bounded_head_history({"head_transition_history": history}, self.heads.values()), {})
        minimal = SimpleNamespace(entry_id="minimal", runtime_data=SimpleNamespace(status={"event": "stopped"}))
        for cls in (ControllerStatus, RecoveryStatus):
            self.assertEqual(cls(minimal).extra_state_attributes["head_transition_summary"], {})
