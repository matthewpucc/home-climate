"""Editable operating rules with explicitly injected private device wiring.

Home Assistant owns temporary comfort holds. Private bindings select ambient
probes; equipment temperatures remain separate actuator diagnostics.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from copy import deepcopy

from .config import Config, outdoor_probe_dict
from .bindings import BindingsError, HouseBindings, ROOM_ROLES
from .protection import ProtectionTemperatures

HOUSE_POLICY = "house"
HOUSE_TIMEZONE = "America/Los_Angeles"

def _bindings(value):
    if not isinstance(value, HouseBindings):
        raise BindingsError("Load private device bindings before building the house policy")
    return value


def room_identities(previous=None, bindings=None):
    """Keep registered room identities when replacing the old room editor.

    Existing installations may have UUID room keys. Probe wiring is authoritative
    for matching; a physical head alone identifies only a single-room head.
    Names, schedules, thresholds and old equipment probes are never imported.
    """
    bindings = _bindings(bindings)
    identities = {key: key for key in sorted(ROOM_ROLES)}
    if not isinstance(previous, dict):
        return identities
    rooms = previous.get("rooms", {})
    heads = previous.get("heads", {})
    if not isinstance(rooms, dict) or not isinstance(heads, dict):
        return identities
    used = set()
    for canonical, sensors in bindings.rooms.items():
        sensors = {getattr(probe, "entity_id", probe) for probe in sensors}
        candidates = []
        for key, room in rooms.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key)
                    or not isinstance(room, dict)):
                continue
            selected = room.get("sensors", ())
            probes = {probe.get("entity_id") for probe in selected
                      if isinstance(probe, dict)} if isinstance(selected, (list, tuple)) else set()
            if probes.intersection(sensors):
                candidates.append(key)
        if not candidates and canonical in {"primary", "guest", "living"}:
            candidates = [key for key, room in rooms.items()
                          if isinstance(key, str) and re.fullmatch(r"[a-z0-9_]+", key)
                          and isinstance(room, dict) and isinstance(room.get("head"), str)
                          and heads.get(room["head"]) == bindings.heads[canonical]]
        if len(candidates) == 1 and candidates[0] not in used:
            identities[canonical] = candidates[0]
            used.add(candidates[0])
    # Never produce duplicate room keys from a partially matching old setup.
    if len(set(identities.values())) != len(identities):
        return {key: key for key in sorted(ROOM_ROLES)}
    return identities


def house_configuration(previous=None, bindings=None, protection=None, policy_settings=None):
    """Build reviewed defaults and explicit edits; never import obsolete tuning."""
    bindings = _bindings(bindings)
    protection = ProtectionTemperatures() if protection is None else protection
    if not isinstance(protection, ProtectionTemperatures):
        protection = ProtectionTemperatures.from_dict(protection)
    identities = room_identities(previous, bindings)
    previous_rooms = previous.get("rooms", {}) if isinstance(previous, dict) else {}
    if not isinstance(previous_rooms, dict):
        previous_rooms = {}
    rooms = {}
    labels = {
        "office": "Office", "entertainment": "Entertainment area",
        "guest": "Guest bedroom", "primary": "Primary bedroom",
        "living": "Living room",
    }
    for key, sensors in bindings.rooms.items():
        rooms[identities[key]] = {
            "name": labels[key],
            "head": "bonus" if key in {"office", "entertainment"} else key,
            "sensors": [(asdict(entity) if hasattr(entity, "entity_id") else
                         {"entity_id": entity, "source": entity,
                          "report_on_change": key == "entertainment"}) for entity in sensors],
            "profile": {"target_f": 72, "low_f": 71, "high_f": 73,
                        "half_band_f": 1, "priority": 1},
        }
        if key in bindings.windows:
            rooms[identities[key]]["windows"] = list(bindings.windows[key])
        if previous_rooms:
            old = previous_rooms.get(identities[key], {})
            if isinstance(old, dict) and isinstance(old.get("area_id"), str):
                rooms[identities[key]]["area_id"] = old["area_id"]

    rooms[identities["primary"]].update({
        "profile": {"target_f": 69, "low_f": 67, "high_f": 70,
                    "half_band_f": 1, "comfort_enabled": False,
                    "priority": 1, "fan_percentage": 0},
        "schedules": [
            {"name": "Sleep", "start": "22:00:00", "end": "05:00:00",
             "target_f": 68, "low_f": 67, "high_f": 69,
             "half_band_f": 1, "priority": 10, "fan_percentage": 60},
            {"name": "Morning", "start": "05:00:00", "end": "09:00:00",
             "target_f": 69, "low_f": 67, "high_f": 70,
             "half_band_f": 1, "priority": 1, "fan_percentage": 60},
        ],
        "fans": [{"entity_id": bindings.fans["primary"],
                  "strategy": "schedule"}],
    })
    def profile(target, low, high, *, comfort=True, priority=1):
        return {"target_f": target, "low_f": low, "high_f": high,
                "half_band_f": 1, "priority": priority, "comfort_enabled": comfort}

    def period(name, start, end, selected, days=None, **extra):
        return {"name": name, "start": start, "end": end, **selected,
                **({"weekdays": list(days)} if days is not None else {}), **extra}

    weekdays, weekend = tuple(range(5)), (5, 6)
    office_limits = profile(70, 67, 73, comfort=False)
    rooms[identities["office"]].update(
        profile=profile(71, 69, 73), schedules=[
            period("Workday comfort", "08:00", "18:00", profile(71, 70, 73, priority=2), weekdays),
            period("Workday overnight limits", "18:00", "08:00", office_limits, weekdays),
            period("Monday early limits", "00:00", "08:00", office_limits, (0,)),
            period("Weekend limits", "00:00", "00:00", office_limits, weekend, all_day=True),
        ])
    rooms[identities["entertainment"]].update(
        profile=profile(70, 68, 73, comfort=False), schedules=[
            period("Weekday comfort", "16:30", "02:00", profile(70, 69, 72, priority=2), weekdays),
            period("Weekend comfort", "09:00", "03:00", profile(70, 68, 73), weekend),
            period("Saturday morning limits", "02:00", "09:00", profile(70, 67, 74, comfort=False), (5,)),
            period("Sunday morning limits", "03:00", "09:00", profile(70, 67, 74, comfort=False), (6,)),
        ])
    guest_limits = profile(70, 68, 74, comfort=False)
    rooms[identities["guest"]].update(profile=guest_limits, schedules=[
        period("Weekday comfort", "17:00", "01:00", profile(70, 69, 73), weekdays),
        period("Weekday daytime limits", "09:00", "17:00", profile(70, 68, 73, comfort=False), (1, 2, 3, 4)),
        period("Monday daytime limits", "10:00", "17:00", profile(70, 68, 73, comfort=False), (0,)),
        period("Weekend comfort", "10:00", "22:00", profile(70, 69, 73), weekend),
        period("Weekend overnight limits", "22:00", "10:00", guest_limits, weekend),
        period("Saturday early limits", "01:00", "10:00", guest_limits, (5,)),
    ])
    cool_day, warm_day = profile(69, 68, 72), profile(71, 69, 73)
    rooms[identities["living"]].update(profile=cool_day,
        seasonal_profiles={"warm": warm_day}, schedules=[
            period("Daytime comfort", "07:00", "22:00", cool_day,
                   seasonal_profiles={"warm": warm_day}),
            period("Overnight limits", "22:00", "07:00", profile(69, 66, 75, comfort=False),
                   seasonal_profiles={"warm": profile(71, 68, 75, comfort=False)}),
        ])
    rooms[identities["guest"]]["fans"] = [{
        "entity_id": bindings.fans["guest"], "strategy": "conditioning",
        "percentage": 60,
    }]
    # Both fans move the shared head's conditioned air into the staggered office.
    rooms[identities["office"]]["fans"] = [{
        "entity_id": entity, "strategy": "transfer", "percentage": 60,
        "source_room": identities["entertainment"],
    } for entity in (bindings.fans["office"], bindings.fans["entertainment"])]

    data = {
        "schema_version": 2, "timezone": HOUSE_TIMEZONE,
        "helper_prefix": "house_climate", "heads": dict(bindings.heads), "rooms": rooms,
        "low_f": 66, "high_f": 75,
        "protection": protection.to_dict(),
        "outdoor_probes": [outdoor_probe_dict(probe) for probe in bindings.outdoor],
        "poll_seconds": 15, "decision_interval_seconds": 90,
        "head_minimum_run_seconds": 300, "head_minimum_off_seconds": 180,
        "head_activation_timeout_seconds": 120,
        "head_cool_min_f": {entity: 65 for entity in bindings.heads.values()},
        "head_heat_call_offset_f": 5, "head_cool_call_offset_f": 5,
        "sensor_stale_seconds": 900, "sensor_confirmation_seconds": 60,
        "sensor_disagreement_f": 3, "forecast_minutes": 10,
        "mode_hold_seconds": 1200, "switch_confirmation_seconds": 120,
        "off_settle_seconds": 60, "command_timeout_seconds": 45,
        "heartbeat_timeout_seconds": 300, "auxiliary_heartbeat_timeout_seconds": 120,
        "equipment_timing_confirmed": True,
        "heater_entity": bindings.heater, "auxiliary_room": identities["living"],
        "auxiliary_fan_enabled": True, "auxiliary_dynamic_stop": True, "heater_high_setpoint_f": 80,
        "heater_cutoff_f": 70, "heater_setpoint_confirmed": True,
        "auxiliary_policy": {}, "seasonal_policy": {"enabled": True},
        "purifiers": {"minimum_percentage": 20, "living_percentage": 100},
        "fallback": {"heat_low_f": 68, "cool_high_f": 73, "fan_percentage": 40},
    }
    if policy_settings is not None:
        if not isinstance(policy_settings, dict):
            raise ValueError("Policy settings must be an object")
        allowed = {"rooms", "auxiliary_policy", "seasonal_policy", "purifiers", "fallback",
                   "heartbeat_timeout_seconds", "auxiliary_heartbeat_timeout_seconds"}
        if set(policy_settings) - allowed:
            raise ValueError("Policy settings cannot replace device wiring or protection settings")
        for name, value in policy_settings.items():
            if name != "rooms":
                if name in {"auxiliary_policy", "seasonal_policy", "purifiers", "fallback"}:
                    if not isinstance(value, dict):
                        raise ValueError(f"{name} must be an object")
                    data[name] = {**data[name], **deepcopy(value)}
                else:
                    data[name] = value
        selected_rooms = policy_settings.get("rooms", {})
        if not isinstance(selected_rooms, dict) or set(selected_rooms) - set(identities):
            raise ValueError("Policy room edits must name configured house rooms")
        for name, edits in selected_rooms.items():
            if not isinstance(edits, dict) or set(edits) - {"profile", "schedules", "seasonal_profiles"}:
                raise ValueError("Room policy edits may change profiles and schedules only")
            for field, value in edits.items():
                if field == "profile":
                    if not isinstance(value, dict):
                        raise ValueError("Room profile must be an object")
                    rooms[identities[name]][field] = {**rooms[identities[name]][field], **deepcopy(value)}
                else:
                    rooms[identities[name]][field] = deepcopy(value)
    purifiers = data["purifiers"]
    for room in rooms.values():
        for fan in room.get("fans", ()):
            fan["minimum_percentage"] = purifiers["minimum_percentage"]
    if "living" in bindings.fans:
        rooms[identities["living"]]["fans"] = [{
            "entity_id": bindings.fans["living"], "strategy": "always",
            "percentage": purifiers["living_percentage"],
            "minimum_percentage": purifiers["living_percentage"],
        }]
    return Config.from_dict(data)


def editable_policy_settings(config, bindings):
    """Export only editable policy; physical entity wiring stays private and separate."""
    identities = room_identities(config.to_dict(), bindings)
    rooms = {}
    for canonical, identity in identities.items():
        selected = config.zones[identity]
        # These house profiles contain literal values, not legacy helper
        # references. Preserve their representation on an unchanged form save:
        # the generic export resolves numbers/clocks and would invalidate a
        # strict persisted comfort-hold fingerprint despite identical policy.
        schedules = []
        for period in selected.schedules:
            value = {"start": period.start, "end": period.end, "name": period.name,
                     **asdict(period.profile)}
            if period.weekdays != tuple(range(7)):
                value["weekdays"] = list(period.weekdays)
            if period.all_day:
                value["all_day"] = True
            if period.seasonal_profiles:
                value["seasonal_profiles"] = {
                    name: asdict(profile) for name, profile in period.seasonal_profiles.items()}
            schedules.append(value)
        rooms[canonical] = {"profile": asdict(selected.profile), "schedules": schedules}
        if selected.seasonal_profiles:
            rooms[canonical]["seasonal_profiles"] = {
                name: asdict(profile) for name, profile in selected.seasonal_profiles.items()}
    return {"rooms": rooms, "auxiliary_policy": asdict(config.auxiliary_policy),
            "purifiers": asdict(config.purifiers),
            "fallback": asdict(config.fallback), "seasonal_policy": asdict(config.seasonal_policy),
            "heartbeat_timeout_seconds": config.heartbeat_timeout_seconds,
            "auxiliary_heartbeat_timeout_seconds": config.auxiliary_heartbeat_timeout_seconds}
