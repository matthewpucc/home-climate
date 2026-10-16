from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .ha import HAError
from .policy import Plan
from .sensors import from_f


class ControlFault(RuntimeError):
    pass


class CommandTimeout(ControlFault):
    """A device has not confirmed a command; its physical state is uncertain."""


@dataclass
class Pending:
    command: tuple
    since: float
    baseline: tuple | None = None
    fresh: bool = False


@dataclass
class FanRetry:
    command: tuple | None
    message: str
    retry_at: float
    attempts: int


class Executor:
    """Nonblocking, acknowledged all-off barrier before any shared mode change."""

    def __init__(self, config: Config, ha, climate_unit: str):
        self.config, self.ha, self.unit = config, ha, climate_unit
        self.phase = "new"
        self.direction = None
        self.all_off_since: float | None = None
        self.pending: dict[str, Pending] = {}
        self.stop_started: float | None = None
        self.fan_failures: dict[str, FanRetry] = {}
        self.device_issues: dict[str, str] = {}

    @staticmethod
    def available(state):
        return state.get("state") not in (None, "unknown", "unavailable")

    @classmethod
    def ready(cls, state):
        return cls.available(state) and bool(state.get("attributes", {}).get("hvac_modes"))

    @staticmethod
    def marker(state):
        attrs = state.get("attributes", {})
        return (state.get("state"), attrs.get("temperature"), attrs.get("hvac_action"),
                tuple(attrs.get("hvac_modes") or ()),
                state.get("last_reported"), state.get("last_updated"), state.get("last_changed"))

    @staticmethod
    def reports_off(state):
        return (state.get("state") == "off"
                and state.get("attributes", {}).get("hvac_action") not in ("heating", "cooling"))

    @property
    def waiting_devices(self):
        return sorted(self.device_issues)

    def ensure(self, entity, service, data, matches, states, now, *, force=False, fresh=False):
        command = (service, tuple(sorted(data.items())))
        pending = self.pending.get(entity)
        observed = states.get(entity, {})
        acknowledged = matches(observed) and (
            pending is None or not pending.fresh or self.marker(observed) != pending.baseline)
        if not force and acknowledged and (pending is None or pending.command == command):
            self.pending.pop(entity, None)
            return True
        if not force and pending and pending.command == command:
            if now - pending.since >= self.config.command_timeout_seconds:
                raise CommandTimeout(f"Command acknowledgement timed out: {entity}")
            return False
        # A transport exception may occur after dispatch. Preserve the command
        # before calling HA so a later off supersedes the uncertain activation.
        self.pending[entity] = Pending(command, now, self.marker(observed), fresh)
        self.ha.service(entity.split(".")[0], service, entity_id=entity, **data)
        return False

    def _issue(self, entity, reason):
        self.device_issues[entity] = reason

    def _recovery_off(self, entity, states, now):
        command = ("set_hvac_mode", (("hvac_mode", "off"),))
        pending = self.pending.get(entity)
        observed = states.get(entity, {})
        if (pending and pending.command == command and self.ready(observed) and self.reports_off(observed)
                and self.marker(observed) != pending.baseline):
            self.pending.pop(entity, None)
            self.device_issues.pop(entity, None)
            return True
        # Off retries stay prompt but do not flood a disconnected integration.
        interval = min(15, self.config.poll_seconds)
        if pending and pending.command == command and now - pending.since < interval:
            return False
        try:
            self.ensure(entity, "set_hvac_mode", {"hvac_mode": "off"}, self.reports_off,
                        states, now, force=True, fresh=True)
        except HAError as exc:
            self._issue(entity, f"{exc}; off acknowledgement required")
        return False

    def _climate_ensure(self, entity, service, data, matches, states, now, *, fresh=False):
        try:
            return self.ensure(entity, service, data, matches, states, now, fresh=fresh)
        except (CommandTimeout, HAError) as exc:
            self._issue(entity, f"{exc}; off acknowledgement required")
            pending = self.pending.get(entity)
            if pending and pending.command[0] == "set_hvac_mode":
                pending.fresh = True
            return False

    def off(self, entity, states, now):
        observed = states.get(entity, {})
        if not self.available(observed):
            self._issue(entity, "Entity unavailable; off acknowledgement required")
        elif not self.ready(observed):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required")
        if entity in self.device_issues:
            return self._recovery_off(entity, states, now)
        pending = self.pending.get(entity)
        command = ("set_hvac_mode", (("hvac_mode", "off"),))
        return self._climate_ensure(
            entity, "set_hvac_mode", {"hvac_mode": "off"}, self.reports_off, states, now,
            fresh=bool(pending and pending.command != command))

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
        if not self.available(state):
            self._issue(entity, "Entity unavailable; off acknowledgement required")
        elif not self.ready(state):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required")
        if entity in self.device_issues:
            self._recovery_off(entity, states, now)
            return
        if mode not in state.get("attributes", {}).get("hvac_modes", []):
            raise ControlFault(f"{entity} does not expose mode {mode}")
        target = self.target(entity, target_f, states, heater)

        def matches(observed):
            try:
                return observed.get("state") == mode and abs(float(observed.get("attributes", {}).get("temperature")) - target) < .05
            except (ValueError, TypeError):
                return False

        self._climate_ensure(entity, "set_temperature", {"hvac_mode": mode, "temperature": target}, matches, states, now)

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
        if not self.available(states.get(entity, {})):
            self._issue(entity, "Entity unavailable; off acknowledgement required")
        elif not self.ready(states.get(entity, {})):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required")
        if entity in self.device_issues:
            self._recovery_off(entity, states, now)
            return
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
            self._climate_ensure(entity, "set_hvac_mode", {"hvac_mode": "fan_only"},
                        lambda s: s.get("state") == "fan_only" and s.get("attributes", {}).get("hvac_action") != "heating",
                        states, now)
        else:
            self.off(entity, states, now)

    def fan(self, entity, percentage, plan, states, now):
        """Circulation is optional: retain a visible issue and retry one fan."""
        failure = self.fan_failures.get(entity)
        command = None
        try:
            state = states.get(entity, {})
            attrs = state.get("attributes", {})
            if percentage and state.get("state") not in ("on", "off"):
                raise ControlFault("Fan unavailable")
            if percentage:
                if not isinstance(attrs.get("supported_features"), int) or not attrs["supported_features"] & 1:
                    raise ControlFault("Fan lacks percentage control")
                try:
                    step = float(attrs.get("percentage_step", 1))
                    if not math.isfinite(step) or not 0 < step <= 100:
                        raise ValueError
                except (ValueError, TypeError, OverflowError):
                    raise ControlFault("Invalid fan percentage step") from None
                # HA's ordered/ranged speed converters use floored integer
                # percentages. E.g. a 3-speed fan's middle speed is 66, not 67.
                rung = max(1, round(percentage / step))
                target = min(100, max(1, math.floor(rung * step + 1e-7)))
                tolerance = min(1.0, step / 2)
                service, data = "set_percentage", {"percentage": target}

                def matches(observed):
                    value = observed.get("attributes", {}).get("percentage")
                    return (observed.get("state") == "on"
                            and isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value) and abs(value - target) <= tolerance)
            else:
                service, data = "turn_off", {}
                def matches(observed):
                    return observed.get("state") == "off"

            command = (service, tuple(sorted(data.items())))
            pending = self.pending.get(entity)
            # Supersede even a timed-out activation whose old snapshot says off.
            urgent_off = failure and service == "turn_off" and failure.command != command
            if not urgent_off and matches(state) and (pending is None or pending.command == command):
                self.pending.pop(entity, None)
                self.fan_failures.pop(entity, None)
                return
            # A newly requested off bypasses the failed activation's backoff.
            if failure and now < failure.retry_at and not urgent_off:
                plan.warnings.append(f"Circulation fan {entity}: {failure.message}; retrying independently")
                return
            self.ensure(entity, service, data, matches, states, now, force=bool(urgent_off))
            if urgent_off:
                failure.command, failure.retry_at = command, now
        except (ControlFault, HAError) as exc:
            if failure and now < failure.retry_at and command in (None, failure.command):
                plan.warnings.append(f"Circulation fan {entity}: {failure.message}; retrying independently")
                return
            self.pending.pop(entity, None)
            attempts = failure.attempts + 1 if failure else 1
            delay = min(300, max(15, self.config.poll_seconds, self.config.command_timeout_seconds)
                        * 2 ** min(attempts - 1, 5))
            observed = states.get(entity, {})
            requested = dict(command[1])["percentage"] if command and command[0] == "set_percentage" else percentage
            target = f"{requested}%" if percentage else "off"
            reason = str(exc).removesuffix(f": {entity}")
            message = (f"{reason}; requested {target}, reported {observed.get('state')} "
                       f"({observed.get('attributes', {}).get('percentage')}%)")
            failure = FanRetry(command, message, now + delay, attempts)
            self.fan_failures[entity] = failure
        if failure:
            plan.warnings.append(f"Circulation fan {entity}: {failure.message}; retrying independently")

    def step(self, plan: Plan, states: dict, now: float):
        # Critical climate commands run before optional circulation services.
        self.auxiliary(plan, states, now)
        self.shared(plan, states, now)
        for entity, percentage in plan.fans.items():
            self.fan(entity, percentage, plan, states, now)
        for entity, issue in self.device_issues.items():
            warning = f"Climate device {entity}: {issue}; retrying independently"
            if warning not in plan.warnings:
                plan.warnings.append(warning)

    def shared(self, plan: Plan, states: dict, now: float):
        if self.phase == "new":
            self.begin_stop(states, now)
            return
        if self.phase == "running":
            incompatible = any(self.available(states.get(entity, {}))
                               and states[entity]["state"] not in ("off", self.direction)
                               for entity in self.config.heads.values())
            if plan.mode != self.direction or incompatible:
                self.begin_stop(states, now)
                return
        if self.phase == "stopping":
            all_off = all(self.ready(states.get(entity, {})) and self.reports_off(states.get(entity, {}))
                          and entity not in self.pending and entity not in self.device_issues
                          for entity in self.config.heads.values())
            # Clear acknowledged off commands before evaluating the barrier on the next tick.
            for entity in self.config.heads.values():
                self.off(entity, states, now)
            if not all_off:
                if now - self.stop_started >= self.config.command_timeout_seconds:
                    plan.warnings.append("Waiting for shared-system heads to acknowledge off; activation blocked")
                return
            self.phase, self.all_off_since = "settling", now
        if self.phase == "settling":
            if any(not self.ready(states.get(entity, {})) or not self.reports_off(states.get(entity, {}))
                   for entity in self.config.heads.values()):
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
        self.fan_failures.clear()
        self.device_issues.clear()
        for entity in failures:
            if entity in self.config.climate_entities:
                self._issue(entity, "Shutdown off command failed; off acknowledgement required")
        return failures
