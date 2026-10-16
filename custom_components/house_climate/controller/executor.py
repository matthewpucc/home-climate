from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .policy import Plan
from .sensors import from_f


class ControlFault(RuntimeError):
    pass


@dataclass
class Pending:
    command: tuple
    since: float


class Executor:
    """Nonblocking, acknowledged all-off barrier before any shared mode change."""

    def __init__(self, config: Config, ha, climate_unit: str):
        self.config, self.ha, self.unit = config, ha, climate_unit
        self.phase = "new"
        self.direction = None
        self.all_off_since: float | None = None
        self.pending: dict[str, Pending] = {}
        self.stop_started: float | None = None

    def ensure(self, entity, service, data, matches, states, now):
        command = (service, tuple(sorted(data.items())))
        pending = self.pending.get(entity)
        if matches(states.get(entity, {})) and (pending is None or pending.command == command):
            self.pending.pop(entity, None)
            return
        if pending and pending.command == command:
            if now - pending.since >= self.config.command_timeout_seconds:
                raise ControlFault(f"Command acknowledgement timed out: {entity}")
            return
        self.ha.service(entity.split(".")[0], service, entity_id=entity, **data)
        self.pending[entity] = Pending(command, now)

    def off(self, entity, states, now):
        self.ensure(entity, "set_hvac_mode", {"hvac_mode": "off"}, lambda s: s.get("state") == "off", states, now)

    def target(self, entity: str, fahrenheit: float, states: dict, heater: bool = False) -> float:
        attrs = states.get(entity, {}).get("attributes", {})
        try:
            minimum, maximum = float(attrs["min_temp"]), float(attrs["max_temp"])
            step = float(attrs.get("target_temp_step", .5 if self.unit in ("C", "°C") else 1))
            value = from_f(fahrenheit, self.unit)
            if not all(math.isfinite(n) for n in (minimum, maximum, step, value)) or minimum > maximum or step <= 0:
                raise ValueError
            if heater and not minimum <= value <= maximum:
                raise ControlFault("Configured high heater setpoint is outside its supported range")
            value = max(minimum, min(maximum, minimum + round((value - minimum) / step) * step))
            return round(value, 3)
        except (KeyError, ValueError, TypeError):
            raise ControlFault(f"Missing/invalid climate temperature range: {entity}") from None

    def climate(self, entity, mode, target_f, states, now, heater=False):
        state = states.get(entity, {})
        if mode not in state.get("attributes", {}).get("hvac_modes", []):
            raise ControlFault(f"{entity} does not expose mode {mode}")
        target = self.target(entity, target_f, states, heater)

        def matches(observed):
            try:
                return observed.get("state") == mode and abs(float(observed.get("attributes", {}).get("temperature")) - target) < .05
            except (ValueError, TypeError):
                return False

        self.ensure(entity, "set_temperature", {"hvac_mode": mode, "temperature": target}, matches, states, now)

    def begin_stop(self, states, now):
        self.phase, self.direction, self.all_off_since = "stopping", None, None
        self.stop_started = now
        for entity in self.config.heads.values():
            self.off(entity, states, now)

    def auxiliary(self, plan: Plan, states: dict, now: float):
        """One owner selects heat, fan-only, or off for the same physical heater."""
        entity = self.config.heater_entity
        if entity is None:
            return
        if plan.heater_on and plan.auxiliary_fan_only:
            raise ControlFault("The auxiliary heater cannot request heat and fan-only together")
        if plan.heater_on:
            self.climate(entity, "heat", self.config.heater_high_setpoint_f, states, now, heater=True)
        elif plan.auxiliary_fan_only:
            state = states.get(entity, {})
            if "fan_only" not in state.get("attributes", {}).get("hvac_modes", []):
                plan.auxiliary_fan_only = False
                plan.warnings.append("Auxiliary fan-only mode unsupported; requesting off")
                self.off(entity, states, now)
                return
            pending = self.pending.get(entity)
            fan_command = ("set_hvac_mode", (("hvac_mode", "fan_only"),))
            # Supersede even an unacknowledged heat command whose old snapshot still
            # says off. Confirm off on a subsequent poll before enabling fan-only.
            if state.get("state") not in ("off", "fan_only") or (pending and pending.command != fan_command):
                self.off(entity, states, now)
                return
            self.ensure(entity, "set_hvac_mode", {"hvac_mode": "fan_only"},
                        lambda s: s.get("state") == "fan_only" and s.get("attributes", {}).get("hvac_action") != "heating",
                        states, now)
        else:
            self.off(entity, states, now)

    def step(self, plan: Plan, states: dict, now: float):
        # Auxiliary equipment is independent of the shared-system changeover delay.
        self.auxiliary(plan, states, now)
        for entity, percentage in plan.fans.items():
            if percentage:
                attrs = states.get(entity, {}).get("attributes", {})
                step = float(attrs.get("percentage_step", 1))
                if not math.isfinite(step) or step <= 0 or step > 100:
                    raise ControlFault(f"Invalid fan percentage step: {entity}")
                percentage = min(100, max(step, round(percentage / step) * step))
                def matches(s, p=percentage):
                    observed = s.get("attributes", {}).get("percentage")
                    return s.get("state") == "on" and isinstance(observed, (int, float)) and abs(observed - p) < .1
                self.ensure(entity, "set_percentage", {"percentage": percentage}, matches, states, now)
            else:
                self.ensure(entity, "turn_off", {}, lambda s: s.get("state") == "off", states, now)

        if self.phase == "new":
            self.begin_stop(states, now)
            return
        if self.phase == "running":
            incompatible = any(states.get(entity, {}).get("state") not in ("off", self.direction) for entity in self.config.heads.values())
            if plan.mode != self.direction or incompatible:
                self.begin_stop(states, now)
                return
        if self.phase == "stopping":
            all_off = all(states.get(entity, {}).get("state") == "off" and entity not in self.pending for entity in self.config.heads.values())
            # Clear acknowledged off commands before evaluating the barrier on the next tick.
            for entity in self.config.heads.values():
                self.off(entity, states, now)
            if not all_off:
                if now - self.stop_started >= self.config.command_timeout_seconds:
                    raise ControlFault("Not all shared-system heads acknowledged off; activation blocked")
                return
            self.phase, self.all_off_since = "settling", now
        if self.phase == "settling":
            if any(states.get(entity, {}).get("state") != "off" for entity in self.config.heads.values()):
                self.begin_stop(states, now)
                return
            if plan.mode == "off" or now - self.all_off_since < self.config.off_settle_seconds:
                return
            self.phase, self.direction = "running", plan.mode
        if self.phase == "running":
            # Off commands first; every active head is then set to the same direction.
            for key, entity in self.config.heads.items():
                if key not in plan.head_targets_f:
                    self.off(entity, states, now)
            for key, target in plan.head_targets_f.items():
                self.climate(self.config.heads[key], plan.mode, target, states, now)

    def shutdown(self) -> list[str]:
        """Best effort across every device even when one service call fails."""
        failures = []
        for entity in [*([self.config.heater_entity] if self.config.heater_entity else []), *self.config.heads.values(), *self.config.fans.values()]:
            try:
                if entity.startswith("climate."):
                    self.ha.service("climate", "set_hvac_mode", entity_id=entity, hvac_mode="off")
                else:
                    self.ha.service("fan", "turn_off", entity_id=entity)
            except Exception:
                failures.append(entity)
        self.phase, self.direction = "new", None
        self.pending.clear()
        return failures
