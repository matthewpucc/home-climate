"""Shared-head eligibility: house protection, then soft comfort and restart room."""
from __future__ import annotations

from collections.abc import Mapping
import math

from .temperature_guard import STOP_MARGIN_F, opposite_limit_reached, projected_extreme

COMFORT_REARM_F = .5


def _value(reading, key, default=None):
    return reading.get(key, default) if isinstance(reading, Mapping) else getattr(reading, key, default)


def _names(config, head):
    return [name for name, room in config.zones.items() if room.head == head]


def protection_requested(config, reading, mode):
    """Positive calls use corroborated extremes and worsening-only forecasts."""
    slope = float(_value(reading, "slope_f_per_minute", 0))
    if mode == "cool":
        extreme = float(_value(reading, "confirmed_high"))
        future = extreme + max(0, slope) * config.forecast_minutes
        return max(extreme, future) >= config.high_f - STOP_MARGIN_F
    if mode == "heat":
        extreme = float(_value(reading, "confirmed_low"))
        future = extreme + min(0, slope) * config.forecast_minutes
        return min(extreme, future) <= config.low_f + STOP_MARGIN_F
    return False


def _release_target(config, mode, target):
    if (not target.get("comfort_enabled", True)
            or (mode == "cool" and float(target["high_f"]) >= config.high_f)
            or (mode == "heat" and float(target["low_f"]) <= config.low_f)):
        return config.high_f - 1.5 if mode == "cool" else config.low_f + 1.5
    desired = float(target["target_f"])
    return min(desired, config.high_f - 1.5) if mode == "cool" else max(desired, config.low_f + 1.5)


def active_protection(config, head, mode, rooms, room_targets, protection):
    """Revalidate a plan's origins using fresh readings, including executor maps."""
    if not protection or protection.get("mode") != mode:
        return None
    names = _names(config, head)
    origins = []
    for name in protection.get("rooms", ()):
        if name not in names or name not in rooms or name not in room_targets:
            continue
        reading, target = rooms[name], room_targets[name]
        try:
            temperature = float(_value(reading, "temperature"))
            release = _release_target(config, mode, target)
            if not math.isfinite(temperature) or not math.isfinite(release):
                continue
            needs = temperature > release if mode == "cool" else temperature < release
            if needs or protection_requested(config, reading, mode):
                origins.append(name)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not origins:
        return None
    labels = ", ".join(config.zones[name].name or name for name in origins)
    return {"mode": mode, "rooms": sorted(set(origins)),
            "reason": f"{mode.title()} for {labels} house protection; soft comfort limits may yield"}


def _detail(config, name, mode, kind, temperature, boundary, required, reason):
    return {"kind": kind, "room": name, "room_name": config.zones[name].name or name,
            "mode": mode, "temperature_f": temperature, "boundary_f": boundary,
            "required_f": required, "reason": reason}


def head_constraint(config, head, mode, rooms, room_targets, *, protection=None, rearm=False):
    """One continuation/start check; protection never overrides missing/raw limits."""
    if mode not in ("heat", "cool"):
        return None
    names = _names(config, head)
    for name in names:
        label = config.zones[name].name or name
        if name not in rooms or name not in room_targets:
            return _detail(config, name, mode, "missing", None, None, None,
                           f"{label}: temperature unavailable in an attached room")
        reading = rooms[name]
        try:
            if opposite_limit_reached(reading, mode, config.low_f, config.high_f):
                boundary = config.low_f + STOP_MARGIN_F if mode == "cool" else config.high_f - STOP_MARGIN_F
                extreme = float(_value(reading, "low" if mode == "cool" else "high"))
                detail = _detail(config, name, mode, "house_limit", extreme, boundary, None,
                                f"{label}: {mode} blocked by opposite temperature limit "
                                f"({extreme:g}°F; {boundary:g}°F house guard); airflow/capacity conflict")
                detail["projected_f"] = projected_extreme(reading, mode)
                return detail
        except (KeyError, TypeError, ValueError, OverflowError):
            return _detail(config, name, mode, "missing", None, None, None,
                           f"{label}: invalid temperature in an attached room")
    if active_protection(config, head, mode, rooms, room_targets, protection):
        return None
    # Legacy calls without deliberate thermostat bias keep their slow comfort
    # behavior. Raw house protections above remain unconditional.
    if getattr(config, f"head_{mode}_call_offset_f", 0) <= 0:
        return None
    for name in names:
        target = room_targets[name]
        if not target.get("comfort_enabled", True):
            continue
        label = config.zones[name].name or name
        try:
            temperature = float(_value(rooms[name], "temperature"))
            boundary = float(target["low_f" if mode == "cool" else "high_f"])
            required = boundary + COMFORT_REARM_F if mode == "cool" else boundary - COMFORT_REARM_F
            if not all(math.isfinite(value) for value in (temperature, boundary, required)):
                raise ValueError("Nonfinite comfort feedback")
        except (KeyError, TypeError, ValueError, OverflowError):
            return _detail(config, name, mode, "missing", None, None, None,
                           f"{label}: invalid comfort feedback")
        reached = temperature <= boundary if mode == "cool" else temperature >= boundary
        if reached:
            edge = "floor" if mode == "cool" else "ceiling"
            return _detail(config, name, mode, "comfort_limit", temperature, boundary, required,
                           f"{label} {temperature:.1f}°F reached its {boundary:g}°F comfort {edge}; "
                           f"{mode} stopped at opposite room comfort limit")
        ready = temperature >= required if mode == "cool" else temperature <= required
        if rearm and not ready:
            return _detail(config, name, mode, "comfort_rearm", temperature, boundary, required,
                           f"{label} {temperature:.1f}°F needs {required:g}°F before {mode} restarts")
    return None


class HeadGuard:
    """Keep cutoff/recovery state across ordinary comfort-plan refreshes."""

    def __init__(self, config):
        self.config = config
        self.cutoffs = set()
        self.protections = {}

    def observe(self, rooms, targets):
        for head in self.config.heads:
            names = _names(self.config, head)
            for mode in ("heat", "cool"):
                key = (head, mode)
                current = active_protection(self.config, head, mode, rooms, targets,
                                            self.protections.get(key))
                origins = set(current["rooms"] if current else ())
                for name in names:
                    if name in rooms and protection_requested(self.config, rooms[name], mode):
                        origins.add(name)
                proposed = {"mode": mode, "rooms": sorted(origins)}
                protection = active_protection(self.config, head, mode, rooms, targets, proposed)
                if protection:
                    self.protections[key] = protection
                else:
                    self.protections.pop(key, None)
                # Record soft-edge contact even if no current request exists:
                # the previous call can finish at target on this same tick.
                soft = head_constraint(self.config, head, mode, rooms, targets)
                if soft and soft["kind"] == "comfort_limit":
                    self.cutoffs.add(key)
                if key in self.cutoffs and not head_constraint(
                        self.config, head, mode, rooms, targets, rearm=True):
                    self.cutoffs.discard(key)

    def protection(self, head, mode, rooms, targets):
        return active_protection(self.config, head, mode, rooms, targets,
                                 self.protections.get((head, mode)))

    def constraint(self, head, mode, rooms, targets, states):
        current = states.get(self.config.heads[head], {}).get("state")
        return head_constraint(self.config, head, mode, rooms, targets,
                               protection=self.protection(head, mode, rooms, targets),
                               rearm=(head, mode) in self.cutoffs or current != mode)
