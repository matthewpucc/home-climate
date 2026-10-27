"""Read explicit current outdoor sources; no discovery, forecasts, or I/O."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .sensors import to_f

OUTDOOR_MAX_AGE_SECONDS = 1800
WEATHER_MAX_AGE_SECONDS = 5400
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
    coverage_intervals: tuple[tuple[float, float], ...] = ()
    selected_role: str | None = None


def _available(state):
    if not isinstance(state, Mapping):
        raise ValueError("unavailable")
    if state.get("state") in (None, "unknown", "unavailable"):
        raise ValueError("unavailable")
    attrs = state.get("attributes") or {}
    if not isinstance(attrs, Mapping):
        raise ValueError("invalid attributes")
    if attrs.get("restored") is True:
        raise ValueError("restored reading")
    return attrs


def _timestamp(stamp, now, maximum_age):
    if not isinstance(stamp, str):
        raise ValueError("missing timestamp")
    observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    age = (now - observed).total_seconds()
    if age < 0:
        raise ValueError("future reading")
    if age >= maximum_age:
        raise ValueError("stale reading")
    return observed.timestamp()


def _current(probe, state, states, now):
    attrs = _available(state)
    weather = probe.entity_id.startswith("weather.")
    maximum_age = WEATHER_MAX_AGE_SECONDS if weather else OUTDOOR_MAX_AGE_SECONDS
    report = _timestamp(state.get("last_reported") or state.get("last_updated"), now, maximum_age)
    coverage_start, expiry = report, report + maximum_age
    observation_entity = getattr(probe, "observation_time_entity", None)
    if observation_entity is not None:
        observation_state = states.get(observation_entity, {})
        try:
            _available(observation_state)
            observation = _timestamp(observation_state.get("state"), now, maximum_age)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"observation time {observation_entity}: {exc}") from None
        coverage_start = max(report, observation)
        expiry = min(expiry, observation + maximum_age)
    attribute = "temperature" if weather else probe.attribute
    raw = attrs.get(attribute) if attribute else state.get("state")
    if isinstance(raw, bool):
        raise ValueError("invalid numeric temperature")
    unit = probe.unit or attrs.get("temperature_unit" if weather else "unit_of_measurement")
    temperature = to_f(float(raw), unit)
    if not math.isfinite(temperature) or not OUTDOOR_MIN_F <= temperature <= OUTDOOR_MAX_F:
        raise ValueError("implausible temperature")
    return temperature, report, coverage_start, expiry


def read_outdoor(config, states, now):
    """Prefer valid primary inputs; otherwise use valid fallback inputs.

    Unmarked existing inputs remain fallbacks, preserving coldest-valid fusion
    unless a primary is explicitly configured. Each selected tier uses its
    coldest valid value. A weather input's bounded report lifetime is distinct
    from physical observation age, checked only when explicitly bound.
    Aliases share their configured physical source identity and count once. A
    failed alias does not discard another valid reading from that same source.
    No temperature is inferred when every configured source is invalid.
    """
    warnings, tiers = [], {"primary": {}, "fallback": {}}
    if now.tzinfo is None:
        return OutdoorReading(warnings=("Outdoor evaluation requires a timezone-aware timestamp",))
    now = now.astimezone(timezone.utc)
    probes = config.outdoor_probes
    for probe in probes:
        try:
            current = _current(probe, states.get(probe.entity_id, {}), states, now)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            warnings.append(f"Outdoor {probe.entity_id}: {exc}")
            continue
        tiers[getattr(probe, "role", "fallback")].setdefault(probe.source, []).append(current)
    selected_role = "primary" if tiers["primary"] else "fallback"
    valid = tiers[selected_role]
    if not valid:
        if not probes:
            warnings.append("No outdoor temperature sources configured")
        return OutdoorReading(warnings=tuple(warnings))
    if len(valid) == 1:
        warnings.append("Outdoor temperature has only one independent source")
    values = [value for aliases in valid.values() for value in aliases]
    return OutdoorReading(
        temperature_f=min(value[0] for value in values),
        sources=len(valid), status="known", warnings=tuple(warnings),
        next_expiry=min(value[3] for value in values),
        source_timestamps=tuple(sorted((source, max(value[1] for value in aliases))
                                      for source, aliases in valid.items())),
        coverage_intervals=tuple(sorted((value[2], value[3]) for value in values)),
        selected_role=selected_role,
    )
