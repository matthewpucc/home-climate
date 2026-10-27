"""Generate an HA package (JSON is valid YAML) from the same probe mappings."""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from pathlib import Path

from .config import Config, NUMBERS, TIMES, TOGGLES, helper


def watchdog_signature(config: Config) -> str:
    mapping = {"watchdog_version": 2, "probes": [{key: value for key, value in asdict(p).items() if key != "report_on_change" or value}
                for p in config.zones["living"].probes],
               "stale": config.sensor_stale_seconds, "cutoff": config.heater_cutoff_f,
               "heater": config.heater_entity}
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:16]


def package(config: Config) -> dict:
    probes = [{"entity": p.entity_id, "attribute": p.attribute or "", "unit": p.unit or "",
               "offset": p.offset_f, "report_on_change": p.report_on_change} for p in config.zones["living"].probes]
    # Independently recomputed by HA, not a Python-reported temperature/health flag.
    safe = """{% set probes = PROBES %}
{% set ns = namespace(values=[]) %}
{% for p in probes %}
  {% set s = states[p.entity] %}
  {% if s is defined and s is not none and s.state not in [none, 'unavailable', 'unknown'] %}
    {% set reported = s.last_reported | default(s.last_updated, true) %}
    {% set stamp = as_timestamp(reported, none) %}
    {% set aware = reported is not none and ((reported.tzinfo is defined and reported.tzinfo is not none)
         or (reported is string and ('+' in reported[10:] or '-' in reported[10:] or reported.endswith('Z')))) %}
    {% set age = as_timestamp(now()) - stamp if stamp is not none else none %}
    {% set raw = state_attr(p.entity, p.attribute) if p.attribute else s.state %}
    {% set unit = p.unit or state_attr(p.entity, 'unit_of_measurement') %}
    {% if aware and age is not none and -60 <= age and (p.report_on_change or age <= STALE)
          and is_number(raw) and unit in ['°F', 'F', '°C', 'C'] %}
      {% set f = (raw | float * 9 / 5 + 32) if unit in ['°C', 'C'] else raw | float %}
      {% set f = f + p.offset %}
      {% if -40 <= f <= 140 %}{% set ns.values = ns.values + [f] %}{% endif %}
    {% endif %}
  {% endif %}
{% endfor %}
{{ ns.values | count > 0 and ns.values | max < CUTOFF }}"""
    safe = safe.replace("PROBES", json.dumps(probes, ensure_ascii=False)).replace("STALE", str(config.sensor_stale_seconds)).replace("CUTOFF", str(config.heater_cutoff_f))
    numbers = {}
    for name, (default, low, high, step) in NUMBERS.items():
        numbers[f"hp_{name}"] = {"name": "HP " + name.replace("_", " "), "min": low, "max": high, "step": step, "mode": "box"}
        # Deliberately no initial: HA restores helper edits across restarts.
        # A one-time initialization script below installs the supplied defaults.
    times = {f"hp_{name}": {"name": "HP " + name.replace("_", " "), "has_date": False, "has_time": True} for name in TIMES}
    times["hp_heartbeat"] = {"name": "HP controller heartbeat", "has_date": True, "has_time": True}
    defaults = []
    for name, (value, _, _, _) in NUMBERS.items():
        if name == "mode_hold_minutes":
            value = config.mode_hold_seconds / 60
        defaults.append({"action": "input_number.set_value", "target": {"entity_id": helper(name)}, "data": {"value": value}})
    for name, value in TIMES.items():
        defaults.append({"action": "input_datetime.set_datetime", "target": {"entity_id": helper(name, "input_datetime")}, "data": {"time": value}})
    for name, value in TOGGLES.items():
        defaults.append({"action": "input_boolean.turn_on" if value else "input_boolean.turn_off",
                         "target": {"entity_id": helper(name, "input_boolean")}})
    unsafe = """{% set age = as_timestamp(now()) - as_timestamp(states('input_datetime.hp_heartbeat'), 0) %}
{% set could_heat = states(HEATER) not in ['off', 'fan_only'] or state_attr(HEATER, 'hvac_action') == 'heating' %}
{{ not is_state('input_boolean.hp_enabled', 'on')
   or age < -60 or age > 120
   or (could_heat and not is_state('binary_sensor.hp_living_probe_safe', 'on')) }}"""
    unsafe = unsafe.replace("HEATER", json.dumps(config.heater_entity))
    return {
        "input_boolean": {
            "hp_enabled": {"name": "HP controller enabled", "initial": False},
            "hp_heater_owned": {"name": "HP auxiliary heater ownership"},
            **{f"hp_{name}": {"name": "HP " + name.replace("_", " ")} for name in TOGGLES},
        },
        "input_number": numbers,
        "input_datetime": times,
        "input_text": {"hp_status": {"name": "HP controller status", "max": 255}},
        "script": {"hp_initialize_defaults": {"alias": "HP initialize defaults (run once)", "sequence": defaults}},
        "template": [{"binary_sensor": [{"name": "HP living probe safe", "unique_id": "hp_living_probe_safe",
                                          "default_entity_id": "binary_sensor.hp_living_probe_safe", "state": safe,
                                          "attributes": {"configuration_signature": watchdog_signature(config)}}]}],
        "automation": [{
            "id": "hp_heater_watchdog",
            "alias": "HP heater watchdog",
            "initial_state": True,
            "mode": "single",
            "triggers": [
                {"trigger": "time_pattern", "seconds": "/15"},
                {"trigger": "homeassistant", "event": "start"},
                {"trigger": "state", "entity_id": ["input_boolean.hp_enabled", "binary_sensor.hp_living_probe_safe", config.heater_entity]},
            ],
            "conditions": [{"condition": "state", "entity_id": "input_boolean.hp_heater_owned", "state": "on"},
                           {"condition": "template", "value_template": unsafe}],
            "actions": [
                {"action": "climate.set_hvac_mode", "target": {"entity_id": config.heater_entity}, "data": {"hvac_mode": "off"}},
                {"action": "persistent_notification.create", "data": {"notification_id": "hp_heater_watchdog",
                    "title": "Living-room Dyson watchdog", "message": "Dyson stopped: controller disabled, heartbeat expired, or heating with unsafe/unavailable external probes. Check the climate controller."}},
            ],
        }],
    }


def write_package(config: Config, path: str) -> None:
    Path(path).write_text(json.dumps(package(config), indent=2, ensure_ascii=False) + "\n")


def write_dashboard(path: str) -> None:
    entities = ["input_boolean.hp_enabled", "input_text.hp_status"]
    entities += [helper(name, "input_boolean") for name in TOGGLES]
    entities += [helper(name) for name in NUMBERS]
    entities += [helper(name, "input_datetime") for name in TIMES]
    entities += ["input_datetime.hp_heartbeat", "binary_sensor.hp_living_probe_safe", "automation.hp_heater_watchdog"]
    Path(path).write_text(json.dumps({"type": "entities", "title": "Home Climate controller", "entities": entities}, indent=2) + "\n")
