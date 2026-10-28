"""Private device wiring and explicit power-confirmation calibration."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from .aggregate_stop import AggregateStopConfig
from .config import OutdoorProbe, outdoor_probe_dict, parse_outdoor_probes

BINDINGS_FILENAME = "home_climate_bindings.json"
LEGACY_BINDINGS_FILENAME = "house_climate_bindings.json"
HEAD_ROLES = frozenset({"bonus", "guest", "primary", "living"})
ROOM_ROLES = frozenset({"office", "entertainment", "guest", "primary", "living"})
FAN_ROLES = frozenset({"primary", "guest", "office", "entertainment"})
POWER_ROLES = frozenset({
    "heater_switch", "battery", "circuit_power", "input_power", "output_power",
    "heater_current", "wall_phase_a_voltage", "wall_phase_b_voltage",
})


class BindingsError(ValueError):
    """Missing or invalid private wiring must prevent equipment control."""


def _keys(value, expected, label, *, optional=()):
    required, allowed = set(expected), set(expected) | set(optional)
    if not isinstance(value, dict) or not required <= set(value) <= allowed:
        detail = f"; optional: {', '.join(sorted(optional))}" if optional else ""
        raise BindingsError(f"{label} must contain exactly: {', '.join(sorted(expected))}{detail}")


def _entity(value, domain, label):
    if not isinstance(value, str) or not re.fullmatch(domain + r"\.[a-z0-9_]+", value):
        raise BindingsError(f"{label} must be a {domain} entity ID")
    return value


@dataclass(frozen=True)
class PowerBindings:
    heater: str
    heater_switch: str | None
    battery: str
    circuit_power: str
    input_power: str
    output_power: str
    heater_current: str | None
    wall_phase_a_voltage: str
    wall_phase_b_voltage: str
    emergency_ac_switch: str | None = None
    emergency_ac_fallback_enabled: bool = False
    aggregate_stop_confirmation: AggregateStopConfig | None = None

    def __post_init__(self):
        if type(self.emergency_ac_fallback_enabled) is not bool:
            raise BindingsError("power.emergency_ac_fallback_enabled must be a boolean")
        if self.emergency_ac_fallback_enabled and self.emergency_ac_switch is None:
            raise BindingsError("Enabled emergency AC fallback requires power.emergency_ac_switch")
        if (self.aggregate_stop_confirmation is not None
                and not isinstance(self.aggregate_stop_confirmation, AggregateStopConfig)):
            raise BindingsError("power.aggregate_stop_confirmation must be validated calibration")
        values = asdict(self)
        values.pop("aggregate_stop_confirmation")
        values.pop("emergency_ac_fallback_enabled")
        for role, value in values.items():
            if value is None and role in {"heater_switch", "heater_current", "emergency_ac_switch"}:
                continue
            domain = ("climate" if role == "heater" else "switch"
                      if role in {"heater_switch", "emergency_ac_switch"} else "sensor")
            _entity(value, domain, f"power.{role}")
        entities = [value for value in values.values() if value is not None]
        if len(set(entities)) != len(entities):
            raise BindingsError("Power roles must use distinct device entities")
        if self.heater_switch is None and self.emergency_ac_switch is None:
            raise BindingsError("A direct heater path requires power.emergency_ac_switch")

    @property
    def entity_ids(self):
        return frozenset(value for role, value in asdict(self).items()
                         if role not in {"aggregate_stop_confirmation", "emergency_ac_fallback_enabled"}
                         and value is not None)

    def to_dict(self):
        """Keep absent optional stages absent in existing private wiring files."""
        result = asdict(self)
        result.pop("heater")
        result.pop("aggregate_stop_confirmation")
        if self.emergency_ac_switch is None:
            result.pop("emergency_ac_switch")
        if not self.emergency_ac_fallback_enabled:
            result.pop("emergency_ac_fallback_enabled")
        if self.aggregate_stop_confirmation is not None:
            result["aggregate_stop_confirmation"] = self.aggregate_stop_confirmation.to_dict()
        return result


@dataclass(frozen=True)
class HouseBindings:
    heads: Mapping[str, str]
    rooms: Mapping[str, tuple[str, ...]]
    fans: Mapping[str, str]
    heater: str
    power: PowerBindings
    windows: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    outdoor: tuple[OutdoorProbe, ...] = ()

    def __post_init__(self):
        if not all(isinstance(value, Mapping) for value in (self.heads, self.rooms, self.fans, self.windows)):
            raise BindingsError("House bindings must use named device-role mappings")
        heads, selected_rooms, fans = dict(self.heads), dict(self.rooms), dict(self.fans)
        _keys(heads, HEAD_ROLES, "heads")
        _keys(selected_rooms, ROOM_ROLES, "rooms")
        _keys(fans, FAN_ROLES, "fans")
        heads = {role: _entity(value, "climate", f"heads.{role}") for role, value in heads.items()}
        heater = _entity(self.heater, "climate", "heater")
        if len(set(heads.values()) | {heater}) != len(heads) + 1:
            raise BindingsError("Heads and the auxiliary heater must be distinct")
        fans = {role: _entity(value, "fan", f"fans.{role}") for role, value in fans.items()}
        if len(set(fans.values())) != len(fans):
            raise BindingsError("Fan roles must use distinct devices")
        rooms, probes = {}, set()
        for role, selected in selected_rooms.items():
            if not isinstance(selected, (list, tuple)) or not selected:
                raise BindingsError(f"rooms.{role} must list one or more ambient sensor entity IDs")
            values = tuple(_entity(entity, "sensor", f"rooms.{role}") for entity in selected)
            if len(set(values)) != len(values) or probes.intersection(values):
                raise BindingsError("Each room must use its own distinct ambient probes")
            rooms[role] = values
            probes.update(values)
        if set(self.windows) - ROOM_ROLES:
            raise BindingsError("windows must use configured room roles")
        windows = {}
        for role, selected in self.windows.items():
            if not isinstance(selected, (list, tuple)):
                raise BindingsError(f"windows.{role} must list binary_sensor entity IDs")
            values = tuple(_entity(entity, "binary_sensor", f"windows.{role}") for entity in selected)
            if len(set(values)) != len(values):
                raise BindingsError("Duplicate window contact in a room")
            # One explicitly mapped opening may affect multiple configured rooms.
            windows[role] = values
        if not isinstance(self.power, PowerBindings) or self.power.heater != heater:
            raise BindingsError("The power guard must control the bound auxiliary heater")
        object.__setattr__(self, "heads", MappingProxyType(heads))
        object.__setattr__(self, "rooms", MappingProxyType(rooms))
        object.__setattr__(self, "fans", MappingProxyType(fans))
        object.__setattr__(self, "windows", MappingProxyType(windows))
        try:
            object.__setattr__(self, "outdoor", parse_outdoor_probes(self.outdoor))
        except (TypeError, ValueError) as exc:
            raise BindingsError(str(exc)) from None

    @classmethod
    def from_dict(cls, raw):
        _keys(raw, {"heads", "rooms", "fans", "heater", "power"}, "Bindings", optional={"windows", "outdoor"})
        _keys(raw["heads"], HEAD_ROLES, "heads")
        _keys(raw["rooms"], ROOM_ROLES, "rooms")
        _keys(raw["fans"], FAN_ROLES, "fans")
        _keys(raw["power"], POWER_ROLES, "power",
              optional={"emergency_ac_switch", "emergency_ac_fallback_enabled",
                        "aggregate_stop_confirmation"})
        for role, selected in raw["rooms"].items():
            if not isinstance(selected, list):
                raise BindingsError(f"rooms.{role} must list ambient sensor entity IDs")
        windows = raw.get("windows", {})
        if not isinstance(windows, dict):
            raise BindingsError("windows must map room roles to binary_sensor entity lists")
        for role, selected in windows.items():
            if not isinstance(selected, list):
                raise BindingsError(f"windows.{role} must list binary_sensor entity IDs")
        power_data = dict(raw["power"])
        if power_data.get("aggregate_stop_confirmation") is not None:
            try:
                power_data["aggregate_stop_confirmation"] = AggregateStopConfig.from_dict(
                    power_data["aggregate_stop_confirmation"])
            except (TypeError, ValueError) as exc:
                raise BindingsError(str(exc)) from None
        power = PowerBindings(heater=raw["heater"], **power_data)
        outdoor = raw.get("outdoor", [])
        if not isinstance(outdoor, list) or any(not isinstance(item, dict) for item in outdoor):
            raise BindingsError("outdoor must list explicit current-temperature source objects")
        return cls(raw["heads"], raw["rooms"], raw["fans"], raw["heater"], power, windows, outdoor)

    def to_dict(self):
        power = self.power.to_dict()
        result = {"heads": dict(self.heads),
                "rooms": {role: list(probes) for role, probes in self.rooms.items()},
                "fans": dict(self.fans), "heater": self.heater, "power": power}
        if self.windows:
            result["windows"] = {role: list(entities) for role, entities in self.windows.items()}
        if self.outdoor:
            result["outdoor"] = [outdoor_probe_dict(probe) for probe in self.outdoor]
        return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BindingsError("Bindings contain a duplicate key")
        result[key] = value
    return result


def load_bindings(config_dir):
    """Prefer current wiring; accept the old filename only when current is absent."""
    root = Path(config_dir).resolve()
    source = root / BINDINGS_FILENAME
    # lexists semantics: a broken symlink is a bad current file, not permission
    # to silently select different equipment from the legacy file.
    if not source.exists() and not source.is_symlink():
        source = root / LEGACY_BINDINGS_FILENAME
    path = source.resolve()
    if not path.is_relative_to(root):
        raise BindingsError(f"{source.name} must stay inside Home Assistant's config directory")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BindingsError(
            f"Add your private {BINDINGS_FILENAME} to Home Assistant's config directory first") from None
    except OSError:
        raise BindingsError(f"Could not read private {source.name}") from None
    except UnicodeError:
        raise BindingsError(f"{source.name} must contain valid JSON entity bindings") from None
    if len(text) > 65536:
        raise BindingsError(f"{source.name} is too large for device wiring")
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError):
        raise BindingsError(f"{source.name} must contain valid JSON entity bindings") from None
    return HouseBindings.from_dict(raw)


def example_bindings():
    """Synthetic documentation/test wiring, only used when explicitly requested."""
    return HouseBindings.from_dict({
        "heads": {"bonus": "climate.shared_head", "guest": "climate.guest_head",
                  "primary": "climate.sleep_head", "living": "climate.common_head"},
        "rooms": {"office": ["sensor.office_ambient_a", "sensor.office_ambient_b", "sensor.office_ambient_c"],
                  "entertainment": ["sensor.entertainment_ambient"],
                  "guest": ["sensor.guest_ambient_a", "sensor.guest_ambient_b"],
                  "primary": ["sensor.sleep_ambient_a", "sensor.sleep_ambient_b"],
                  "living": ["sensor.living_ambient_a", "sensor.living_ambient_b"]},
        "fans": {"primary": "fan.sleep_circulation", "guest": "fan.guest_circulation",
                 "office": "fan.office_transfer", "entertainment": "fan.shared_transfer"},
        "heater": "climate.auxiliary_heater",
        "power": {"heater_switch": "switch.auxiliary_heater_power", "battery": "sensor.reserve_battery",
                  "circuit_power": "sensor.shared_circuit_power", "input_power": "sensor.reserve_input_power",
                  "output_power": "sensor.reserve_output_power", "heater_current": "sensor.heater_plug_current",
                  "wall_phase_a_voltage": "sensor.supply_a_voltage", "wall_phase_b_voltage": "sensor.supply_b_voltage"},
    })
