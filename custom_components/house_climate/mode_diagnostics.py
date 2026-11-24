"""Bounded native mode attribution, independent of equipment control."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from math import isfinite

from homeassistant.core import Context, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event
from homeassistant.helpers.storage import Store

from .unexpected_off import UnexpectedOff

_LOGGER = logging.getLogger(__name__)
MODE = {"off": 1, "heat": 2, "cool": 3, "auto": 4, "heat_cool": 5, "fan_only": 6, "dry": 7}
PATH = {"normal": 1, "shutdown_handoff": 2, "recovery": 3}
REASON = {"worker_command": 1, "terminal_preset": 2, "ownership_cleanup": 3, "integration_repair": 4, "worker_reconciliation": 5}
OUTCOME = {"invoked": 1, "returned": 2, "failed": 3, "cancelled_uncertain": 4}
FEEDBACK = {"unattributed": 1, "matching_service_context": 2, "consistent_only": 3}
COMMAND_FIELDS = ("mode_code", "path_code", "reason_code", "outcome_code", "dispatched_at", "finished_at")
FIELDS = (*(f"command_{key}" for key in COMMAND_FIELDS),
          *(f"handoff_{key}" for key in COMMAND_FIELDS if key not in ("path_code", "reason_code")),
          "feedback_mode_code", "feedback_at", "feedback_attribution_code",
          "handoff_feedback_mode_code", "handoff_feedback_at", "handoff_feedback_attribution_code")


def _now():
    return datetime.now(timezone.utc).timestamp()


def _valid(field, value):
    if field.endswith("_at"):
        return type(value) in (int, float) and isfinite(value) and value > 0
    table = next((table for suffix, table in (("attribution_code", FEEDBACK), ("mode_code", MODE),
        ("path_code", PATH), ("reason_code", REASON), ("outcome_code", OUTCOME)) if field.endswith(suffix)), {})
    return type(value) is int and value in table.values()


class ModeDiagnostics:
    def __init__(self, hass, entry_id, heads, off_eligible=None):
        self.hass = hass
        self.heads = frozenset(heads)
        self.store = Store(hass, 1, f"house_climate.mode_diagnostics.{entry_id}", atomic_writes=True)
        self.rows = {}
        self.pending = {}
        self.latest_dispatch = {}
        self.seen = set()
        self.unsubscribers = ()
        self.unexpected_off = (UnexpectedOff(self.heads, off_eligible, _now)
                               if off_eligible is not None else None)

    async def async_load(self):
        try:
            saved = await self.store.async_load()
        except Exception:
            _LOGGER.warning("Home Climate mode diagnostics could not be loaded", exc_info=True)
            return
        if isinstance(saved, dict):
            self.rows = {entity: {key: value for key, value in row.items()
                                  if key in FIELDS and _valid(key, value)}
                         for entity, row in saved.items() if entity in self.heads and isinstance(row, dict)}
        # A resumed runtime cannot infer an outcome or causal link for a
        # previously invoked service whose task did not complete.
        for row in self.rows.values():
            for prefix in ("command", "handoff"):
                if row.get(f"{prefix}_outcome_code") == OUTCOME["invoked"]:
                    row[f"{prefix}_outcome_code"] = OUTCOME["cancelled_uncertain"]

    @callback
    def start(self):
        if self.heads and not self.unsubscribers:
            self.unsubscribers = (async_track_state_change_event(self.hass, self.heads, self._reported),
                                  async_track_state_report_event(self.hass, self.heads, self._reported))

    @callback
    def close(self):
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers = ()
        self.pending.clear()

    def _save(self):
        # HA's Store flushes delayed writes in the final-write phase. Never
        # await storage or promote its outcome into actuator safety decisions.
        try:
            self.store.async_delay_save(lambda: {entity: dict(row) for entity, row in self.rows.items()}, 2)
        except Exception:
            _LOGGER.debug("Home Climate mode diagnostics write not scheduled", exc_info=True)

    def values(self, entity):
        return self.rows.get(entity, {})

    @callback
    def _reported(self, event):
        try:
            state = event.data.get("new_state")
            if self.unexpected_off is not None:
                try:
                    if state is not None and self.hass.states.get(state.entity_id) is state:
                        self.unexpected_off.report(
                            state, event.data.get("last_reported", state.last_reported), event.context)
                    elif state is None:
                        self.unexpected_off.reset(event.data.get("entity_id"))
                except Exception:
                    _LOGGER.debug("Home Climate unexpected Off observation failed", exc_info=True)
            if (state is None or state.entity_id not in self.heads or state.state not in MODE
                    or state.attributes.get("restored") or state.attributes.get("assumed_state")):
                return
            instant, stamp = _now(), state.last_reported.timestamp()
            row = self.rows.setdefault(state.entity_id, {})
            if not 0 <= instant - stamp <= 180 or stamp <= row.get("feedback_at", 0):
                return
            mode = MODE[state.state]
            pending = self.pending.get(state.entity_id)
            attribution = FEEDBACK["unattributed"]
            handoff_match = False
            # Only correlate the first qualifying report for the latest
            # active request. Later unrelated cache writes preserve this
            # diagnostic rather than converting it into a new acknowledgement.
            if state.context.user_id is not None and (not pending or state.context.id != pending[2]):
                self.pending.pop(state.entity_id, None)
            elif pending and pending[0] < stamp <= pending[0] + 180 and mode == pending[1]:
                attribution = FEEDBACK["matching_service_context" if state.context.id == pending[2]
                                       else "consistent_only"]
                self.pending.pop(state.entity_id)
                handoff_match = pending[3] == "shutdown_handoff"
            elif state.entity_id in self.seen and mode == row.get("feedback_mode_code"):
                return
            else:
                self.pending.pop(state.entity_id, None)
            self.seen.add(state.entity_id)
            row.update(feedback_mode_code=mode, feedback_at=stamp, feedback_attribution_code=attribution)
            if handoff_match:
                row.update(handoff_feedback_mode_code=mode, handoff_feedback_at=stamp,
                           handoff_feedback_attribution_code=attribution)
            self._save()
        except Exception:
            _LOGGER.debug("Home Climate mode feedback observation failed", exc_info=True)

    async def call(self, domain, service, data, *, path, reason, context=None):
        """Invoke the original service unchanged; diagnostic failures are isolated."""
        entity = data.get("entity_id")
        mode = data.get("hvac_mode")
        observed = (domain == "climate" and service in ("set_temperature", "set_hvac_mode")
                    and isinstance(entity, str) and entity in self.heads and isinstance(mode, str)
                    and mode in MODE and path in PATH and reason in REASON)
        record = None
        if observed:
            try:
                context = context or Context()
                now = _now()
                if self.unexpected_off is not None:
                    self.unexpected_off.command(entity, now, context.id)
                fields = dict(mode_code=MODE[mode], path_code=PATH[path], reason_code=REASON[reason],
                              outcome_code=OUTCOME["invoked"], dispatched_at=now)
                row = self.rows.setdefault(entity, {})
                prefixes = ("command", "handoff") if path == "shutdown_handoff" else ("command",)
                for prefix in prefixes:
                    row.pop(f"{prefix}_finished_at", None)
                    if prefix == "handoff":
                        for key in ("mode_code", "at", "attribution_code"):
                            row.pop(f"handoff_feedback_{key}", None)
                    row.update({f"{prefix}_{key}": value for key, value in fields.items()
                                if f"{prefix}_{key}" in FIELDS})
                token = object()
                for prefix in prefixes:
                    self.latest_dispatch[entity, prefix] = token
                record = row, token, prefixes
                self.pending[entity] = (now, MODE[mode], context.id, path)
                self._save()
            except Exception:
                _LOGGER.debug("Home Climate mode command observation failed", exc_info=True)
        outcome = "cancelled_uncertain"
        try:
            result = await self.hass.services.async_call(domain, service, data, blocking=True, context=context)
            outcome = "returned"
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            if record:
                row, token, prefixes = record
                for prefix in prefixes:
                    if self.latest_dispatch.get((entity, prefix)) is token:
                        row[f"{prefix}_outcome_code"] = OUTCOME[outcome]
                        row[f"{prefix}_finished_at"] = _now()
                self._save()
