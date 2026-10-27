"""Bounded evidence of head commands and reports, without recovery decisions."""
from __future__ import annotations

from collections import deque
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
