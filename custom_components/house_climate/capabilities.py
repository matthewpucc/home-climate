"""Stable, identity-scoped evidence of normally available head capabilities."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import time

from homeassistant.helpers.storage import Store
from homeassistant.config_entries import ConfigEntryState

_LOGGER = logging.getLogger(__name__)
STABLE_SECONDS = 120
STABLE_REPORTS = 3
MANAGED_MODES = frozenset(("off", "heat", "cool"))
KNOWN_MODES = MANAGED_MODES | {"auto", "heat_cool", "dry", "fan_only"}


@dataclass
class Candidate:
    modes: frozenset[str]
    since: float
    marker: object
    last_fresh: float
    reports: int = 1


class CapabilityEvidence:
    """Learn a stable reported set, never fabricate Heat or downgrade a shrink.

    Current Heat/Cool/Off is deliberately not an identity context: those ordinary
    transitions must not erase an incident. Explicit presets/operation modes are
    contexts because adapters may legitimately change supported capabilities.
    """

    def __init__(self, hass, *, clock=time.monotonic):
        self.hass, self.clock = hass, clock
        self.baselines = {}
        self.candidates = {}
        self.store = None
        self.storage_warning = None
        self._save_task = None
        self._closed = False
        self._revision = 0
        self._active_candidates = {}

    async def async_load(self, entry_id):
        self.store = Store(self.hass, 1, f"house_climate.{entry_id}.capabilities")
        try:
            async with asyncio.timeout(5):
                data = await self.store.async_load()
            if not data:
                return
            rows = data.get("baselines")
            if not isinstance(rows, dict):
                raise ValueError("Invalid saved capability baselines")
            for key, modes in rows.items():
                if (isinstance(key, str) and len(key) == 64 and isinstance(modes, list)
                        and modes and all(isinstance(mode, str) and mode in KNOWN_MODES for mode in modes)):
                    self.baselines[key] = frozenset(modes)
        except Exception:
            self._storage_failed("load")

    def _storage_failed(self, operation):
        message = f"Could not {operation} capability baselines; using runtime observations"
        if self.storage_warning != message:
            _LOGGER.warning(message)
        self.storage_warning = message

    @staticmethod
    def identity(item, owner, state):
        if item is None or owner is None:
            return None
        attrs = state.attributes
        context = [attrs.get("preset_mode"), attrs.get("operation_mode")]
        if any(value is not None and not isinstance(value, str) for value in context):
            return None
        identity = [owner.entry_id, owner.domain, item.id, item.unique_id, item.platform, *context]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    def observe(self, item, owner, state):
        if (state is None or state.state in ("unknown", "unavailable")
                or owner is None or owner.state is not ConfigEntryState.LOADED or owner.disabled_by
                or item is None or item.disabled_by or item.config_entry_id != owner.entry_id
                or state.attributes.get("restored") is True):
            self._break_candidate(item)
            return None
        raw = state.attributes.get("hvac_modes")
        if (not isinstance(raw, (list, tuple)) or not raw
                or any(not isinstance(mode, str) or mode not in KNOWN_MODES for mode in raw)):
            self._break_candidate(item)
            return None
        identity = self.identity(item, owner, state)
        if identity is None:
            self._break_candidate(item)
            return None
        previous = self._active_candidates.get(item.id)
        if previous != identity:
            if previous is not None:
                self.candidates.pop(previous, None)
            self._active_candidates[item.id] = identity
        modes = frozenset(raw)
        expected = self.baselines.get(identity)
        now, marker = self.clock(), state.last_reported
        candidate = self.candidates.get(identity)
        if candidate is None or candidate.modes != modes:
            candidate = self.candidates[identity] = Candidate(modes, now, marker, now)
        elif marker > candidate.marker:
            candidate.marker = marker
            candidate.reports += 1
            candidate.last_fresh = now
        stable = candidate.reports >= STABLE_REPORTS and candidate.last_fresh - candidate.since >= STABLE_SECONDS
        # An established set survives incomplete/shrunk reports. A stable
        # superset may add a genuinely newly advertised capability.
        if stable and (expected is None or (expected & MANAGED_MODES) <= modes and expected != modes):
            self.baselines[identity] = expected = modes
            self._schedule_save()
        missing = (expected - modes) if expected is not None else frozenset()
        return {
            "identity": identity, "expected": expected, "available": modes,
            "missing": missing & MANAGED_MODES,
            "stable": stable and expected is not None and (expected & MANAGED_MODES) <= modes,
            "context": {"preset_mode": state.attributes.get("preset_mode"),
                        "operation_mode": state.attributes.get("operation_mode")},
        }

    def _break_candidate(self, item):
        previous = self._active_candidates.pop(item.id, None) if item is not None else None
        if previous is not None:
            self.candidates.pop(previous, None)

    def _schedule_save(self):
        self._revision += 1
        if self.store is None or self._closed or (self._save_task and not self._save_task.done()):
            return
        self._save_task = self.hass.async_create_background_task(
            self._async_save(), "Home Climate capability baseline save")

    async def _async_save(self):
        try:
            while True:
                revision = self._revision
                async with asyncio.timeout(5):
                    await self.store.async_save({"baselines": {
                        key: sorted(modes) for key, modes in self.baselines.items()}})
                if revision == self._revision:
                    break
        except Exception:
            self._storage_failed("save")

    async def async_close(self):
        self._closed = True
        if self._save_task and not self._save_task.done():
            try:
                async with asyncio.timeout(2):
                    await asyncio.shield(self._save_task)
            except TimeoutError:
                self._save_task.cancel()
