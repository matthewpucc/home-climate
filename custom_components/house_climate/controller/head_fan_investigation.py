"""Read-only fan investigation evidence. Never generates actuator requests.

HA fan_mode is a cached report, not proof of fan RPM or command acceptance.
The optional trial adapter labels cached corroboration unverified; no physical
poll provenance is inferred.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time
import math
from zoneinfo import ZoneInfo


def experiment_window(now: datetime, timezone: str, *, trial_seconds: float = 0,
                      restoration_seconds: float = 300) -> dict:
    """Fail closed unless a complete bounded trial fits before local 22:00.

    This is an eligibility diagnostic, not an authorization. A future executor
    must recheck at dispatch and retire pending experimental work before cutoff.
    Ordinary control, Off and protection must never wait for this window.
    """
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Aware clock required")
        for value in (trial_seconds, restoration_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid duration")
        local = now.astimezone(ZoneInfo(timezone))
        cutoff = datetime.combine(local.date(), time(22), local.tzinfo)
        remaining = cutoff.timestamp() - now.timestamp()
        if not 9 <= local.hour < 22:
            reason = "sleep_window"
        elif remaining <= trial_seconds + restoration_seconds:
            reason = "insufficient_restoration_time"
        else:
            reason = "daytime_window"
        return {"eligible": reason == "daytime_window", "reason": reason,
                "timezone": timezone, "cutoff": cutoff.isoformat()}
    except (ValueError, TypeError, OverflowError, KeyError):
        return {"eligible": False, "reason": "invalid_clock_or_duration",
                "timezone": timezone, "cutoff": None}


def fan_evidence(state: dict) -> dict:
    """Expose only current advertised modes; never reuse historical support."""
    state = state if isinstance(state, Mapping) else {}
    attrs = state.get("attributes")
    attrs = attrs if isinstance(attrs, Mapping) else {}
    usable = (state.get("state") not in (None, "unknown", "unavailable")
              and not attrs.get("restored") and not attrs.get("assumed_state"))
    modes = attrs.get("fan_modes")
    valid = (isinstance(modes, (list, tuple)) and bool(modes)
             and all(isinstance(mode, str) and bool(mode.strip()) for mode in modes))
    supported = sorted(set(modes)) if usable and valid else []
    reported = attrs.get("fan_mode")
    return {"advertised_modes": supported,
            "reported_mode": reported if isinstance(reported, str) and reported in supported else None,
            "physical_confirmation": "unknown",
            "reason": "cached_ha_report" if supported else "capability_unavailable"}


def investigation_snapshot(config, plan, states: dict, now: datetime) -> dict:
    """Publish evidence grouped by actual configured rooms and shared heads.

    No candidate fan mode, learned parameter, dispatch or calibration is created.
    Missing sensors remain missing. Source evidence lives in plan.rooms and is
    not duplicated here (avoids recorder growth and double counting).
    """
    heads = {}
    for key, entity in config.heads.items():
        room_names = sorted(name for name, room in config.zones.items() if room.head == key)
        rooms = {}
        for name in room_names:
            reading = plan.rooms.get(name)
            rooms[name] = {"available": reading is not None,
                           "gradient_f": (reading["high"] - reading["low"]) if reading else None,
                           "independent_sources": reading["sources"] if reading else 0}
        heads[key] = {**fan_evidence(states.get(entity, {})), "entity_id": entity,
                      "rooms": rooms, "joint_evaluation_required": len(room_names) > 1}
    return {"stage": "observation_only", "live_authorized": False,
            "window": experiment_window(now, config.timezone), "heads": heads}
