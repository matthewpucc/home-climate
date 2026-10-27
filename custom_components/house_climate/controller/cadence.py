"""Slow comfort decisions with fresh, reduction-only safety checks on every tick."""
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from .config import Config, Settings
from .policy import Plan, Policy
from .sensors import Sensors
from .temperature_guard import heating_cutoff_reached
from .head_guard import protection_requested


class DecisionCadence:
    """Cache comfort requests; a maintenance tick may only make them safer.

    State events wake maintenance promptly without advancing the comfort timer.
    Explicit settings/schedule changes and newly confirmed house-protection conditions
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
        self.cached_plan.head_protections.pop(head, None)
        self.cached_plan.inhibited_heads[head] = reason
        self._safety_warnings = [warning for warning in self._safety_warnings
                                 if not warning.startswith(f"{head}: ")]
        self._warning(f"{head}: {reason}; waiting for next comfort decision")

    def _trim(self, states, rooms, now, climate_unit="°F"):
        plan = self.cached_plan
        low, high = self.config.low_f, self.config.high_f
        guard = self.policy.head_guard
        guard.observe(rooms, plan.room_targets, states)
        for head, entity in self.config.heads.items():
            names = [name for name, room in self.config.zones.items() if room.head == head]
            requested = {mode for mode in ("heat", "cool")
                         if any(plan.room_targets.get(name, {}).get(f"{mode}_requested")
                                for name in names)}
            if (plan.mode in ("heat", "cool")
                    and (head in plan.head_targets_f or head in plan.inhibited_heads)):
                requested.add(plan.mode)
            details = {}
            for mode in requested:
                detail = guard.constraint(head, mode, rooms, plan.room_targets, states)
                if detail:
                    details[mode] = detail
            if details:
                plan.head_constraints[head] = details
            else:
                plan.head_constraints.pop(head, None)
            if plan.mode in ("heat", "cool") and (detail := details.get(plan.mode)):
                self._inhibit(head, detail["reason"])
            protection = guard.protection(head, plan.mode, rooms, plan.room_targets)
            if (head in plan.head_targets_f and states.get(entity, {}).get("state") != plan.mode
                    and not protection and any((name, plan.mode) in self.policy.forecast_pending for name in names)):
                # Revalidate unstarted cached requests before minimum-Off or
                # delayed dispatch can turn an obsolete forecast into a call.
                needed = False
                for name in names:
                    if name not in rooms or name not in plan.room_targets:
                        continue
                    target = plan.room_targets[name]
                    fresh = self.policy.demand(name, rooms[name],
                        (target["low_f"], target["high_f"], target["target_f"]),
                        target["priority"], running_modes=set(), update=False)
                    if fresh.get(plan.mode, {}).get("active"):
                        needed = True
                    elif (name, plan.mode) in self.policy.forecast_pending:
                        self.policy.forecast_pending.discard((name, plan.mode))
                        self.policy.calls[name, plan.mode] = False
                        target[f"{plan.mode}_requested"] = False
                if not needed:
                    self._inhibit(head, f"Pending {plan.mode} request cleared before activation")
            if protection and head in plan.head_targets_f:
                plan.head_protections[head] = protection
            else:
                plan.head_protections.pop(head, None)
            modes = states.get(entity, {}).get("attributes", {}).get("hvac_modes") or []
            unmet = {}
            for mode in requested:
                if request := self.policy.unavailable_request(
                        head, mode, rooms, plan.room_targets, states, climate_unit):
                    unmet[mode] = request
            if unmet:
                plan.unavailable_mode_requests[head] = unmet
            else:
                plan.unavailable_mode_requests.pop(head, None)
            if plan.mode in ("heat", "cool") and modes and plan.mode not in modes:
                self._inhibit(head, f"{plan.mode} unavailable (HA modes: {', '.join(modes)})")

        auxiliary = rooms.get(self.config.auxiliary_room)
        if self.config.heater_entity:
            cutoff = min(self.config.heater_cutoff_f,
                         plan.room_targets.get(self.config.auxiliary_room, {}).get("high_f", self.config.heater_cutoff_f))
            if plan.heater_on and (not auxiliary or heating_cutoff_reached(auxiliary, cutoff, high)):
                plan.heater_on = False
                self.policy.heater_on = False
                self._warning("Auxiliary heat stopped by fresh room temperature/probe check; waiting for next comfort decision")
            if plan.auxiliary_fan_only and (not auxiliary or auxiliary.low <= low + .25):
                plan.auxiliary_fan_only = False
                self._warning("Auxiliary circulation stopped by fresh room temperature/probe check; waiting for next comfort decision")

        for key, fan in self.config.fan_controls.items():
            room = rooms.get(fan.room)
            unsafe = not room
            if fan.strategy in ("cooling", "transfer"):
                unsafe = unsafe or room.low <= low + .25
            if fan.strategy == "transfer":
                unsafe = unsafe or fan.source_room not in rooms
            assist_head = plan.fan_head_assists.get(fan.entity_id)
            if unsafe and plan.fans.get(fan.entity_id, 0):
                plan.fans[fan.entity_id] = 0
                if assist_head:
                    plan.fan_head_assists.pop(fan.entity_id, None)
                    self.policy.transfer_on[key] = False
                self._warning(f"Circulation fan {fan.entity_id}: stopped by fresh room temperature/probe check; waiting for next comfort decision")
                continue
            if assist_head and assist_head not in plan.head_targets_f:
                # Assistance depends on its selected head. Ambient transfer
                # can still help independently, using the already-on threshold.
                source = rooms.get(fan.source_room)
                target = plan.room_targets.get(fan.room, {}).get("target_f")
                useful = False
                if not unsafe and source and target is not None:
                    delta = room.temperature - source.temperature
                    useful = (((room.temperature > target and delta > 0)
                               or (room.temperature < target and delta < 0))
                              and abs(delta) > .25)
                plan.fan_head_assists.pop(fan.entity_id, None)
                if not useful and plan.fans.get(fan.entity_id, 0):
                    plan.fans[fan.entity_id] = 0
                    self.policy.transfer_on[key] = False
                    self._warning(f"Circulation fan {fan.entity_id}: assisted head is inhibited and ambient transfer is not useful; waiting for next comfort decision")
        # Fresh head feedback can reveal conditioned air even when the ambient
        # gradient still looks useful. Maintenance only removes cached fan calls.
        prior_warnings = set(plan.warnings)
        self.policy.trim_transfer_fans(plan, states, rooms, now)
        for warning in plan.warnings:
            if warning not in prior_warnings:
                self._warning(warning)

    def decide(self, states: dict, now: datetime, settings: Settings, climate_unit="°F", *, monotonic_now=None):
        stamp = self.clock() if monotonic_now is None else monotonic_now
        bands = {name: self.policy.band(name, settings, now) for name in self.config.zones}
        targets = {name: self.policy.stop_targets(band) for name, band in bands.items()}
        rooms, sensor_warnings = self.sensors.read(states, now, climate_unit,
                                                  targets=targets, calls=self.policy.calls,
                                                  head_requests=self.policy.head_requests)
        # A queued forecast can become a genuine comfort/house demand between
        # slow decisions. Preserve that stronger provenance and its ordinary
        # target-reaching hysteresis if the original forecast later clears.
        for name, mode in list(self.policy.forecast_pending):
            if name in rooms and self.policy.actual_demand(rooms[name], bands[name], mode):
                self.policy.forecast_pending.discard((name, mode))
        signature = self._settings_signature(settings, now)
        emergencies = {(name, direction) for name, room in rooms.items()
                       for direction in ("heat", "cool")
                       if protection_requested(self.config, room, direction)}
        # Loss of a source is not evidence that its previous danger resolved.
        # Restore of that same known condition waits for the normal decision;
        # a newly dangerous valid reading still gets prompt protection priority.
        emergencies.update(condition for condition in self._emergencies
                           if condition[0] not in rooms)
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
        self._trim(states, rooms, now, climate_unit)
        self.policy.head_requests = {head: self.cached_plan.mode for head in self.cached_plan.head_targets_f}
        plan = deepcopy(self.cached_plan)
        plan.rooms = {name: asdict(room) for name, room in rooms.items()}
        from .recovery_inputs import signature
        plan.recovery_inputs = signature(self.config, states, now, settings, climate_unit)
        plan.warnings = list(dict.fromkeys(self._decision_warnings + sensor_warnings + self._safety_warnings))
        return plan
