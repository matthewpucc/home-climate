"""Slow comfort decisions with fresh, reduction-only safety checks on every tick."""
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from .config import Config, Settings
from .policy import Plan, Policy
from .sensors import Sensors


class DecisionCadence:
    """Cache comfort requests; a maintenance tick may only make them safer.

    State events wake maintenance promptly without advancing the comfort timer.
    Explicit settings/schedule changes and newly confirmed hard-limit breaches
    are the exceptions. Executor mutations never reach the cached policy plan.
    """

    def __init__(self, config: Config, policy=None, clock=None):
        self.config = config
        self.clock = clock or time.monotonic
        self.policy = policy if policy is not None else Policy(config)
        self.sensors = Sensors(config)
        self.cached_plan: Plan | None = None
        self.next_decision = 0.0
        self._signature = None
        self._emergencies = set()
        self._decision_warnings: list[str] = []
        self._safety_warnings: list[str] = []

    @property
    def interval(self):
        return self.config.decision_interval_seconds

    def reset(self):
        self.policy = Policy(self.config)
        self.sensors = Sensors(self.config)
        self.cached_plan = None
        self.next_decision = 0.0
        self._signature = None
        self._emergencies = set()
        self._decision_warnings = []
        self._safety_warnings = []

    def status(self, monotonic_now=None):
        stamp = self.clock() if monotonic_now is None else monotonic_now
        return {
            "decision_remaining_seconds": max(0.0, self.next_decision - stamp) if self.cached_plan else 0.0,
            "decision_interval_seconds": self.interval,
        }

    def _settings_signature(self, settings, now):
        profiles = []
        for name, room in self.config.zones.items():
            profile = asdict(settings.profile(room, now, self.config.timezone, name))
            for key in ("target_f", "half_band_f", "fan_percentage", "low_f", "high_f"):
                if profile[key] is not None:
                    profile[key] = settings.value(profile[key])
            profiles.append((name, tuple(sorted(profile.items()))))
        return (tuple(profiles), tuple(sorted(settings.numbers.items())),
                tuple(sorted((key, value.isoformat()) for key, value in settings.times.items())),
                tuple(sorted(settings.toggles.items())))

    def _warning(self, message):
        if message not in self._safety_warnings:
            self._safety_warnings.append(message)

    def _inhibit(self, head, reason):
        self.cached_plan.head_targets_f.pop(head, None)
        getattr(self.cached_plan, "head_call_targets_f", {}).pop(head, None)
        self.cached_plan.inhibited_heads[head] = reason
        self._warning(f"{head}: {reason}; waiting for next comfort decision")

    def _trim(self, states, rooms):
        plan = self.cached_plan
        low, high = self.config.low_f, self.config.high_f
        for head, entity in self.config.heads.items():
            names = [name for name, room in self.config.zones.items() if room.head == head]
            if any(name not in rooms for name in names):
                self._inhibit(head, "temperature unavailable in an attached room")
                continue
            if plan.mode == "cool" and any(rooms[name].low <= low + .25 for name in names):
                self._inhibit(head, "cooling blocked by opposite temperature limit")
            elif plan.mode == "heat" and any(rooms[name].high >= high - .25 for name in names):
                self._inhibit(head, "heating blocked by opposite temperature limit")
            elif (plan.mode in ("heat", "cool")
                  and getattr(self.config, f"head_{plan.mode}_call_offset_f", 0) > 0):
                # A biased device setpoint deliberately keeps its internal
                # thermostat calling. External probes must therefore stop it at
                # the opposite comfort edge even between slow decisions.
                for name in names:
                    target = plan.room_targets.get(name, {})
                    boundary = target.get("low_f" if plan.mode == "cool" else "high_f")
                    temperature = rooms[name].temperature
                    if boundary is not None and (temperature <= boundary if plan.mode == "cool" else temperature >= boundary):
                        self._inhibit(head, f"{plan.mode} stopped at opposite room comfort limit")
                        break
            modes = states.get(entity, {}).get("attributes", {}).get("hvac_modes") or []
            if plan.mode in ("heat", "cool") and modes and plan.mode not in modes:
                self._inhibit(head, f"{plan.mode} unavailable (HA modes: {', '.join(modes)})")

        auxiliary = rooms.get(self.config.auxiliary_room)
        if self.config.heater_entity:
            cutoff = min(self.config.heater_cutoff_f,
                         plan.room_targets.get(self.config.auxiliary_room, {}).get("high_f", self.config.heater_cutoff_f))
            if plan.heater_on and (not auxiliary or auxiliary.high >= cutoff):
                plan.heater_on = False
                self.policy.heater_on = False
                self._warning("Auxiliary heat stopped by fresh room temperature/probe check; waiting for next comfort decision")
            if plan.auxiliary_fan_only and (not auxiliary or auxiliary.low <= low + .25):
                plan.auxiliary_fan_only = False
                self._warning("Auxiliary circulation stopped by fresh room temperature/probe check; waiting for next comfort decision")

        for fan in self.config.fan_controls.values():
            room = rooms.get(fan.room)
            unsafe = not room
            if fan.strategy in ("cooling", "transfer"):
                unsafe = unsafe or room.low <= low + .25
            if fan.strategy == "transfer":
                unsafe = unsafe or fan.source_room not in rooms
            if unsafe and plan.fans.get(fan.entity_id, 0):
                plan.fans[fan.entity_id] = 0
                self._warning(f"Circulation fan {fan.entity_id}: stopped by fresh room temperature/probe check; waiting for next comfort decision")

    def decide(self, states: dict, now: datetime, settings: Settings, climate_unit="°F", *, monotonic_now=None):
        stamp = self.clock() if monotonic_now is None else monotonic_now
        rooms, sensor_warnings = self.sensors.read(states, now, climate_unit)
        signature = self._settings_signature(settings, now)
        emergencies = {(name, direction) for name, room in rooms.items()
                       for direction, outside in (("heat", room.confirmed_low < self.config.low_f),
                                                  ("cool", room.confirmed_high > self.config.high_f))
                       if outside}
        if (self.cached_plan is None or stamp >= self.next_decision or signature != self._signature
                or emergencies - self._emergencies):
            # Carry maintenance evidence into the policy's separate sensor reader.
            # In particular, persistent-outlier confirmation must not restart at
            # every slow decision or wait another interval after a safety breach.
            self.policy.sensors = deepcopy(self.sensors)
            self.cached_plan = self.policy.decide(states, now, settings, climate_unit)
            self.next_decision = stamp + self.interval
            self._signature = signature
            self._decision_warnings = [warning for warning in self.cached_plan.warnings if warning not in sensor_warnings]
            self._safety_warnings = []
        self._emergencies = emergencies
        self._trim(states, rooms)
        plan = deepcopy(self.cached_plan)
        plan.rooms = {name: asdict(room) for name, room in rooms.items()}
        plan.warnings = list(dict.fromkeys(self._decision_warnings + sensor_warnings + self._safety_warnings))
        return plan
