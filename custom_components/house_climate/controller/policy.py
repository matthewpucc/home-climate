from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import math

from .config import Config, Settings
from .sensors import Reading, Sensors, from_f, to_f


def native_target(fahrenheit: float, attrs: dict, unit: str) -> float:
    """Bound and quantize a setpoint in the climate entity's native HA unit."""
    minimum, maximum = float(attrs["min_temp"]), float(attrs["max_temp"])
    step = float(attrs.get("target_temp_step", .5 if unit in ("C", "°C") else 1))
    value = from_f(fahrenheit, unit)
    if (not all(math.isfinite(number) for number in (minimum, maximum, step, value))
            or minimum > maximum or step <= 0):
        raise ValueError("Invalid climate temperature range")
    value = max(minimum, min(maximum, minimum + round((value - minimum) / step) * step))
    return round(value, 3)


def call_target_f(config: Config, entity: str, mode: str, comfort: float,
                  states: dict, climate_unit: str) -> float:
    """An equipment call setpoint; room probes still decide when the call ends."""
    attrs = states.get(entity, {}).get("attributes", {})
    offset = config.head_heat_call_offset_f if mode == "heat" else config.head_cool_call_offset_f
    target = comfort
    if offset > 0:
        onboard = attrs.get("current_temperature")
        try:
            if isinstance(onboard, bool):
                raise ValueError("Boolean onboard temperature")
            onboard = to_f(float(onboard), climate_unit)
            if not math.isfinite(onboard) or not -40 <= onboard <= 140:
                raise ValueError("Implausible onboard temperature")
            target = max(comfort, onboard) if mode == "heat" else min(comfort, onboard)
        except (TypeError, ValueError, OverflowError):
            pass
        target += offset if mode == "heat" else -offset
    try:
        return to_f(native_target(target, attrs, climate_unit), climate_unit)
    except (KeyError, TypeError, ValueError, OverflowError):
        # Disconnected/incomplete heads still get an explainable shadow plan.
        # The executor retains its activation-time metadata validation.
        return target


