"""A small semantic freshness check before a disruptive integration repair."""
from dataclasses import asdict
import hashlib
import json

from .sensors import probe_value


def signature(config, states, now, settings, climate_unit="°F"):
    """Ignore report churn, but expire probes and schedules at their boundaries."""
    probes = []
    profiles = []
    for name, room in config.zones.items():
        for probe in room.probes:
            state = states.get(probe.entity_id, {})
            attrs = state.get("attributes", {})
            try:
                value = ("valid", probe_value(probe, states, now, config.sensor_stale_seconds, climate_unit))
            except (ValueError, TypeError, OverflowError):
                value = ("invalid", state.get("state"))
            probes.append((name, probe.entity_id, probe.attribute, value,
                           probe.unit or attrs.get("unit_of_measurement") or climate_unit))
        profile = asdict(settings.profile(room, now, config.timezone, name))
        for key in ("target_f", "half_band_f", "fan_percentage", "low_f", "high_f"):
            if profile[key] is not None:
                profile[key] = settings.value(profile[key])
        profiles.append((name, name in settings.room_profiles, profile))
    heads = []
    for entity in config.climate_entities:
        state = states.get(entity, {})
        attrs = state.get("attributes", {})
        metadata = {key: attrs.get(key) for key in (
            "hvac_modes", "preset_mode", "operation_mode", "hvac_action", "restored",
            "min_temp", "max_temp", "target_temp_step", "temperature_unit")}
        modes = metadata.get("hvac_modes")
        if isinstance(modes, (list, tuple)) and all(isinstance(mode, str) for mode in modes):
            metadata["hvac_modes"] = sorted(set(modes))
        heads.append((entity, state.get("state"), metadata))
    material = [probes, heads, profiles, settings.numbers,
                {key: value.isoformat() for key, value in settings.times.items()}, settings.toggles]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
