from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import Config, Settings
from .sensors import Reading, Sensors


@dataclass
class Plan:
    mode: str = "off"
    head_targets_f: dict[str, float] = field(default_factory=dict)
    heater_on: bool = False
    auxiliary_fan_only: bool = False
    fans: dict[str, int] = field(default_factory=dict)
    rooms: dict = field(default_factory=dict)
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
        profile = settings.profile(self.config.zones[name], now, self.config.timezone)
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
        heat_edge, cool_edge = max(low, LOW + 1), min(high, HIGH - 1)
        heat_stop = target if low > LOW else LOW + 1.5
        cool_stop = target if high < HIGH else HIGH - 1.5
        heat = self.calls.get((name, "heat"), False)
        cool = self.calls.get((name, "cool"), False)
        heat = (reading.confirmed_low < heat_stop or future_low < LOW + 1) if heat else (reading.confirmed_low < heat_edge or future_low < LOW + 1)
        cool = (reading.confirmed_high > cool_stop or future_high > HIGH - 1) if cool else (reading.confirmed_high > cool_edge or future_high > HIGH - 1)
        self.calls[name, "heat"], self.calls[name, "cool"] = heat, cool
        output = {}
        for mode, active, amount, violation, guard in (
            ("heat", heat, heat_stop - min(reading.confirmed_low, future_low), LOW - reading.confirmed_low, LOW + 1 - future_low),
            ("cool", cool, max(reading.confirmed_high, future_high) - cool_stop, reading.confirmed_high - HIGH, future_high - (HIGH - 1)),
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
        profiles = {key: settings.profile(room, now, self.config.timezone) for key, room in self.config.zones.items()}
        plan.fans = {entity: 0 for entity in self.config.fans.values()}
        demands = {name: self.demand(name, reading, self.band(name, settings, now), profiles[name].priority)
                   for name, reading in rooms.items()}
        for key in list(self.calls):
            if key[0] not in rooms:
                self.calls.pop(key)

        # The auxiliary thermostat uses external room probes, never its own sensor.
        auxiliary = rooms.get(self.config.auxiliary_room)
        heater_available = bool(self.config.heater_entity) and states.get(
            self.config.heater_entity, {}).get("state") not in (None, "unknown", "unavailable")
        if auxiliary and heater_available:
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
                    if name == self.config.auxiliary_room and mode == "heat" and heater_available:
                        continue
                    if demands[name][mode]["active"]:
                        requests.append(demands[name][mode])
                if not requests:
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
        if winner != "off":
            plan.reasons.append(f"{winner}: " + ", ".join(plan.head_targets_f))
        else:
            plan.reasons.append("No eligible shared-system call")
        if plan.heater_on:
            plan.reasons.append("Auxiliary heating using external room probes")
        elif plan.auxiliary_fan_only:
            plan.reasons.append("Auxiliary heater circulating air in fan-only mode")
        return plan
