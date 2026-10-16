"""Read version-1 household configuration. New configurations use explicit room profiles."""
from __future__ import annotations

LOW, HIGH = 66.0, 75.0
NUMBERS = {
    "office_target": (72, 67, 74, .5),
    "guest_target": (72, 67, 74, .5),
    "living_target": (68, 67, 69, .5),
    "primary_night_target": (68, 67, 74, .5),
    "primary_morning_high": (70, 68, 74, .5),
    "comfort_half_band": (.5, .25, 1, .25),
    "living_half_band": (.5, .25, 1, .25),
    "primary_night_half_band": (1, .5, 1.5, .5),
    "primary_night_fan_percentage": (60, 0, 100, 10),
    "transfer_fan_percentage": (60, 0, 100, 10),
    "guest_fan_percentage": (50, 0, 100, 10),
    "mode_hold_minutes": (20, 5, 120, 1),
}
TIMES = {"night_start": "22:00:00", "night_end": "05:00:00", "morning_end": "09:00:00"}
TOGGLES = {"living_fan_enabled": True}


def helper(name: str, domain: str = "input_number") -> str:
    return f"{domain}.hp_{name}"




def upgrade(raw):
    from .config import Config, FanControl, Period, Probe, Profile, Zone
    raw = dict(raw)
    zones = {}
    for key, value in raw.pop("zones").items():
        probes = tuple(Probe(**{"source": p["entity_id"], **p}) for p in value["sensors"])
        profile = Profile(target_f=f"@{key}_target", half_band_f="@comfort_half_band")
        schedules = ()
        if key == "living":
            profile = Profile(target_f="@living_target", half_band_f="@living_half_band", priority=.1)
        elif key == "primary":
            profile = Profile(comfort_enabled=False)
            schedules = (
                Period("@night_start", "@night_end", Profile(target_f="@primary_night_target",
                       half_band_f="@primary_night_half_band", priority=10,
                       fan_percentage="@primary_night_fan_percentage"), "Sleep"),
                Period("@night_end", "@morning_end", Profile(target_f="@primary_morning_high",
                       target_offset_f=-.5, low_f=LOW, high_f="@primary_morning_high"), "Morning"),
            )
        elif key == "entertainment":
            profile = Profile(comfort_enabled=False)
        elif key not in ("office", "guest"):
            profile = Profile()
        zones[key] = Zone(value["head"], probes, name=key.replace("_", " ").title(),
                          profile=profile, schedules=schedules)
    fans = raw.get("fans", {})
    controls = {}
    for key, entity in fans.items():
        if key in ("office", "entertainment") and "office" in zones and "entertainment" in zones:
            controls[key] = FanControl(entity, "office", "transfer", "@transfer_fan_percentage", "entertainment")
        elif key == "primary":
            controls[key] = FanControl(entity, key, "schedule")
        else:
            controls[key] = FanControl(entity, key, "cooling", "@guest_fan_percentage")
    return Config(zones=zones, fan_controls=controls, auxiliary_room="living" if raw.get("heater_entity") else None,
                  legacy=True, helper_prefix="hp", **raw)
