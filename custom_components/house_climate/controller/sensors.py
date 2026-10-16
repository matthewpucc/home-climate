from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from .config import Config, Probe


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
    if age > stale_seconds or age < -60:
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


class Sensors:
    def __init__(self, config: Config):
        self.config = config
        self.history: dict[str, deque] = {}
        self.extremes: dict[tuple[str, str, str], datetime] = {}

    def read(self, states: dict, now: datetime, climate_unit: str = "°F") -> tuple[dict[str, Reading], list[str]]:
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
        return result, warnings
