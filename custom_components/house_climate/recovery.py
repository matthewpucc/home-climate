"""Reload a failed heat-pump integration without changing actuator state.

The executor remains responsible for fresh off acknowledgements and shared-system
direction changes. A config-entry reload never counts as either acknowledgement.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

FAILURE_SECONDS = 120
RETRY_SECONDS = (15 * 60, 30 * 60, 60 * 60)
MAX_ATTEMPTS = 3


class RecoveryManager:
    """Rate-limit native reloads of integrations owning failed heat-pump heads."""

    def __init__(self, hass, runtime, *, clock=time.monotonic):
        self.hass = hass
        self.runtime = runtime
        self._clock = clock
        self._first_seen = {}
        self._attempts = {}
        self._next_retry = {}
        self._last_result = {}
        self._details = {}
        self._summary = "Idle"
        self._task = None
        self._active_entry = None
        self._closed = False

    @property
    def diagnostics(self):
        return {
            "reload_status": self._summary,
            "reload_details": {key: dict(value) for key, value in self._details.items()},
        }

    def _enabled(self):
        if not self.runtime.live or self.runtime.stop_event.is_set():
            return False
        if self.runtime.controls is not None:
            return self.runtime.controls.enabled
        enabled = self.hass.states.get(self.runtime.config.entity("enabled"))
        return enabled is not None and enabled.state == "on"

    def _notify(self):
        self.runtime._async_recovery_changed()

    def _retry_timestamp(self, entry_id, now):
        remaining = max(0, self._next_retry.get(entry_id, now) - now)
        if not remaining:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()

    @callback
    def observe(self, status):
        """Observe an executor report on HA's loop; enqueue at most one reload."""
        if self._closed:
            return
        if not self._enabled():
            self._first_seen.clear()
            self._details = {}
            self._summary = "Shadow mode" if not self.runtime.live else "Disabled"
            return
        now = self._clock()
        heads = set(self.runtime.config.heads.values())
        recovery = status.get("recovery_details", {})
        failed = {
            entity: detail
            for entity, detail in recovery.items()
            if entity in heads and detail.get("kind") == "recovery"
        }
        registry = er.async_get(self.hass)
        # A normal live report without a recovery issue confirms the incident
        # ended. A stopped/fault report omitting plan data is not such evidence.
        if status.get("event") == "live":
            for entity in set(self._first_seen) - failed.keys():
                state = self.hass.states.get(entity)
                if (state is not None and state.state not in ("unknown", "unavailable")
                        and state.attributes.get("hvac_modes")):
                    self._first_seen.pop(entity, None)
        for entity in failed:
            self._first_seen.setdefault(entity, now)

        groups = {}
        details = {}
        for entity in failed:
            item = registry.async_get(entity)
            entry_id = item.config_entry_id if item is not None else None
            entry = (self.hass.config_entries.async_get_entry(entry_id)
                     if entry_id and self.hass.config_entries is not None else None)
            if entry is None or entry.domain == "house_climate" or entry.disabled_by:
                details[entity] = {
                    "entities": [entity], "affected_heads": [], "attempts": 0,
                    "integration": None, "title": None, "next_retry_at": None,
                    "last_result": None,
                    "reason": "No reloadable owning integration",
                }
                continue
            groups.setdefault(entry_id, (entry, []))[1].append(entity)

        # Successful recovery resets the incident's attempt budget but retains
        # the previous reload's cooldown, including across brief Wi-Fi flaps.
        if status.get("event") == "live":
            outstanding_entries = {
                item.config_entry_id for entity in self._first_seen
                if (item := registry.async_get(entity)) is not None
            }
            for entry_id in set(self._attempts) - outstanding_entries:
                self._attempts.pop(entry_id, None)

        scheduled = None
        for entry_id, (entry, entities) in groups.items():
            affected = sorted(
                item.entity_id for item in er.async_entries_for_config_entry(registry, entry_id)
                if item.entity_id in heads
            )
            age = max(now - self._first_seen[entity] for entity in entities)
            attempts = self._attempts.get(entry_id, 0)
            detail = {
                "integration": entry.domain, "title": entry.title,
                "entities": sorted(entities), "affected_heads": affected,
                "attempts": attempts, "last_result": self._last_result.get(entry_id),
                "next_retry_at": self._retry_timestamp(entry_id, now),
            }
            if self._active_entry == entry_id:
                reason = "Reloading owning integration; awaiting device reports"
            elif age < FAILURE_SECONDS:
                reason = f"Waiting for sustained failure: {max(1, int(FAILURE_SECONDS - age))}s"
            elif entry.state is not ConfigEntryState.LOADED:
                reason = f"Owning integration is {entry.state.value}; HA manages setup"
            elif attempts >= MAX_ATTEMPTS:
                reason = "Automatic reload paused after three attempts; check owning integration"
            elif now < self._next_retry.get(entry_id, now):
                reason = "Waiting for integration reload cooldown"
            elif self._task is not None and not self._task.done():
                reason = "Another integration reload is in progress"
            elif scheduled is not None:
                reason = "Another integration reload is queued"
            else:
                scheduled = entry_id
                self._attempts[entry_id] = attempts + 1
                self._next_retry[entry_id] = now + RETRY_SECONDS[min(attempts, len(RETRY_SECONDS) - 1)]
                detail["attempts"] = attempts + 1
                detail["next_retry_at"] = self._retry_timestamp(entry_id, now)
                reason = "Reloading owning integration; awaiting device reports"
            detail["reason"] = reason
            details[entry_id] = detail
        self._details = details
        self._summary = next(iter(details.values()))["reason"] if details else "Idle"
        if scheduled is not None:
            self._active_entry = scheduled
            self._task = self.hass.async_create_background_task(
                self._async_reload(scheduled), "house-climate-integration-recovery"
            )

    async def _async_reload(self, entry_id):
        try:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not self._enabled() or entry is None or entry.state is not ConfigEntryState.LOADED:
                self._last_result[entry_id] = "Reload cancelled before starting"
                return
            _LOGGER.warning(
                "Reloading %s (%s) after a sustained heat-pump communication failure; "
                "other devices attached to that integration may briefly become unavailable",
                entry.title, entry.domain,
            )
            result = await self.hass.config_entries.async_reload(entry_id)
            self._last_result[entry_id] = (
                "Reload completed; waiting for normal device reports" if result
                else "Reload failed; Home Assistant could not reload the integration"
            )
        except asyncio.CancelledError:
            self._last_result[entry_id] = "Reload cancelled while controller stopped"
            raise
        except Exception as exc:
            self._last_result[entry_id] = f"Reload failed: {exc}"
            _LOGGER.warning("Heat-pump integration reload failed: %s", exc)
        finally:
            self._active_entry = None
            detail = self._details.get(entry_id)
            if detail is not None:
                detail["last_result"] = self._last_result.get(entry_id)
                detail["reason"] = ("Automatic reload paused after three attempts; check owning integration"
                                    if self._attempts.get(entry_id, 0) >= MAX_ATTEMPTS
                                    else "Waiting for integration reload cooldown")
                self._summary = detail["reason"]
            if not self._closed:
                self._notify()

    async def async_close(self):
        """Stop automatic recovery before the controller's own shutdown."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._active_entry = None
        self._summary = "Stopped"
