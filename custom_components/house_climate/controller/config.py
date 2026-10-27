"""Versioned configuration for rooms sharing a single heat/cool system."""
from __future__ import annotations

import copy
import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .protection import ProtectionTemperatures

# Kept as import aliases for version-1 configuration and its migration tests.
from .legacy import LOW, HIGH, NUMBERS, TIMES, TOGGLES, helper

Value = float | str


class HeartbeatTimeoutError(ValueError):
    """A watchdog setting that can be highlighted on its HA form field."""

    def __init__(self, field, code, message):
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(frozen=True)
class Probe:
    entity_id: str
    source: str
    attribute: str | None = None
    unit: str | None = None
    offset_f: float = 0
    report_on_change: bool = False


def parse_outdoor_probes(value):
    """Explicit current-temperature source wiring, with no rule overrides."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("Outdoor sources must be a list of explicit source objects")
    result, entities = [], set()
    for selected in value:
        if isinstance(selected, Probe):
            probe = selected
        else:
            if (not isinstance(selected, dict) or not {"entity_id", "source"} <= set(selected)
                    or set(selected) - {"entity_id", "source", "attribute", "unit"}):
                raise ValueError("Outdoor sources require entity_id/source and optional attribute/unit")
            probe = Probe(**selected)
        if (not isinstance(probe.entity_id, str)
                or not re.fullmatch(r"(?:sensor|weather)\.[a-z0-9_]+", probe.entity_id)):
            raise ValueError("Outdoor sources must use sensor or weather entity IDs")
        if not isinstance(probe.source, str) or not probe.source.strip():
            raise ValueError("Outdoor sources need an explicit physical source identity")
        if (probe.attribute is not None and (not isinstance(probe.attribute, str)
                or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", probe.attribute))):
            raise ValueError("Outdoor source attributes must name one current numeric attribute")
        if probe.entity_id.startswith("weather.") and probe.attribute not in (None, "temperature"):
            raise ValueError("Weather sources must use the current temperature attribute")
        if probe.unit not in (None, "°F", "F", "°C", "C"):
            raise ValueError("Outdoor source units must be Fahrenheit or Celsius")
        if probe.offset_f != 0 or probe.report_on_change is not False:
            raise ValueError("Outdoor wiring cannot override calibration or freshness rules")
        if probe.entity_id in entities:
            raise ValueError("Duplicate outdoor entity; aliases must use different entities and the same source")
        entities.add(probe.entity_id)
        # Whitespace must not make aliases of one source look independent.
        result.append(Probe(probe.entity_id, probe.source.strip(), probe.attribute, probe.unit))
    return tuple(result)


@dataclass(frozen=True)
class Profile:
    target_f: Value = 70
    half_band_f: Value = .5
    priority: float = 1
    comfort_enabled: bool = True
    fan_percentage: Value = 0
    low_f: Value | None = None
    high_f: Value | None = None
    target_offset_f: float = 0


@dataclass(frozen=True)
class Period:
    start: str
    end: str
    profile: Profile
    name: str = ""


@dataclass(frozen=True)
class Zone:
    head: str
    probes: tuple[Probe, ...]
    name: str = ""
    area_id: str | None = None
    profile: Profile = field(default_factory=Profile)
    schedules: tuple[Period, ...] = ()
    windows: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.windows, (list, tuple)):
            raise ValueError("Room windows must list binary_sensor entity IDs")
        object.__setattr__(self, "windows", tuple(self.windows))


@dataclass(frozen=True)
class FanControl:
    entity_id: str
    room: str
    strategy: str = "cooling"
    percentage: Value = 50
    source_room: str | None = None


@dataclass(frozen=True)
class Config:
    timezone: str
    heads: dict[str, str]
    fans: dict[str, str]
    zones: dict[str, Zone]
    heater_entity: str | None = None
    auxiliary_room: str | None = None
    auxiliary_fan_enabled: bool = True
    fan_controls: dict[str, FanControl] = field(default_factory=dict)
    low_f: float = 66
    high_f: float = 75
    helper_prefix: str = "house_climate"
    legacy: bool = False
    poll_seconds: float = 15
    decision_interval_seconds: float = 90
    head_minimum_run_seconds: float = 300
    head_minimum_off_seconds: float = 180
    head_heat_call_offset_f: float = 5
    head_cool_call_offset_f: float = 5
    head_cool_min_f: dict[str, float] = field(default_factory=dict)
    sensor_stale_seconds: float = 900
    sensor_confirmation_seconds: float = 60
    sensor_disagreement_f: float = 3
    forecast_minutes: float = 10
    mode_hold_seconds: float = 1200
    switch_confirmation_seconds: float = 120
    off_settle_seconds: float = 180
    command_timeout_seconds: float = 45
    head_activation_timeout_seconds: float = 120
    heartbeat_timeout_seconds: float = 300
    auxiliary_heartbeat_timeout_seconds: float = 120
    equipment_timing_confirmed: bool = False
    heater_high_setpoint_f: float = 86
    heater_setpoint_confirmed: bool = False
    heater_cutoff_f: float = 70
    protection: ProtectionTemperatures | None = None
    outdoor_probes: tuple[Probe, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "outdoor_probes", parse_outdoor_probes(self.outdoor_probes))
        if self.protection is not None and not isinstance(self.protection, ProtectionTemperatures):
            raise ValueError("Protection must be a ProtectionTemperatures object or absent")

    def entity(self, name, domain="input_boolean"):
        if self.legacy:
            name = {"auxiliary_probe_safe": "living_probe_safe", "auxiliary_watchdog": "heater_watchdog"}.get(name, name)
        return f"{domain}.{self.helper_prefix}_{name}"

    @property
    def climate_entities(self):
        return [*self.heads.values(), *([self.heater_entity] if self.heater_entity else [])]

    @classmethod
    def load(cls, path):
        with open(path, "rb") as stream:
            return cls.from_dict(tomllib.load(stream))

    @classmethod
    def from_dict(cls, data):
        raw = copy.deepcopy(data)
        if "zones" in raw and "schema_version" not in raw:
            from .legacy import upgrade
            result = upgrade(raw)
            result.validate()
            return result
        if raw.pop("schema_version", None) != 2:
            raise ValueError("Unsupported configuration version; expected schema_version = 2")
        protection = raw.pop("protection", None)
        if protection is not None:
            protection = ProtectionTemperatures.from_dict(protection)
        outdoor = parse_outdoor_probes(raw.pop("outdoor_probes", ()))
        zones, fans, controls = {}, {}, {}
        for key, value in raw.pop("rooms", {}).items():
            value = dict(value)
            probes = tuple(Probe(**{"source": p["entity_id"], **p}) for p in value.pop("sensors", []))
            profile = Profile(**value.pop("profile", {}))
            periods = []
            for period in value.pop("schedules", []):
                period = dict(period)
                start, end, name = period.pop("start"), period.pop("end"), period.pop("name", "")
                periods.append(Period(start, end, Profile(**period), name))
            for fan in value.pop("fans", []):
                fan = FanControl(room=key, **fan)
                if fan.entity_id in fans:
                    raise ValueError("A fan can belong to only one room")
                fans[fan.entity_id] = fan.entity_id
                controls[fan.entity_id] = fan
            zones[key] = Zone(probes=probes, profile=profile, schedules=tuple(periods), **value)
        result = cls(zones=zones, fans=fans, fan_controls=controls,
                     protection=protection, outdoor_probes=outdoor, **raw)
        result.validate()
        return result

    def to_dict(self, settings=None):
        settings = settings or Settings.read({}, self)
        result = {key: value for key, value in asdict(self).items()
                  if key not in ("zones", "fans", "fan_controls", "legacy")}
        if self.outdoor_probes:
            result["outdoor_probes"] = [
                {key: value for key, value in {"entity_id": probe.entity_id, "source": probe.source,
                  "attribute": probe.attribute, "unit": probe.unit}.items() if value is not None}
                for probe in self.outdoor_probes]
        else:
            result.pop("outdoor_probes", None)
        if self.legacy:
            result["mode_hold_seconds"] = settings["mode_hold_minutes"] * 60
            result["auxiliary_fan_enabled"] = settings.toggles["auxiliary_fan_enabled"]
        result["schema_version"] = 2
        result["rooms"] = {}
        def profile_data(profile):
            data = asdict(profile)
            for key in ("target_f", "half_band_f", "fan_percentage", "low_f", "high_f"):
                if data[key] is not None:
                    data[key] = settings.value(data[key])
            return data
        for key, room in self.zones.items():
            data = {"name": room.name or key, "head": room.head,
                    "sensors": [asdict(probe) for probe in room.probes],
                    "profile": profile_data(room.profile), "schedules": [], "fans": []}
            if room.area_id:
                data["area_id"] = room.area_id
            if room.windows:
                data["windows"] = list(room.windows)
            for period in room.schedules:
                data["schedules"].append({"start": settings.clock(period.start).isoformat(),
                    "end": settings.clock(period.end).isoformat(), "name": period.name, **profile_data(period.profile)})
            for fan in self.fan_controls.values():
                if fan.room == key:
                    value = asdict(fan)
                    value.pop("room")
                    value["percentage"] = settings.value(value["percentage"])
                    if value["source_room"] is None:
                        value.pop("source_room")
                    data["fans"].append(value)
            result["rooms"][key] = data
        return {key: value for key, value in result.items() if value is not None}

    def validate(self, live=False):
        ZoneInfo(self.timezone)
        if not self.zones or not self.heads:
            raise ValueError("Configure at least one room and one shared-system head")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.helper_prefix):
            raise ValueError("Invalid helper prefix")
        if not all(math.isfinite(v) for v in (self.low_f, self.high_f)) or self.high_f - self.low_f < 3:
            raise ValueError("Temperature limits must be finite and at least 3°F apart")
        if type(self.equipment_timing_confirmed) is not bool or type(self.heater_setpoint_confirmed) is not bool:
            raise ValueError("Commissioning flags must be booleans")
        if len(set(self.heads.values())) != len(self.heads) or self.heater_entity in self.heads.values():
            raise ValueError("Heads and auxiliary heater must be distinct")
        if len(set(self.fans.values())) != len(self.fans):
            raise ValueError("Fans must be distinct")
        for domain, entities in (("climate", self.climate_entities), ("fan", self.fans.values())):
            if any(not re.fullmatch(domain + r"\.[a-z0-9_]+", entity) for entity in entities):
                raise ValueError(f"Invalid {domain} entity ID")
        if bool(self.heater_entity) != bool(self.auxiliary_room) or (self.auxiliary_room and self.auxiliary_room not in self.zones):
            raise ValueError("Assign the optional auxiliary heater to a configured room")
        settings = Settings.read({}, self)
        for key, room in self.zones.items():
            if not isinstance(room.name, str):
                raise ValueError("Room names must be text")
            if not re.fullmatch(r"[a-z0-9_]+", key) or room.head not in self.heads:
                raise ValueError("Invalid room ID or head reference")
            if not room.probes:
                raise ValueError(f"Configure temperature probes for {room.name or key}")
            if len({p.entity_id for p in room.probes}) != len(room.probes):
                raise ValueError("Duplicate probe entity in a room")
            if any(not isinstance(entity, str) or not re.fullmatch(r"binary_sensor\.[a-z0-9_]+", entity)
                   for entity in room.windows):
                raise ValueError("Room windows must use binary_sensor entity IDs")
            if len(set(room.windows)) != len(room.windows):
                raise ValueError("Duplicate window contact in a room")
            for probe in room.probes:
                if type(probe.report_on_change) is not bool:
                    raise ValueError("Probe report_on_change must be a boolean")
                if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", probe.entity_id) or not probe.source:
                    raise ValueError("Invalid probe entity or physical source")
                if probe.entity_id == self.heater_entity or not math.isfinite(probe.offset_f):
                    raise ValueError("Use calibrated independent room probes for the auxiliary thermostat")
            for profile in (room.profile, *(period.profile for period in room.schedules)):
                target, half = settings.value(profile.target_f) + profile.target_offset_f, settings.value(profile.half_band_f)
                values = (target, half, profile.priority, settings.value(profile.fan_percentage))
                if not all(math.isfinite(v) for v in values) or not self.low_f <= target <= self.high_f or not 0 < half <= 5:
                    raise ValueError("Invalid room comfort target or deadband")
                if not 0 < profile.priority <= 10 or not 0 <= settings.value(profile.fan_percentage) <= 100:
                    raise ValueError("Invalid room priority or scheduled fan speed")
                if type(profile.comfort_enabled) is not bool:
                    raise ValueError("Comfort enabled must be a boolean")
                low = settings.value(profile.low_f) if profile.low_f is not None else max(self.low_f, target-half)
                high = settings.value(profile.high_f) if profile.high_f is not None else min(self.high_f, target+half)
                if not self.low_f <= low < high <= self.high_f or not low <= target <= high:
                    raise ValueError("Room comfort bands must stay inside the system limits")
                if key == self.auxiliary_room and profile.comfort_enabled and high > self.heater_cutoff_f:
                    raise ValueError("Auxiliary room target/deadband exceeds the independent heater cutoff")
            intervals = []
            for period in room.schedules:
                start, end = settings.clock(period.start), settings.clock(period.end)
                a, b = seconds(start), seconds(end)
                if a == b:
                    raise ValueError("Schedule start and end must differ; use the base profile for all-day settings")
                parts = [(a,b)] if a < b else [(a,86400),(0,b)]
                if any(max(x,a2) < min(y,b2) for x,y in parts for a2,b2 in intervals):
                    raise ValueError("Schedules within a room must not overlap")
                intervals.extend(parts)
        for fan in self.fan_controls.values():
            if fan.room not in self.zones or fan.entity_id not in self.fans.values():
                raise ValueError("Invalid fan room/entity")
            if fan.strategy not in ("cooling", "conditioning", "always", "schedule", "transfer"):
                raise ValueError("Invalid fan strategy")
            if fan.strategy == "transfer" and (fan.source_room not in self.zones or fan.source_room == fan.room):
                raise ValueError("An airflow fan needs a different source room")
            if not 0 <= settings.value(fan.percentage) <= 100:
                raise ValueError("Invalid fan percentage")
        if set(self.fans.values()) != {f.entity_id for f in self.fan_controls.values()}:
            raise ValueError("Every fan must have a control rule")
        for name in ("poll_seconds", "sensor_stale_seconds", "sensor_confirmation_seconds",
                     "sensor_disagreement_f", "mode_hold_seconds", "off_settle_seconds",
                     "command_timeout_seconds", "switch_confirmation_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("decision_interval_seconds", "head_minimum_run_seconds", "head_minimum_off_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0
                    or (name == "decision_interval_seconds" and value == 0)):
                requirement = "positive" if name == "decision_interval_seconds" else "nonnegative"
                raise ValueError(f"{name} must be finite and {requirement}")
        value = self.head_activation_timeout_seconds
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 1 <= value <= 300):
            raise ValueError("head_activation_timeout_seconds must be from 1 to 300 seconds")
        for name, minimum, maximum, code in (
            ("heartbeat_timeout_seconds", 60, 900, "invalid_heartbeat_timeout"),
            ("auxiliary_heartbeat_timeout_seconds", 30, 900, "invalid_auxiliary_heartbeat_timeout"),
        ):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not minimum <= value <= maximum):
                raise HeartbeatTimeoutError(
                    name, code, f"{name} must be from {minimum} to {maximum} seconds")
        if self.auxiliary_heartbeat_timeout_seconds > self.heartbeat_timeout_seconds:
            raise HeartbeatTimeoutError(
                "auxiliary_heartbeat_timeout_seconds", "auxiliary_heartbeat_exceeds_controller",
                "auxiliary_heartbeat_timeout_seconds must not exceed heartbeat_timeout_seconds")
        if not isinstance(self.head_cool_min_f, dict):
            raise ValueError("head_cool_min_f must map configured climate entity IDs to temperatures")
        for entity, value in self.head_cool_min_f.items():
            if entity not in self.heads.values():
                raise ValueError("Cooling minimums must reference configured heat-pump climate entity IDs")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not -40 <= value <= 140):
                raise ValueError("Cooling minimums must be finite temperatures from -40 to 140°F")
        for name in ("head_heat_call_offset_f", "head_cool_call_offset_f"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 20):
                raise ValueError(f"{name} must be a finite temperature difference from 0 to 20°F")
        if not 0 <= self.forecast_minutes <= 30:
            raise ValueError("Invalid forecast horizon")
        if self.heater_entity and (not self.low_f < self.heater_cutoff_f < self.high_f or not math.isfinite(self.heater_high_setpoint_f)):
            raise ValueError("Invalid auxiliary heater setpoint or cutoff")
        if live and (not self.equipment_timing_confirmed or (self.heater_entity and not self.heater_setpoint_confirmed)):
            raise ValueError("Confirm equipment timing and any auxiliary heater setpoint before live control")
        if live and (self.poll_seconds > 30 or self.command_timeout_seconds > 60):
            raise ValueError("Live watchdog checks require poll_seconds <= 30 seconds and command_timeout_seconds <= 60 seconds")


def seconds(value):
    return value.hour * 3600 + value.minute * 60 + value.second


@dataclass
class Settings:
    numbers: dict[str, float]
    times: dict[str, time]
    toggles: dict[str, bool]
    room_profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def read(cls, states, config, strict=False):
        if not config.legacy:
            return cls({"mode_hold_minutes": config.mode_hold_seconds / 60}, {},
                       {"auxiliary_fan_enabled": config.auxiliary_fan_enabled})
        from .legacy_settings import read
        result = read(cls, states, config, strict)
        result.toggles["auxiliary_fan_enabled"] = result.toggles["living_fan_enabled"]
        return result

    def __getitem__(self, key):
        return self.numbers[key]

    def value(self, value):
        return float(self.numbers[value[1:]]) if isinstance(value, str) and value.startswith("@") else float(value)

    def clock(self, value):
        return self.times[value[1:]] if value.startswith("@") else time.fromisoformat(value)

    def active(self, period, local):
        start, end = self.clock(period.start), self.clock(period.end)
        return start <= local < end if start < end else local >= start or local < end

    def profile(self, room, now, timezone, room_id=None):
        if room_id is not None and room_id in self.room_profiles:
            return self.room_profiles[room_id]
        local = now.astimezone(ZoneInfo(timezone)).time()
        return next((p.profile for p in room.schedules if self.active(p, local)), room.profile)

    def night(self, local):
        return local >= self.times["night_start"] or local < self.times["night_end"]
