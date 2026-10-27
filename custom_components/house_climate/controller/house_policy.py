"""Fixed operating rules with explicitly injected private ambient-device wiring.

Home Assistant owns temporary comfort holds. Private bindings select ambient
probes; equipment temperatures remain separate actuator diagnostics.
"""
from __future__ import annotations

import re

from .config import Config
from .bindings import BindingsError, HouseBindings, ROOM_ROLES

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


def house_configuration(previous=None, bindings=None):
    """Return fresh, validated fixed rules; previous tuning cannot override them."""
    bindings = _bindings(bindings)
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
            "sensors": [{"entity_id": entity, "source": entity,
                         "report_on_change": key == "entertainment"}
                        for entity in sensors],
            "profile": {"target_f": 72, "low_f": 71, "high_f": 73,
                        "half_band_f": 1, "priority": 1},
        }
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
             "half_band_f": 1, "priority": 1, "fan_percentage": 0},
        ],
        "fans": [{"entity_id": bindings.fans["primary"],
                  "strategy": "schedule"}],
    })
    rooms[identities["living"]]["profile"] = {
        "target_f": 68, "low_f": 67, "high_f": 69,
        "half_band_f": 1, "priority": .1,
    }
    rooms[identities["guest"]]["fans"] = [{
        "entity_id": bindings.fans["guest"], "strategy": "conditioning",
        "percentage": 60,
    }]
    # Both fans move the shared head's conditioned air into the staggered office.
    rooms[identities["office"]]["fans"] = [{
        "entity_id": entity, "strategy": "transfer", "percentage": 60,
        "source_room": identities["entertainment"],
    } for entity in (bindings.fans["office"], bindings.fans["entertainment"])]

    return Config.from_dict({
        "schema_version": 2, "timezone": HOUSE_TIMEZONE,
        "helper_prefix": "house_climate", "heads": dict(bindings.heads), "rooms": rooms,
        "low_f": 66, "high_f": 75,
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
        "auxiliary_fan_enabled": True, "heater_high_setpoint_f": 86,
        "heater_cutoff_f": 70, "heater_setpoint_confirmed": True,
    })
