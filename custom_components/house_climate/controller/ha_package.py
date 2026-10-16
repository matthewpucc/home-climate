"""Generate independent HA lease and auxiliary-heater watchdog helpers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .config import Config


def watchdog_signature(config: Config) -> str:
    if config.legacy:
        from .legacy_package import watchdog_signature as legacy_signature
        return legacy_signature(config)
    probes = config.zones[config.auxiliary_room].probes if config.auxiliary_room else ()
    mapping = {"watchdog_version": 3, "probes": [asdict(p) for p in probes],
               "stale": config.sensor_stale_seconds, "cutoff": config.heater_cutoff_f,
               "heater": config.heater_entity, "prefix": config.helper_prefix}
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:16]


def package(config: Config) -> dict:
    if config.legacy:
        from .legacy_package import package as legacy_package
        return legacy_package(config)
    prefix = config.helper_prefix
    entity = config.entity
    result = {
        "input_boolean": {
            f"{prefix}_enabled": {"name": "House Climate enabled", "initial": False},
            f"{prefix}_heater_owned": {"name": "House Climate controller ownership"},
        },
        "input_datetime": {f"{prefix}_heartbeat": {
            "name": "House Climate heartbeat", "has_date": True, "has_time": True}},
        "input_text": {f"{prefix}_status": {"name": "House Climate status", "max": 255}},
    }
    if not config.heater_entity:
        return result
    probes = [{"entity": p.entity_id, "attribute": p.attribute or "", "unit": p.unit or "",
               "offset": p.offset_f} for p in config.zones[config.auxiliary_room].probes]
    safe = """{% set probes = PROBES %}
{% set ns = namespace(values=[]) %}
{% for p in probes %}
  {% set s = states[p.entity] %}
  {% if s is defined and s is not none and s.state not in ['unavailable', 'unknown'] %}
    {% set reported = s.last_reported | default(s.last_updated, true) %}
    {% set age = as_timestamp(now()) - as_timestamp(reported, 0) %}
    {% set raw = state_attr(p.entity, p.attribute) if p.attribute else s.state %}
    {% set unit = p.unit or state_attr(p.entity, 'unit_of_measurement') %}
    {% if -60 <= age <= STALE and is_number(raw) and unit in ['°F', 'F', '°C', 'C'] %}
      {% set f = (raw | float * 9 / 5 + 32) if unit in ['°C', 'C'] else raw | float %}
      {% set f = f + p.offset %}
      {% if -40 <= f <= 140 %}{% set ns.values = ns.values + [f] %}{% endif %}
    {% endif %}
  {% endif %}
{% endfor %}
{{ ns.values | count > 0 and ns.values | max < CUTOFF }}"""
    safe = safe.replace("PROBES", json.dumps(probes, ensure_ascii=False)).replace("STALE", str(config.sensor_stale_seconds)).replace("CUTOFF", str(config.heater_cutoff_f))

    unsafe = """{% set age = as_timestamp(now()) - as_timestamp(states(HEARTBEAT), 0) %}
{% set could_heat = states(HEATER) not in ['off', 'fan_only'] or state_attr(HEATER, 'hvac_action') == 'heating' %}
{{ not is_state(ENABLED, 'on') or age < -60 or age > 120
   or (could_heat and not is_state(PROBE_SAFE, 'on')) }}"""
    for key, value in {"HEARTBEAT": entity("heartbeat", "input_datetime"),
                       "HEATER": config.heater_entity, "ENABLED": entity("enabled"),
                       "PROBE_SAFE": entity("auxiliary_probe_safe", "binary_sensor")}.items():
        unsafe = unsafe.replace(key, json.dumps(value))
    result["template"] = [{"binary_sensor": [{
        "name": f"{prefix.replace('_', ' ').title()} auxiliary probe safe",
        "unique_id": f"{prefix}_auxiliary_probe_safe",
        "default_entity_id": entity("auxiliary_probe_safe", "binary_sensor"), "state": safe,
        "attributes": {"configuration_signature": watchdog_signature(config)},
    }]}]
    result["automation"] = [{
        "id": f"{prefix}_auxiliary_watchdog", "alias": f"{prefix.replace('_', ' ').title()} auxiliary watchdog",
        "initial_state": True, "mode": "single",
        "triggers": [
            {"trigger": "time_pattern", "seconds": "/15"},
            {"trigger": "homeassistant", "event": "start"},
            {"trigger": "state", "entity_id": [
                entity("enabled"), entity("auxiliary_probe_safe", "binary_sensor"), config.heater_entity]},
        ],
        "conditions": [
            {"condition": "state", "entity_id": entity("heater_owned"), "state": "on"},
            {"condition": "template", "value_template": unsafe},
        ],
        "actions": [
            {"action": "climate.set_hvac_mode", "target": {"entity_id": config.heater_entity},
             "data": {"hvac_mode": "off"}},
            {"action": "persistent_notification.create", "data": {
                "notification_id": f"{prefix}_auxiliary_watchdog", "title": "House Climate auxiliary watchdog",
                "message": "Auxiliary heater stopped: controller disabled, heartbeat expired, or external probes unsafe/unavailable. Check House Climate.",
            }},
        ],
    }]
    return result


def write_package(config: Config, path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(package(config), indent=2, ensure_ascii=False) + "\n")


def write_dashboard(path: str, config: Config | None = None) -> None:
    if config is None or config.legacy:
        from .legacy_package import write_dashboard as legacy_dashboard
        legacy_dashboard(path)
        return
    entities = [config.entity("enabled"), config.entity("status", "input_text"),
                config.entity("heartbeat", "input_datetime"), config.entity("heater_owned")]
    if config.heater_entity:
        entities += [config.entity("auxiliary_probe_safe", "binary_sensor"), config.entity("auxiliary_watchdog", "automation")]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"type": "entities", "title": "House Climate", "entities": entities}, indent=2) + "\n")
