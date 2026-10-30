"""Read-only, debounced room window evidence; never changes temperature rules."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time


OPEN_DEBOUNCE_SECONDS = 120
CLOSE_DEBOUNCE_SECONDS = 120


def contact_state(state):
    """HA contacts report on change, so an unchanged timestamp is not stale."""
    if not isinstance(state, Mapping):
        return "unknown", "missing"
    attrs = state.get("attributes")
    if isinstance(attrs, Mapping) and attrs.get("restored") is True:
        return "unknown", "restored"
    if isinstance(attrs, Mapping) and attrs.get("assumed_state") is True:
        return "unknown", "assumed"
    raw = state.get("state")
    if raw == "on":
        return "open", None
    if raw == "off":
        return "closed", None
    return "unknown", raw if raw in ("unknown", "unavailable") else "missing or invalid"


def contact_inputs(config, states):
    """Only material contact state participates in decision-input fencing."""
    return [(name, entity, *contact_state(states.get(entity)), contact_changed(states.get(entity)))
            for name, room in config.zones.items() for entity in room.windows]


def contact_changed(state):
    """A changed-state marker detects close/reopen events between snapshots."""
    if not isinstance(state, Mapping):
        return None
    raw = state.get("last_changed")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


@dataclass
class RoomWindow:
    state: str = "unknown"
    last_confirmed_state: str | None = None
    candidate: str | None = None
    since: datetime | None = None
    since_tick: float | None = None
    last_tick: float | None = None
    observed: str | None = None
    contacts: dict = field(default_factory=dict)


class WindowContacts:
    """Track known continuity, independently of temperature or control demand.

    One known open contact proves an opening even if another is unknown. A room
    is closed only when every configured contact reports closed. Unknown data
    breaks pending confirmation and remains explicitly unknown; it is never a
    fabricated closed report. Timers begin at observation, not at an old restored
    entity timestamp.
    """

    def __init__(self, config, clock=None):
        self.config = config
        self.clock = clock or time.monotonic
        self.rooms = {name: RoomWindow() for name, room in config.zones.items() if room.windows}

    def observe(self, states, now, *, monotonic_now=None):
        tick = self.clock() if monotonic_now is None else monotonic_now
        result = {}
        for name, saved in self.rooms.items():
            contacts, issues, markers = {}, {}, {}
            for entity in self.config.zones[name].windows:
                state = states.get(entity)
                value, issue = contact_state(state)
                contacts[entity] = value
                markers[entity] = (value, contact_changed(state))
                if issue:
                    issues[entity] = issue
            observed = ("open" if "open" in contacts.values() else
                        "closed" if all(value == "closed" for value in contacts.values()) else "unknown")
            backwards = saved.last_tick is not None and tick < saved.last_tick
            continuous = (any(value == "open" and saved.contacts.get(entity) == markers[entity]
                              for entity, value in contacts.items()) if observed == "open" else
                          bool(saved.contacts) and saved.contacts == markers)
            missed_transition = observed == saved.observed and not continuous
            saved.observed, saved.contacts, saved.last_tick = observed, markers, tick
            if observed == "unknown":
                saved.state = "unknown"
                saved.candidate = saved.since = saved.since_tick = None
            elif observed == saved.state:
                if missed_transition:
                    # A close/reopen ending in the same cached state cannot
                    # inherit continuity from the earlier opening episode.
                    saved.state = "unknown"
                    saved.candidate, saved.since, saved.since_tick = observed, now, tick
                else:
                    saved.candidate = saved.since = saved.since_tick = None
            else:
                if saved.candidate != observed or backwards or missed_transition:
                    saved.candidate, saved.since, saved.since_tick = observed, now, tick
                duration = OPEN_DEBOUNCE_SECONDS if observed == "open" else CLOSE_DEBOUNCE_SECONDS
                if tick - saved.since_tick >= duration:
                    saved.state = saved.last_confirmed_state = observed
                    saved.candidate = saved.since = saved.since_tick = None
            reason = (f"Confirming {saved.candidate} window state" if saved.candidate else
                      "Window state is unknown" if saved.state == "unknown" else
                      f"Window confirmed {saved.state}")
            result[name] = {
                "state": saved.state, "observed_state": observed,
                "pending_state": saved.candidate,
                "pending_since": saved.since.isoformat() if saved.since is not None else None,
                "last_confirmed_state": saved.last_confirmed_state,
                "reason": reason, "contacts": contacts, "contact_issues": issues,
            }
        return result
