"""Scoped continuity evidence for a previously authorized window-relief start.

This module does not qualify an opening, choose temperature bounds, or authorize
conditioning. The planner must establish that policy permission first, using the
same snapshot passed here. An adapter can then reject changed or expired inputs
before dispatching that particular positive command. Nothing is sent to HA.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math

from .outdoor import read_outdoor
from .sensors import probe_value
from .windows import contact_changed, contact_state


def _finite_tick(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Authorization times must be finite numbers")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError("Authorization times must be finite numbers") from None
    if not math.isfinite(value):
        raise ValueError("Authorization times must be finite numbers")
    return value


def _probe_input(probe, states, now, stale_seconds, climate_unit):
    """Describe accepted inputs and why a redundant source was excluded.

    Report timestamps do not fingerprint unchanged readings, but their validity
    does. This follows the planner's accepted-source contract without rebuilding
    its fusion, outlier confirmation, or quorum decisions.
    """
    state = states.get(probe.entity_id)
    if not isinstance(state, Mapping):
        return (asdict(probe), "excluded", "missing" if state is None else "invalid state"), None
    attrs = state.get("attributes") or {}
    if not isinstance(attrs, Mapping):
        return (asdict(probe), "excluded", "invalid attributes"), None
    raw = attrs.get(probe.attribute) if probe.attribute else state.get("state")
    unit = probe.unit or attrs.get("unit_of_measurement") or climate_unit
    issue = None
    if attrs.get("restored") or attrs.get("assumed_state"):
        issue = ("untrusted", bool(attrs.get("restored")), bool(attrs.get("assumed_state")))
    elif isinstance(raw, bool):
        issue = "not numeric"
    if issue is None:
        try:
            reading = probe_value(probe, states, now, stale_seconds, climate_unit)
            remaining = None
            if not probe.report_on_change:
                reported = state.get("last_reported") or state.get("last_updated")
                stamp = datetime.fromisoformat(reported.replace("Z", "+00:00"))
                remaining = stale_seconds - (now - stamp).total_seconds()
            return (asdict(probe), "valid", reading, unit), remaining
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            issue = str(exc)
    # Invalidity changes matter; display names and timestamp-only noise do not.
    marker = (type(raw).__name__, repr(raw)[:80])
    return (asdict(probe), "excluded", issue, marker, unit), None


def _fingerprint(config, head, states, now, settings, climate_unit):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Authorization requires a timezone-aware observation time")
    if head not in config.heads or not isinstance(states, Mapping):
        raise ValueError("Authorization scope is not a configured head")
    rooms = sorted((name, room) for name, room in config.zones.items() if room.head == head)
    if not rooms:
        raise ValueError("Authorization head has no configured rooms")
    material = []
    for name, room in rooms:
        probes = []
        for probe in room.probes:
            evidence, _ = _probe_input(probe, states, now,
                                       config.sensor_stale_seconds, climate_unit)
            probes.append(evidence)
        if not any(evidence[1] == "valid" for evidence in probes):
            raise ValueError("Scoped room has no trusted current probe")
        contacts = []
        for entity in room.windows:
            state = states.get(entity)
            value, issue = contact_state(state)
            attrs = (state.get("attributes") or {}) if isinstance(state, Mapping) else {}
            if not isinstance(attrs, Mapping):
                value, issue = "unknown", "invalid attributes"
            elif attrs.get("assumed_state"):
                # This dispatch proof may not inherit an assumed open signal.
                # Leave the underlying contact debounce model unchanged.
                value, issue = "unknown", "assumed"
            contacts.append((entity, value, issue, contact_changed(state)))
        values = [entry[1] for entry in contacts]
        if values and "open" not in values and not all(value == "closed" for value in values):
            raise ValueError("Scoped window evidence is unknown")
        profile = asdict(settings.profile(room, now, config.timezone, name))
        for key in ("target_f", "half_band_f", "fan_percentage", "low_f", "high_f"):
            if profile[key] is not None:
                profile[key] = settings.value(profile[key])
        material.append((name, probes, contacts, name in settings.room_profiles, profile))
    protection = getattr(config, "protection", None)
    outdoor = None
    if protection is not None:
        outside = read_outdoor(config, states, now)
        outdoor = (asdict(protection), [asdict(probe) for probe in config.outdoor_probes],
                   outside.status, outside.temperature_f, outside.sources, outside.selected_role)
    payload = (config.heads[head], config.sensor_stale_seconds, material, outdoor)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class WindowAuthorization:
    """An immutable planning snapshot, not an equipment or comfort lease."""

    head: str
    entity: str
    mode: str
    fingerprint: str
    issued_at_monotonic: float
    expires_at_monotonic: float

    def __post_init__(self):
        if (not isinstance(self.head, str) or not self.head
                or not isinstance(self.entity, str) or not self.entity
                or self.mode not in ("heat", "cool")
                or not isinstance(self.fingerprint, str) or len(self.fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in self.fingerprint)):
            raise ValueError("Invalid window authorization metadata")
        start = _finite_tick(self.issued_at_monotonic)
        expiry = _finite_tick(self.expires_at_monotonic)
        if expiry <= start:
            raise ValueError("Window authorization must have a future expiry")
        object.__setattr__(self, "issued_at_monotonic", start)
        object.__setattr__(self, "expires_at_monotonic", expiry)


@dataclass(frozen=True)
class WindowAuthorizationResult:
    valid: bool
    reason: str
    applicable: bool = True

    def __bool__(self):
        return self.valid


def window_relief_required(config, head, plan, states):
    """Only cooling which relies on confirmed-open soft relief needs a ticket."""
    if plan.mode != "cool" or head not in plan.head_targets_f:
        return False
    context = plan.protection_context
    opened = set(context.get("confirmed_open_rooms", ()))
    if not any(name in opened for name, room in config.zones.items() if room.head == head):
        return False
    from .head_guard import head_constraint
    # Dependence is measured against ordinary comfort, including when the old
    # measured-deficit relief has also qualified. It cannot bypass this fence.
    without_relief = {**context, "confirmed_open_rooms": (), "window_effects": {}}
    ordinary = head_constraint(config, head, "cool", plan.rooms, plan.room_targets,
        protection=plan.head_protections.get(head), rearm=True, context=without_relief)
    return bool(ordinary and ordinary.get("kind") in {"comfort_limit", "comfort_rearm"})


def refresh_window_authorizations(plan, config, states, now, settings, *,
                                  monotonic_now, expires_at_monotonic, climate_unit="°F"):
    """Bind tickets only after the caller has freshly applied head eligibility.

    The old objects remain immutable for jobs already queued by a prior plan.
    A failed build leaves the head explicitly requiring authorization without a
    ticket, so an adapter must defer rather than use an unguarded fallback.
    """
    plan.window_authorizations = {}
    plan.window_authorization_heads = []
    tick = _finite_tick(monotonic_now)
    deadline = _finite_tick(expires_at_monotonic)
    contextual = plan.decision_valid_until_monotonic
    if contextual is not None:
        deadline = min(deadline, _finite_tick(contextual))
    outside = read_outdoor(config, states, now) if config.protection is not None else None
    if outside is not None and outside.next_expiry is not None:
        deadline = min(deadline, tick + outside.next_expiry - now.timestamp())
    for head in plan.head_targets_f:
        if not window_relief_required(config, head, plan, states):
            continue
        plan.window_authorization_heads.append(head)
        scoped_deadline = deadline
        for room in config.zones.values():
            if room.head != head:
                continue
            for probe in room.probes:
                evidence, remaining = _probe_input(probe, states, now,
                                                   config.sensor_stale_seconds, climate_unit)
                if evidence[1] == "valid" and remaining is not None:
                    scoped_deadline = min(scoped_deadline, tick + remaining)
        try:
            plan.window_authorizations[head] = build_window_authorization(
                config, head, plan.mode, states, now, settings,
                expires_at_monotonic=scoped_deadline, monotonic_now=tick,
                climate_unit=climate_unit)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            pass


def build_window_authorization(config, head, mode, states, now, settings, *,
                               expires_at_monotonic, monotonic_now, climate_unit="°F"):
    """Bind a permitted plan to its inputs; never infer policy permission here.

    The caller supplies expiry from its existing plan/service deadlines. No
    temperature floor, added grace period, or automatic renewal is introduced.
    """
    fingerprint = _fingerprint(config, head, states, now, settings, climate_unit)
    return WindowAuthorization(head, config.heads[head], mode, fingerprint,
                               monotonic_now, expires_at_monotonic)


def check_window_authorization(ticket, config, entity, mode, states, now, settings, *,
                               monotonic_now, climate_unit="°F"):
    """Fail closed on changed/invalid inputs; negative Off needs no ticket."""
    if mode == "off":
        return WindowAuthorizationResult(True, "Off does not require window authorization", False)
    if not isinstance(ticket, WindowAuthorization):
        return WindowAuthorizationResult(False, "Window authorization is missing")
    if (entity != ticket.entity or mode != ticket.mode
            or config.heads.get(ticket.head) != ticket.entity):
        return WindowAuthorizationResult(False, "Window authorization belongs to another head or mode")
    try:
        tick = _finite_tick(monotonic_now)
        if tick < ticket.issued_at_monotonic:
            return WindowAuthorizationResult(False, "Window authorization clock moved backwards")
        if tick >= ticket.expires_at_monotonic:
            return WindowAuthorizationResult(False, "Window authorization expired")
        current = _fingerprint(config, ticket.head, states, now, settings, climate_unit)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return WindowAuthorizationResult(False, "Window authorization inputs are invalid or no longer current")
    if current != ticket.fingerprint:
        return WindowAuthorizationResult(False, "Window authorization inputs changed; a fresh plan is required")
    return WindowAuthorizationResult(True, "Window authorization inputs remain current")
