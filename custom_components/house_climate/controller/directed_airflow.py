"""Demand-directed intent, independent of HVAC arbitration and speed execution."""
from __future__ import annotations

from dataclasses import asdict
import math


class DirectedAirflow:
    def __init__(self, policy):
        self.policy = policy
        self.selected = {}
        self.boosting = {}
        self.changed_at = {}
        self.candidates = {}

    def useful(self, profile, plan, rooms, *, retained=False, boosted=False):
        policy = self.policy
        destination, source = rooms.get(profile.destination), rooms.get(profile.source)
        target = plan.room_targets.get(profile.destination)
        if destination is None or source is None or target is None:
            return None
        band = target["low_f"], target["high_f"], target["target_f"]
        demands = policy.demand(profile.destination, destination, band, target["priority"], update=False)
        modes = [mode for mode in ("heat", "cool")
                 if target.get(f"{mode}_requested") and demands[mode]["active"]]
        if not modes and target["comfort_enabled"]:
            # Aim can serve comfort inside the HVAC deadband. This never
            # creates a heat-pump call, and does not apply to limits-only time.
            delta = destination.temperature - target["target_f"]
            if abs(delta) > (.25 if retained else .5):
                modes = ["cool" if delta > 0 else "heat"]
        if len(modes) != 1:
            return None
        mode = modes[0]
        stop = policy.stop_targets(band, comfort_enabled=target["comfort_enabled"])[mode]
        # A forecast/extreme can request HVAC without justifying this room aim.
        if (mode == "heat" and destination.temperature >= stop
                or mode == "cool" and destination.temperature <= stop):
            return None
        from .protection_context import limits
        low, high = limits(policy.config, policy.context)
        if ((mode == "cool" and destination.low <= low + .25)
                or (mode == "heat" and destination.high >= high - .25)):
            return None
        head = policy.config.zones[profile.source].head
        if policy.transfer_airflow.opposing_direction(head, destination.temperature, stop):
            return None
        if (head in plan.head_targets_f and plan.mode == mode
                and policy.config.zones[profile.destination].head == head):
            return mode, "Assist selected shared-head output", head, True
        if profile.purpose == "circulation":
            # Cooling airflow is useful for comfort, without claiming that it
            # lowers room temperature or satisfies the HVAC call.
            if mode == "cool":
                return mode, "Local comfort aim at purifier minimum; cooling benefit unmeasured", None, False
            return None
        delta = destination.temperature - source.temperature
        useful = delta > 0 if mode == "cool" else delta < 0
        if useful and abs(delta) > (.25 if boosted else .75):
            return mode, "Useful ambient gradient; thermal benefit unmeasured", None, True
        if mode == "cool" and target["comfort_enabled"]:
            return mode, "Comfort aim at purifier minimum; thermal transfer unproven", None, False
        return None

    def update(self, plan, rooms, now, *, allow_start=True):
        for fan in self.policy.config.fan_controls.values():
            if fan.airflow is None:
                continue
            entity, config = fan.entity_id, fan.airflow
            minimum = math.ceil(fan.purification_minimum)
            previous = self.selected.get(entity)
            choices = {name: result for name, profile in config.profiles.items()
                       if fan.speed_override != 0
                       and (result := self.useful(profile, plan, rooms, retained=name == previous,
                                                 boosted=name == self.boosting.get(entity)))}
            # Keep a still-useful route through ambiguous demand. Without a
            # previous route, equally plausible destinations cause no movement.
            selected = previous if previous in choices else next(iter(choices)) if len(choices) == 1 else None
            reason = "No unambiguous supported direction demand; retain last physical profile at purifier minimum"
            if selected != previous and selected is not None:
                candidate = self.candidates.get(entity)
                if candidate is None or candidate[0] != selected:
                    self.candidates[entity] = selected, now
                candidate = self.candidates[entity]
                elapsed = (now - self.changed_at[entity]).total_seconds() if entity in self.changed_at else math.inf
                confirmed = (now - candidate[1]).total_seconds() >= config.confirmation_seconds
                # The initial route needs no reversal debounce; later changes
                # wait at minimum when the old intent has become invalid.
                if not allow_start or (previous is not None and (elapsed < config.minimum_dwell_seconds or not confirmed)):
                    selected = None
                    reason = "Direction change awaiting confirmation/minimum dwell; purifier minimum retained"
                else:
                    self.selected[entity] = selected
                    self.changed_at[entity] = now
                    self.candidates.pop(entity, None)
            elif selected is None:
                self.candidates.pop(entity, None)
            else:
                self.candidates.pop(entity, None)
            if not allow_start:
                cached = plan.fan_airflow.get(entity, {})
                if selected != cached.get("profile") or cached.get("mode") != (choices.get(selected) or (None,))[0]:
                    selected = None
                    reason = "Direction demand changed; wait for next comfort decision at purifier minimum"
            plan.fans[entity] = minimum
            intent = {"profile": None, "destination": None, "mode": None, "reason": reason,
                      "last_selected_profile": self.selected.get(entity), "limitation": config.limitation,
                      "outlet_control": config.outlet,
                      "physical_benefit_verified": False}
            if selected:
                mode, reason, head, boost = choices[selected]
                profile = config.profiles[selected]
                intent.update(profile=selected, destination=profile.destination, mode=mode, reason=reason,
                              requested=asdict(profile), head=head, boost=boost,
                              provisional=profile.calibration == "provisional")
                if boost:
                    requested = fan.requested_percentage(self.policy._airflow_settings.value(fan.percentage))
                    if not allow_start:
                        # Maintenance is reduction-only, even when the same
                        # profile gains a newly useful ambient gradient.
                        requested = min(requested, cached.get("percentage", minimum))
                    plan.fans[entity] = requested
            intent["percentage"] = plan.fans[entity]
            intent["speed_override"] = fan.speed_override
            if selected and intent.get("boost") and plan.fans[entity] > minimum:
                self.boosting[entity] = selected
            else:
                self.boosting.pop(entity, None)
            plan.fan_airflow[entity] = intent
