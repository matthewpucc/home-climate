from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from datetime import datetime
from statistics import median

from .config import Config, Probe
from .temperature_guard import STOP_MARGIN_F


def to_f(value: float, unit: str) -> float:
    if unit in ("°F", "F"):
        return value
    if unit in ("°C", "C"):
        return value * 9 / 5 + 32
    raise ValueError(f"Unknown temperature unit: {unit!r}")


def from_f(value: float, unit: str) -> float:
    if unit in ("°F", "F"):
        return value
    if unit in ("°C", "C"):
        return (value - 32) * 5 / 9
    raise ValueError(f"Unknown temperature unit: {unit!r}")


def probe_value(probe: Probe, states: dict, now: datetime, stale_seconds: float,
                climate_unit: str = "°F") -> float:
    state = states.get(probe.entity_id, {})
    if state.get("state") in (None, "unavailable", "unknown"):
        raise ValueError("unavailable")
    stamp = state.get("last_reported") or state.get("last_updated")
    timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    if timestamp is None or timestamp.tzinfo is None:
        raise ValueError("missing timestamp")
    age = (now - timestamp).total_seconds()
    if age < -60 or (age > stale_seconds and not probe.report_on_change):
        raise ValueError("stale/future reading")
    attrs = state.get("attributes", {})
    raw = attrs.get(probe.attribute) if probe.attribute else state.get("state")
    unit = probe.unit or attrs.get("unit_of_measurement")
    if not unit and probe.attribute and probe.entity_id.startswith("climate."):
        unit = climate_unit
    value = to_f(float(raw), unit) + probe.offset_f
    if not math.isfinite(value) or not -40 <= value <= 140:
        raise ValueError("implausible temperature")
    return value


@dataclass(frozen=True)
class Reading:
    temperature: float
    low: float
    high: float
    confirmed_low: float
    confirmed_high: float
    slope_f_per_minute: float
    sources: int
    coast_after_mode: str | None = None
    coast_slope_f_per_minute: float | None = None
    post_stop_forecast: bool = False


def forecast_slope(reading, mode):
    """Start forecasts may use post-stop evidence; raw stop guards never do."""
    def value(key, default=None):
        return reading.get(key, default) if isinstance(reading, Mapping) else getattr(reading, key, default)
    opposite = "cool" if mode == "heat" else "heat"
    if value("post_stop_forecast", False) or value("coast_after_mode") == opposite:
        return float(value("coast_slope_f_per_minute") or 0)
    return float(value("slope_f_per_minute", 0))


def forecast_extreme(reading, mode, minutes):
    key = "confirmed_low" if mode == "heat" else "confirmed_high"
    extreme = float(reading.get(key) if isinstance(reading, Mapping) else getattr(reading, key))
    slope = forecast_slope(reading, mode)
    return extreme + (min(0, slope) if mode == "heat" else max(0, slope)) * minutes


@dataclass
class Coast:
    mode: str
    stopped: bool = False
    history: deque = field(default_factory=deque)