@dataclass
class Plan:
    mode: str = "off"
    head_targets_f: dict[str, float] = field(default_factory=dict)
    head_call_targets_f: dict[str, float] = field(default_factory=dict)
    inhibited_heads: dict[str, str] = field(default_factory=dict)
    heater_on: bool = False
    auxiliary_fan_only: bool = False
    fans: dict[str, int] = field(default_factory=dict)
    fan_head_assists: dict[str, str] = field(default_factory=dict)
    rooms: dict = field(default_factory=dict)
    room_targets: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Policy:
    """Pure decision layer. Never performs I/O or assumes a command succeeded."""

    def __init__(self, config: Config):
        self.config = config
        self.sensors = Sensors(config)
        self.calls: dict[tuple[str, str], bool] = {}
        self.selected_mode = "off"
        self.last_direction: str | None = None
        self.mode_since: datetime | None = None
        self.candidate: tuple[str, datetime] | None = None
        self.heater_on = False
        self.transfer_on: dict[str, bool] = {}

    def band(self, name: str, settings: Settings, now: datetime) -> tuple[float, float, float]:
        profile = settings.profile(self.config.zones[name], now, self.config.timezone, name)
        low, high = self.config.low_f, self.config.high_f
        if not profile.comfort_enabled:
            return low, high, (low + high) / 2
        target = settings.value(profile.target_f) + profile.target_offset_f
        half = settings.value(profile.half_band_f)
        return (max(low, settings.value(profile.low_f) if profile.low_f is not None else target - half),
                min(high, settings.value(profile.high_f) if profile.high_f is not None else target + half), target)

    def demand(self, name: str, reading: Reading, band: tuple, priority: float) -> dict:
        LOW, HIGH = self.config.low_f, self.config.high_f
        low, high, target = band
        drift = reading.slope_f_per_minute * self.config.forecast_minutes
        future_low, future_high = reading.confirmed_low + drift, reading.confirmed_high + drift
        # Comfort follows the fused room estimate. Persistent probe extremes
        # remain independent evidence for system-limit protection below.
        temperature, future_temperature = reading.temperature, reading.temperature + drift
        heat_edge, cool_edge = max(low, LOW + 1), min(high, HIGH - 1)
        heat_stop = target if low > LOW else LOW + 1.5
        cool_stop = target if high < HIGH else HIGH - 1.5
        heat = self.calls.get((name, "heat"), False)
        cool = self.calls.get((name, "cool"), False)
        heat = (temperature < heat_stop or future_low < LOW + 1) if heat else (temperature < heat_edge or future_low < LOW + 1)
        cool = (temperature > cool_stop or future_high > HIGH - 1) if cool else (temperature > cool_edge or future_high > HIGH - 1)
        self.calls[name, "heat"], self.calls[name, "cool"] = heat, cool
        output = {}
        for mode, active, amount, violation, guard in (
            ("heat", heat, heat_stop - min(temperature, future_temperature), LOW - reading.confirmed_low, LOW + 1 - future_low),
            ("cool", cool, max(temperature, future_temperature) - cool_stop, reading.confirmed_high - HIGH, future_high - (HIGH - 1)),
        ):
            amount = max(.05, amount) if active else 0
            output[mode] = {
                "active": active, "target": heat_stop if mode == "heat" else cool_stop,
                "score": (max(0, violation), max(0, guard), priority if active else 0, amount),
            }
        return output

    def decide(self, states: dict, now: datetime, settings: Settings, climate_unit: str = "°F") -> Plan:
        rooms, warnings = self.sensors.read(states, now, climate_unit)
        plan = Plan(rooms={k: asdict(v) for k, v in rooms.items()}, warnings=warnings)
        LOW, HIGH = self.config.low_f, self.config.high_f
        profiles = {key: settings.profile(room, now, self.config.timezone, key) for key, room in self.config.zones.items()}
        plan.fans = {entity: 0 for entity in self.config.fans.values()}
        bands = {name: self.band(name, settings, now) for name in self.config.zones}
        demands = {name: self.demand(name, reading, bands[name], profiles[name].priority)
                   for name, reading in rooms.items()}
        # Report the exact active profile used for this decision. Presentation
        # must not re-evaluate schedules in a different timezone or helper snapshot.
        plan.room_targets = {
            name: {"low_f": band[0], "high_f": band[1], "target_f": band[2],
                   "comfort_enabled": profiles[name].comfort_enabled,
                   "priority": profiles[name].priority,
                   "heat_requested": demands.get(name, {}).get("heat", {}).get("active", False),
                   "cool_requested": demands.get(name, {}).get("cool", {}).get("active", False)}
            for name, band in bands.items()
        }
        for key in list(self.calls):
            if key[0] not in rooms:
                self.calls.pop(key)

        # The auxiliary thermostat uses external room probes, never its own sensor.
        auxiliary = rooms.get(self.config.auxiliary_room)
        heater_available = bool(self.config.heater_entity) and states.get(
            self.config.heater_entity, {}).get("state") not in (None, "unknown", "unavailable")
        heater_modes = states.get(self.config.heater_entity, {}).get("attributes", {}).get("hvac_modes") or []
        heater_can_heat = heater_available and (not heater_modes or {"off", "heat"} <= set(heater_modes))
        if auxiliary and heater_available and not heater_can_heat and demands[self.config.auxiliary_room]["heat"]["active"]:
            plan.warnings.append(
                "Auxiliary heat requested but unavailable (HA modes: " + ", ".join(heater_modes) + ")")
        if auxiliary and heater_can_heat:
            low, high, target = self.band(self.config.auxiliary_room, settings, now)
            if auxiliary.high >= min(high, self.config.heater_cutoff_f):
                self.heater_on = False
            elif auxiliary.temperature < low:
                self.heater_on = True
        else:
            self.heater_on = False
        plan.heater_on = self.heater_on
        fan_enabled = settings.toggles["auxiliary_fan_enabled"]
        if (auxiliary and heater_available and not plan.heater_on and fan_enabled
                and auxiliary.low > LOW + .25 and demands[self.config.auxiliary_room]["cool"]["active"]):
            if "fan_only" in states[self.config.heater_entity].get("attributes", {}).get("hvac_modes", []):
                plan.auxiliary_fan_only = True
            else:
                plan.warnings.append("Auxiliary heater does not expose fan_only; circulation skipped")

        for key, fan in self.config.fan_controls.items():
            room = rooms.get(fan.room)
            if not room:
                self.transfer_on.pop(key, None)
                continue
            demand = demands[fan.room]
            active = False
            percentage = settings.value(fan.percentage)
            if fan.strategy == "schedule":
                percentage = settings.value(profiles[fan.room].fan_percentage)
                active = percentage > 0
            elif fan.strategy == "always":
                active = True
            elif fan.strategy == "cooling":
                active = demand["cool"]["active"] and room.low > LOW + .25
            elif fan.strategy == "conditioning":
                active = demand["heat"]["active"] or demand["cool"]["active"]
            elif fan.strategy == "transfer":
                source = rooms.get(fan.source_room)
                if source:
                    delta = room.temperature - source.temperature
                    target = self.band(fan.room, settings, now)[2]
                    useful = (room.temperature > target and delta > 0) or (room.temperature < target and delta < 0)
                    active = useful and abs(delta) > (.25 if self.transfer_on.get(key) else .75)
                self.transfer_on[key] = active
            plan.fans[fan.entity_id] = round(percentage) if active else 0

        eligible = {"heat": {}, "cool": {}}
        scores = {"heat": (0., 0., 0., 0.), "cool": (0., 0., 0., 0.)}
        for head in self.config.heads:
            names = [name for name, zone in self.config.zones.items() if zone.head == head]
            if any(name not in rooms for name in names):
                continue
            for mode in ("heat", "cool"):
                requests = []
                for name in names:
                    if name == self.config.auxiliary_room and mode == "heat" and heater_can_heat:
                        continue
                    if demands[name][mode]["active"]:
                        requests.append(demands[name][mode])
                if not requests:
                    continue
                modes = states.get(self.config.heads[head], {}).get("attributes", {}).get("hvac_modes") or []
                if modes and mode not in modes:
                    labels = ", ".join(self.config.zones[name].name or name for name in names)
                    plan.warnings.append(
                        f"{labels}: {mode} requested but unavailable (HA modes: {', '.join(modes)})")
                    continue
                # Immediate raw extremes stop aggravating a potentially unsafe area.
                blocked = any(rooms[name].low <= LOW + .25 for name in names) if mode == "cool" else any(rooms[name].high >= HIGH - .25 for name in names)
                if blocked:
                    plan.warnings.append(f"{head}: {mode} blocked by opposite temperature limit; airflow/capacity conflict")
                    continue
                chosen = max(requests, key=lambda request: request["score"])
                eligible[mode][head] = chosen["target"]
                # Worst room wins in each safety/priority tier; room count cannot outvote a limit.
                scores[mode] = tuple(max(a, b) for a, b in zip(scores[mode], chosen["score"]))

        available = [mode for mode in ("heat", "cool") if eligible[mode]]
        winner = max(available, key=lambda mode: (scores[mode], mode == self.selected_mode)) if available else "off"
        if all(eligible.values()):
            plan.warnings.append("Simultaneous heat/cool demand: shared equipment cannot satisfy both at once")
        if winner != "off" and self.last_direction and winner != self.last_direction:
            if not self.candidate or self.candidate[0] != winner:
                self.candidate = (winner, now)
            held = (now - self.mode_since).total_seconds() if self.mode_since else float("inf")
            confirmed = (now - self.candidate[1]).total_seconds() >= self.config.switch_confirmation_seconds
            # Actual out-of-range conditions may bypass comfort dwell, but never hardware off-time.
            emergency = scores[winner][0] > scores[self.last_direction][0]
            old_score = scores[self.last_direction]
            # For comfort-only switches require a meaningful advantage, not tiny fluctuations.
            advantage = scores[winner][:3] > old_score[:3] or scores[winner][3] > old_score[3] + .5
            if not emergency and (held < settings["mode_hold_minutes"] * 60 or not confirmed or not advantage):
                winner = self.last_direction if eligible[self.last_direction] else "off"
                plan.reasons.append("Holding shared direction to avoid frequent mode changes")
        else:
            self.candidate = None
        if winner != "off" and winner != self.last_direction:
            self.last_direction, self.mode_since = winner, now
            self.candidate = None
        self.selected_mode = winner
        plan.mode = winner
        plan.head_targets_f = eligible.get(winner, {})
        plan.head_call_targets_f = {
            key: call_target_f(self.config, self.config.heads[key], winner, target, states, climate_unit)
            for key, target in plan.head_targets_f.items()
        }
        # A transfer fan can distribute conditioned air before the source
        # area's ambient probe reflects the head's output. Only assist a head
        # that survived the complete shared-mode arbitration and safety checks.
        for key, fan in self.config.fan_controls.items():
            if fan.strategy != "transfer":
                continue
            destination, source = rooms.get(fan.room), rooms.get(fan.source_room)
            if not destination or not source:
                continue
            head = self.config.zones[fan.room].head
            if (self.config.zones[fan.source_room].head != head
                    or head not in plan.head_targets_f):
                continue
            target = bands[fan.room][2]
            assists = ((winner == "cool" and destination.temperature > target)
                       or (winner == "heat" and destination.temperature < target))
            if assists:
                plan.fans[fan.entity_id] = round(settings.value(fan.percentage))
                if plan.fans[fan.entity_id]:
                    plan.fan_head_assists[fan.entity_id] = head
                self.transfer_on[key] = True
        if winner != "off":
            plan.reasons.append(f"{winner}: " + ", ".join(plan.head_targets_f))
        else:
            plan.reasons.append("No eligible shared-system call")
        if plan.heater_on:
            plan.reasons.append("Auxiliary heating using external room probes")
        elif plan.auxiliary_fan_only:
            plan.reasons.append("Auxiliary heater circulating air in fan-only mode")
        return plan
