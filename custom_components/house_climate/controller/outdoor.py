"""Read explicit current outdoor sources; no discovery, forecasts, or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .sensors import to_f

OUTDOOR_MAX_AGE_SECONDS = 1800
OUTDOOR_MIN_F = -100
OUTDOOR_MAX_F = 150


@dataclass(frozen=True)
class OutdoorReading:
    temperature_f: float | None = None
    sources: int = 0
    status: str = "unknown"
    warnings: tuple[str, ...] = ()
    next_expiry: float | None = None
    source_timestamps: tuple[tuple[str, float], ...] = ()


def _current(probe, state, now):
    if state.get("state") in (None, "unknown", "unavailable"):
        raise ValueError("unavailable")
    attrs = state.get("attributes") or {}
    if attrs.get("restored") is True:
        raise ValueError("restored reading")
    stamp = state.get("last_reported") or state.get("last_updated")
    if not isinstance(stamp, str):
        raise ValueError("missing timestamp")
    observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    age = (now - observed).total_seconds()
    if age < 0:
        raise ValueError("future reading")
    if age >= OUTDOOR_MAX_AGE_SECONDS:
        raise ValueError("stale reading")
    weather = probe.entity_id.startswith("weather.")
    attribute = "temperature" if weather else probe.attribute
    raw = attrs.get(attribute) if attribute else state.get("state")
    if isinstance(raw, bool):
        raise ValueError("invalid numeric temperature")
    unit = probe.unit or attrs.get("temperature_unit" if weather else "unit_of_measurement")
    temperature = to_f(float(raw), unit)
    if not math.isfinite(temperature) or not OUTDOOR_MIN_F <= temperature <= OUTDOOR_MAX_F:
        raise ValueError("implausible temperature")
    return temperature, observed.timestamp()


def read_outdoor(config, states, now):
    """Use the coldest valid independent source and expose its validity horizon.

    Aliases share their configured physical source identity and count once. A
    failed alias does not discard another valid reading from that same source.
    No temperature is inferred when every configured source is invalid.
    """
    warnings, valid, stamps = [], {}, []
    if now.tzinfo is None:
        return OutdoorReading(warnings=("Outdoor evaluation requires a timezone-aware timestamp",))
    now = now.astimezone(timezone.utc)
    probes = config.outdoor_probes
    for probe in probes:
        try:
            temperature, observed = _current(probe, states.get(probe.entity_id, {}), now)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            warnings.append(f"Outdoor {probe.entity_id}: {exc}")
            continue
        valid.setdefault(probe.source, []).append((temperature, observed))
        stamps.append(observed)
    if not valid:
        if not probes:
            warnings.append("No outdoor temperature sources configured")
        return OutdoorReading(warnings=tuple(warnings))
    if len(valid) == 1:
        warnings.append("Outdoor temperature has only one independent source")
    return OutdoorReading(
        temperature_f=min(temperature for values in valid.values() for temperature, _stamp in values),
        sources=len(valid), status="known", warnings=tuple(warnings),
        next_expiry=min(stamps) + OUTDOOR_MAX_AGE_SECONDS,
        source_timestamps=tuple(sorted((source, max(stamp for _temp, stamp in values))
                                      for source, values in valid.items())),
    )