class Sensors:
    def __init__(self, config: Config):
        self.config = config
        self.history: dict[str, deque] = {}
        self.extremes: dict[tuple[str, str, str], datetime] = {}
        self.conditioned: dict[str, str] = {}
        self.coasts: dict[str, Coast] = {}

    def _forecast_context(self, rooms, states, now, targets, calls, head_requests):
        """Do not project completed conditioning as ten more minutes of drift.

        Only a room's own observed conditioning request can establish this
        context. An attached warm room that did not ask for Heat must retain
        its hot forecast even while its shared head heats another area.
        """
        for name, zone in self.config.zones.items():
            room = rooms.get(name)
            if room is None:
                coast = self.coasts.get(name)
                if coast:
                    coast.history.clear()
                continue
            observed = states.get(self.config.heads[zone.head], {})
            mode = observed.get("state") if observed.get("attributes", {}).get("hvac_modes") else None
            stops = targets.get(name)
            if stops is None:
                continue
            coast = self.coasts.get(name)
            if mode in ("heat", "cool"):
                requested = head_requests.get(zone.head) == mode
                reached = room.temperature >= stops[mode] if mode == "heat" else room.temperature <= stops[mode]
                if self.conditioned.get(name) not in (None, mode):
                    self.conditioned.pop(name, None)
                if requested and calls.get((name, mode)):
                    self.conditioned[name] = mode
                    # A real renewed request in the same direction starts a
                    # new conditioning interval rather than an old coast.
                    if coast and not reached:
                        self.coasts.pop(name, None)
                        coast = None
            previous = self.conditioned.get(name)
            if previous:
                reached = room.temperature >= stops[previous] if previous == "heat" else room.temperature <= stops[previous]
                future = forecast_extreme(room, previous, self.config.forecast_minutes)
                reserve_needed = (future <= self.config.low_f + STOP_MARGIN_F if previous == "heat"
                                  else future >= self.config.high_f - STOP_MARGIN_F)
                # Reaching a nominal target does not complete a call whose
                # same-direction reserve evidence still requires conditioning.
                # A subsequent unexplained Off must retain that recovery need.
                if reached and not reserve_needed and coast is None:
                    coast = self.coasts[name] = Coast(previous)
            if coast is None:
                continue
            slope = 0.0
            if mode == "off":
                coast.stopped = True
            if coast.stopped and mode in ("off", "heat", "cool"):
                # Keep this newer evidence across shared-head restarts. Such
                # a run can create a genuine trend for this room even when
                # only another attached area requested the conditioning.
                if coast.history and (now - coast.history[-1][0]).total_seconds() > self.config.sensor_stale_seconds:
                    coast.history.clear()
                if not coast.history or coast.history[-1][0] != now:
                    coast.history.append((now, room.temperature))
                while coast.history and (now - coast.history[0][0]).total_seconds() > 600:
                    coast.history.popleft()
                elapsed = (now - coast.history[0][0]).total_seconds()
                if elapsed >= 60:
                    slope = max(-.1, min(.1, (room.temperature - coast.history[0][1]) * 60 / elapsed))
            else:
                # Until Off is observed, or while head metadata is unknown,
                # there is no new trustworthy post-stop forecast evidence.
                coast.history.clear()
            rooms[name] = replace(room, coast_after_mode=coast.mode,
                                  coast_slope_f_per_minute=slope,
                                  post_stop_forecast=coast.stopped)

    def read(self, states: dict, now: datetime, climate_unit: str = "°F", *,
             targets=None, calls=None, head_requests=None) -> tuple[dict[str, Reading], list[str]]:
        result, warnings = {}, []
        for name, zone in self.config.zones.items():
            label = zone.name or name
            values = {}
            for probe in zone.probes:
                if probe.source in values:
                    continue  # Aliases are not independent corroboration.
                try:
                    values[probe.source] = probe_value(probe, states, now, self.config.sensor_stale_seconds, climate_unit)
                except (ValueError, TypeError, OverflowError) as exc:
                    warnings.append(f"{label}: {probe.entity_id}: {exc}")
            if not values:
                warnings.append(f"{label}: no trustworthy temperature; inhibit its head")
                self.history.pop(name, None)
                self.extremes = {k: v for k, v in self.extremes.items() if k[0] != name}
                continue
            center = median(values.values())
            if len(values) == 1:
                warnings.append(f"{label}: only one independent temperature source")
            if max(values.values()) - min(values.values()) > self.config.sensor_disagreement_f:
                warnings.append(f"{label}: probes disagree; preserve persistent hot/cold readings")
            low, high = center, center
            for source, value in values.items():
                for direction, extreme in (("low", value < center - .5), ("high", value > center + .5)):
                    key = (name, source, direction)
                    if not extreme:
                        self.extremes.pop(key, None)
                        continue
                    since = self.extremes.setdefault(key, now)
                    if (now - since).total_seconds() >= self.config.sensor_confirmation_seconds:
                        if direction == "low":
                            low = min(low, value)
                        else:
                            high = max(high, value)
            self.extremes = {k: v for k, v in self.extremes.items() if k[0] != name or k[1] in values}
            history = self.history.setdefault(name, deque())
            # Avoid manufacturing a trend across a disconnected interval.
            if history and (now - history[-1][0]).total_seconds() > self.config.sensor_stale_seconds:
                history.clear()
            history.append((now, center))
            while history and (now - history[0][0]).total_seconds() > 600:
                history.popleft()
            elapsed = (now - history[0][0]).total_seconds()
            slope = (center - history[0][1]) * 60 / elapsed if elapsed >= 60 else 0
            slope = max(-.1, min(.1, slope))
            result[name] = Reading(center, min(values.values()), max(values.values()), low, high, slope, len(values))
        if targets is not None and calls is not None:
            self._forecast_context(result, states, now, targets, calls, head_requests or {})
        return result, warnings
