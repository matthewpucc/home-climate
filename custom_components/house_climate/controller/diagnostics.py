"""Bounded evidence of head commands and reports, without recovery decisions."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import math

HEAD_TRANSITION_LIMIT = 16


class HeadTransitions:
    """Keep executor service receipts distinct from device acknowledgement.

    Reports do not prove physical compressor operation. Known modes are only
    evidence of capabilities observed during this executor's lifetime; losing
    one does not itself authorize an integration reload.
    """

    def __init__(self, entities):
        self.events = {entity: deque(maxlen=HEAD_TRANSITION_LIMIT) for entity in entities}
        self.known_modes = {entity: set() for entity in entities}
        self._reports = {}
        self._sequence = 0

    @staticmethod
    def _number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _modes(state):
        modes = (state.get("attributes") or {}).get("hvac_modes")
        return sorted({mode for mode in modes if isinstance(mode, str)}) if isinstance(modes, (list, tuple)) else []

    def observe_capabilities(self, entity, state):
        if entity in self.known_modes and state.get("state") not in (None, "unknown", "unavailable"):
            self.known_modes[entity].update(self._modes(state))

    def lost_modes(self, entity, missing):
        return sorted(self.known_modes.get(entity, set()).intersection(missing))

    def record(self, entity, event, state, **details):
        if entity not in self.events:
            return None
        attrs = state.get("attributes") or {}
        self._sequence += 1
        self.events[entity].append({
            "sequence": self._sequence,
            "event": event,
            "source": "executor",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "observed_mode": state.get("state"),
            "observed_target_temperature": self._number(attrs.get("temperature")),
            "observed_current_temperature": self._number(attrs.get("current_temperature")),
            "reported_at": str(state.get("last_reported") or state.get("last_updated")
                               or state.get("last_changed") or "") or None,
            "available_modes": self._modes(state),
            **details,
        })
        return self._sequence

    def report(self, entity, state, requested_mode):
        self.observe_capabilities(entity, state)
        attrs = state.get("attributes") or {}
        # Identical reports must not evict command evidence or churn the recorder.
        marker = (state.get("state"), self._number(attrs.get("temperature")),
                  attrs.get("hvac_action"), tuple(self._modes(state)),
                  (state.get("integration_repair") or {}).get("epoch"),
                  (state.get("integration_repair") or {}).get("held"))
        if self._reports.get(entity) != marker:
            self._reports[entity] = marker
            self.record(entity, "state_reported", state, requested_mode=requested_mode,
                        known_modes=sorted(self.known_modes.get(entity, set())),
                        integration_repair=dict(state.get("integration_repair") or {}))

    def snapshot(self):
        return {entity: deepcopy(list(events)) for entity, events in self.events.items() if events}


# Export only the executor's evidence fields, never arbitrary runtime/config data.
_HEAD_EVENT_FIELDS = frozenset({
    "sequence", "event", "source", "recorded_at", "observed_mode",
    "observed_target_temperature", "observed_current_temperature", "reported_at",
    "available_modes", "requested_mode", "known_modes", "command_id", "service",
    "error_type", "reason",
})
_HEAD_COMMAND_FIELDS = frozenset({
    "hvac_mode", "temperature", "target_temp_low", "target_temp_high", "preset_mode",
})


def bounded_head_history(status, heads):
    """Copy the current bounded executor evidence for configured heads only."""
    history = status.get("head_transition_history") if isinstance(status, Mapping) else None
    if not isinstance(history, Mapping):
        return {}
    result = {}
    for head in dict.fromkeys(heads):
        rows = history.get(head, ())
        if not isinstance(rows, (list, tuple)):
            continue
        selected = []
        for row in rows[-HEAD_TRANSITION_LIMIT:]:
            if not isinstance(row, dict):
                continue
            item = {key: deepcopy(value) for key, value in row.items()
                    if key in _HEAD_EVENT_FIELDS}
            if isinstance(row.get("data"), dict):
                item["data"] = {key: deepcopy(value) for key, value in row["data"].items()
                                if key in _HEAD_COMMAND_FIELDS}
            if isinstance(row.get("integration_repair"), dict):
                item["integration_repair"] = {
                    key: deepcopy(row["integration_repair"][key]) for key in ("epoch", "held")
                    if key in row["integration_repair"]}
            selected.append(item)
        if selected:
            result[head] = selected
    return result


def head_transition_summary(status, heads):
    """Small current-window summary, not a durable event counter or archive."""
    return {head: {"retained_event_count": len(rows),
                   "last_sequence": rows[-1].get("sequence"),
                   "last_event": rows[-1].get("event"),
                   "last_recorded_at": rows[-1].get("recorded_at")}
            for head, rows in bounded_head_history(status, heads).items()}
