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
    receipt: bool = False


@dataclass
class ClimateRecovery:
    baseline: tuple
    reason: str
    off_receipt: bool = False
    last_attempt: float | None = None


@dataclass
class FanRetry:
    command: tuple | None
    message: str
    retry_at: float
    attempts: int


@dataclass
class HeadCycle:
    mode: str | None = None
    run_since: float | None = None
    off_since: float | None = None
    target_f: float | None = None
    target_native: float | None = None
    profile_signature: tuple | None = None


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
        self.recoveries: dict[str, ClimateRecovery] = {}
        self.capability_blocks: dict[str, dict] = {}
        self._states = {}
        self._wanted_modes = {}
        self._refresh_needed = False
        self.head_cycles = {entity: HeadCycle() for entity in config.heads.values()}
        self.cycle_holds: dict[str, dict] = {}
        self._cycle_profiles: dict[str, tuple | None] = {}

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
        self.pending[entity].receipt = True
        return False

    def _sync_issue(self, entity):
        if entity in self.recoveries:
            self.device_issues[entity] = self.recoveries[entity].reason
        elif entity in self.capability_blocks:
            self.device_issues[entity] = self.capability_blocks[entity]["reason"]
        else:
            self.device_issues.pop(entity, None)

    def _issue(self, entity, reason, states=None, *, baseline=None, receipt=False):
        recovery = self.recoveries.get(entity)
        if recovery:
            recovery.reason = reason
        else:
            states = self._states if states is None else states
            pending = self.pending.get(entity)
            if baseline is None:
                baseline = pending.baseline if pending else self.marker(states.get(entity, {}))
            is_off = bool(pending and pending.command == ("set_hvac_mode", (("hvac_mode", "off"),)))
            recovery = ClimateRecovery(
                baseline, reason, receipt or bool(is_off and pending.receipt),
                pending.since if is_off else None)
            self.recoveries[entity] = recovery
        self._sync_issue(entity)

    def _clear_recovery(self, entity):
        self.pending.pop(entity, None)
        self.recoveries.pop(entity, None)
        self._sync_issue(entity)

    def recovery_status(self, now):
        devices = {}
        for entity, reason in self.device_issues.items():
            state = self._states.get(entity, {})
            block = self.capability_blocks.get(entity)
            devices[entity] = {
                "reason": reason, "state": state.get("state", "missing"),
                "available_modes": list(state.get("attributes", {}).get("hvac_modes") or []),
                "required_mode": self._wanted_modes.get(entity),
                "kind": "recovery" if entity in self.recoveries else "capability",
            }
            if block:
                devices[entity]["missing_modes"] = block["missing_modes"]
        remaining = (max(0, self.config.off_settle_seconds - (now - self.all_off_since))
                     if self.phase == "settling" and self.all_off_since is not None else None)
        return {"phase": self.phase, "waiting_devices": self.waiting_devices,
                "settle_remaining_seconds": remaining, "devices": devices,
                "cycle_holds": {entity: dict(hold) for entity, hold in self.cycle_holds.items()}}

    def _recovery_off(self, entity, states, now):
        command = ("set_hvac_mode", (("hvac_mode", "off"),))
        recovery = self.recoveries[entity]
        observed = states.get(entity, {})
        if (recovery.off_receipt and self.ready(observed) and self.reports_off(observed)
                and self.marker(observed) != recovery.baseline):
            self._clear_recovery(entity)
            return True
        interval = min(15, self.config.poll_seconds)
        if recovery.last_attempt is not None and now - recovery.last_attempt < interval:
            return False
        recovery.last_attempt = now
        try:
            self.ensure(entity, "set_hvac_mode", {"hvac_mode": "off"}, self.reports_off,
                        states, now, force=True, fresh=True)
            # Retries renew only the retry clock. Keep the first uncertain
            # report as evidence, including when an already-off call is a no-op.
            self.pending[entity].baseline = recovery.baseline
            recovery.off_receipt = True
            self._refresh_needed = True
            if (self.ready(observed) and self.reports_off(observed)
                    and self.marker(observed) != recovery.baseline):
                self._clear_recovery(entity)
                return True
        except HAError as exc:
            self.pending[entity].baseline = recovery.baseline
            self._issue(entity, f"{exc}; off acknowledgement required", states)
        return False

    def _climate_ensure(self, entity, service, data, matches, states, now, *, fresh=False):
        try:
            before = self.pending.get(entity)
            acknowledged = self.ensure(entity, service, data, matches, states, now, fresh=fresh)
            pending = self.pending.get(entity)
            if (service == "set_hvac_mode" and data.get("hvac_mode") == "off"
                    and pending is not None and pending is not before and pending.receipt):
                self._refresh_needed = True
            return acknowledged
        except (CommandTimeout, HAError) as exc:
            self._issue(entity, f"{exc}; off acknowledgement required", states)
            pending = self.pending.get(entity)
            if pending and pending.command[0] == "set_hvac_mode":
                pending.fresh = True
            return False

    def _supports(self, entity, mode, states):
        modes = set(states.get(entity, {}).get("attributes", {}).get("hvac_modes") or [])
        missing = sorted({"off", mode} - modes)
        if missing:
            available = ", ".join(sorted(modes)) or "none reported"
            reason = f"Requested {mode} unavailable (missing {', '.join(missing)}); available modes: {available}"
            self.capability_blocks[entity] = {"reason": reason, "missing_modes": missing}
            self._sync_issue(entity)
            return False
        self.capability_blocks.pop(entity, None)
        self._sync_issue(entity)
        return True

    def off(self, entity, states, now, *, preserve_capability=False):
        observed = states.get(entity, {})
        if not preserve_capability:
            self.capability_blocks.pop(entity, None)
            self._sync_issue(entity)
        if not self.available(observed):
            self._issue(entity, "Entity unavailable; off acknowledgement required", states)
        elif not self.ready(observed):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required", states)
        if entity in self.recoveries:
            return self._recovery_off(entity, states, now)
        pending = self.pending.get(entity)
        command = ("set_hvac_mode", (("hvac_mode", "off"),))
        if pending and pending.command != command:
            self._issue(entity, "Previous activation must be reconciled; off acknowledgement required", states)
            return self._recovery_off(entity, states, now)
        # An available off device with unsupported heat/cool needs no fresh
        # report unless an actual earlier activation remains uncertain.
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

    def _observe_head_cycles(self, states, now):
        """Timers follow acknowledged head modes, never requested activations."""
        for entity, cycle in self.head_cycles.items():
            observed = states.get(entity, {})
            if not self.ready(observed) or entity in self.recoveries:
                self.head_cycles[entity] = HeadCycle()
                continue
            pending = self.pending.get(entity)
            if self.reports_off(observed) and pending is None:
                if cycle.mode != "off" or cycle.off_since is None:
                    self.head_cycles[entity] = HeadCycle(mode="off", off_since=now)
                continue
            # A stale off report during activation, or an off still awaiting
            # acknowledgement, cannot establish a new off interval.
            cycle.off_since = None
            if observed.get("state") != cycle.mode:
                cycle.mode, cycle.run_since = None, None
                cycle.target_f, cycle.target_native = None, None
                cycle.profile_signature = None

    def _acknowledge_head_run(self, entity, mode, target_f, native_target, now):
        cycle = self.head_cycles.get(entity)
        if cycle is None:
            return
        if cycle.mode != mode or cycle.run_since is None:
            cycle.mode, cycle.run_since = mode, now
        cycle.off_since = None
        cycle.target_f, cycle.target_native = target_f, native_target
        cycle.profile_signature = self._cycle_profiles.get(entity)

    def _head_profile_signature(self, key, plan):
        names = sorted(name for name, room in self.config.zones.items() if room.head == key)
        if not names or any(name not in plan.room_targets for name in names):
            return None
        return tuple((name, tuple(plan.room_targets[name].get(field) for field in
                                 ("low_f", "high_f", "target_f", "comfort_enabled")))
                     for name in names)

    def _can_retain_head(self, key, entity, plan, states):
        cycle = self.head_cycles[entity]
        observed = states.get(entity, {})
        if (key in getattr(plan, "inhibited_heads", {})
                or entity in self.pending or entity in self.recoveries
                or entity in self.capability_blocks
                or not self.ready(observed) or observed.get("state") != self.direction
                or cycle.mode != self.direction or cycle.run_since is None
                or cycle.target_f is None or cycle.profile_signature is None
                or cycle.profile_signature != self._head_profile_signature(key, plan)):
            return False
        modes = set(observed.get("attributes", {}).get("hvac_modes") or ())
        if not {"off", self.direction} <= modes:
            return False
        action = observed.get("attributes", {}).get("hvac_action")
        if action == ("heating" if self.direction == "cool" else "cooling"):
            return False
        try:
            target = float(observed.get("attributes", {}).get("temperature"))
            if not math.isfinite(target) or abs(target - cycle.target_native) >= .05:
                return False
        except (TypeError, ValueError):
            return False
        names = [name for name, room in self.config.zones.items() if room.head == key]
        comfort_enabled = False
        for name in names:
            reading, comfort = plan.rooms.get(name), plan.room_targets.get(name)
            if not reading or not comfort:
                return False
            comfort_enabled = comfort_enabled or comfort.get("comfort_enabled") is True
            try:
                low, high = float(reading["low"]), float(reading["high"])
                lower, upper = float(comfort["low_f"]), float(comfort["high_f"])
                confirmed_low = float(reading.get("confirmed_low", reading.get("temperature", low)))
                confirmed_high = float(reading.get("confirmed_high", reading.get("temperature", high)))
                if not all(math.isfinite(value) for value in
                           (low, high, lower, upper, confirmed_low, confirmed_high)):
                    return False
                if self.direction == "cool" and (confirmed_low <= lower or low <= self.config.low_f + .25):
                    return False
                if self.direction == "heat" and (confirmed_high >= upper or high >= self.config.high_f - .25):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        return bool(names) and comfort_enabled

    def _retain_minimum_runs(self, plan, states, now):
        minimum = self.config.head_minimum_run_seconds
        if (minimum <= 0 or self.phase != "running" or self.direction not in ("heat", "cool")
                or plan.mode not in ("off", self.direction)):
            return
        incompatible = any(self.available(states.get(entity, {}))
                           and states[entity]["state"] not in ("off", self.direction)
                           for entity in self.config.heads.values())
        if incompatible:
            return
        for key, entity in self.config.heads.items():
            cycle = self.head_cycles[entity]
            if key in plan.head_targets_f or not self._can_retain_head(key, entity, plan, states):
                continue
            remaining = minimum - (now - cycle.run_since)
            if remaining <= 0:
                continue
            plan.mode = self.direction
            plan.head_targets_f[key] = cycle.target_f
            self.cycle_holds[entity] = {
                "kind": "minimum_run", "remaining_seconds": remaining,
                "reason": "Finishing minimum heat-pump run",
            }

    def _head_off_guard(self, entity, states, now):
        """Return True while this head must remain off; other heads can run."""
        minimum = self.config.head_minimum_off_seconds
        if minimum <= 0 or entity not in self.head_cycles:
            return False
        required = max(minimum, self.config.off_settle_seconds)
        pending = self.pending.get(entity)
        off_command = ("set_hvac_mode", (("hvac_mode", "off"),))
        if pending and pending.command == off_command:
            self.off(entity, states, now)
            if entity in self.pending or entity in self.recoveries:
                self.cycle_holds[entity] = {
                    "kind": "minimum_off", "remaining_seconds": required,
                    "reason": "Waiting for heat-pump off acknowledgement before timing idle",
                }
                return True
            self._observe_head_cycles(states, now)
        # An activation already sent must keep its normal acknowledgement and
        # timeout path. Do not turn its stale off report into a new idle timer.
        if entity in self.pending:
            return False
        observed = states.get(entity, {})
        if self.ready(observed) and observed.get("state") == "off" and not self.reports_off(observed):
            self.off(entity, states, now)
            self.cycle_holds[entity] = {
                "kind": "minimum_off", "remaining_seconds": required,
                "reason": "Waiting for heat-pump off acknowledgement before timing idle",
            }
            return True
        if not self.ready(observed) or not self.reports_off(observed):
            return False
        self._observe_head_cycles(states, now)
        since = self.head_cycles[entity].off_since
        remaining = required if since is None else required - (now - since)
        if remaining <= 0:
            return False
        self.cycle_holds[entity] = {
            "kind": "minimum_off", "remaining_seconds": remaining,
            "reason": "Waiting for minimum heat-pump off time",
        }
        return True

    def climate(self, entity, mode, target_f, states, now, heater=False):
        state = states.get(entity, {})
        if not self.available(state):
            self._issue(entity, "Entity unavailable; off acknowledgement required", states)
        elif not self.ready(state):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required", states)
        if entity in self.recoveries and not self._recovery_off(entity, states, now):
            return
        if not self._supports(entity, mode, states):
            self.off(entity, states, now, preserve_capability=True)
            return
        if not heater and self._head_off_guard(entity, states, now):
            return
        target = self.target(entity, target_f, states, heater)

        def matches(observed):
            try:
                return observed.get("state") == mode and abs(float(observed.get("attributes", {}).get("temperature")) - target) < .05
            except (ValueError, TypeError):
                return False

        acknowledged = self._climate_ensure(
            entity, "set_temperature", {"hvac_mode": mode, "temperature": target}, matches, states, now)
        if acknowledged:
            self._acknowledge_head_run(entity, mode, target_f, target, now)

    def begin_stop(self, states, now):
        self.cycle_holds.clear()
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
            self._issue(entity, "Entity unavailable; off acknowledgement required", states)
        elif not self.ready(states.get(entity, {})):
            self._issue(entity, "Climate metadata incomplete; off acknowledgement required", states)
        if entity in self.recoveries and not self._recovery_off(entity, states, now):
            return
        if plan.heater_on:
            self.climate(entity, "heat", self.config.heater_high_setpoint_f, states, now, heater=True)
        elif plan.auxiliary_fan_only:
            state = states.get(entity, {})
            if not self._supports(entity, "fan_only", states):
                self.off(entity, states, now, preserve_capability=True)
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
        self.cycle_holds.clear()
        self._observe_head_cycles(states, now)
        self._retain_minimum_runs(plan, states, now)
        self._cycle_profiles = {entity: self._head_profile_signature(key, plan)
                                for key, entity in self.config.heads.items()}
        self._states = states
        self._wanted_modes = {entity: plan.mode if key in plan.head_targets_f else "off"
                              for key, entity in self.config.heads.items()}
        if self.config.heater_entity:
            self._wanted_modes[self.config.heater_entity] = (
                "heat" if plan.heater_on else "fan_only" if plan.auxiliary_fan_only else "off")
        self._refresh_needed = False
        self.auxiliary(plan, states, now)
        self.shared(plan, states, now)
        # Consume synchronous HA reports once, without stacking full polling
        # intervals between off acknowledgement and the next valid local plan.
        if self._refresh_needed:
            refreshed = self.ha.states()
            self._states = refreshed
            self._refresh_needed = False
            self.auxiliary(plan, refreshed, now)
            self._observe_head_cycles(refreshed, now)
            self.shared(plan, refreshed, now)
        self._observe_head_cycles(self._states, now)
        for entity, hold in self.cycle_holds.items():
            message = f"{entity}: {hold['reason']}; {math.ceil(hold['remaining_seconds'])}s remaining"
            if message not in plan.reasons:
                plan.reasons.append(message)
        for entity, percentage in plan.fans.items():
            self.fan(entity, percentage, plan, self._states, now)
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
            for entity in self.config.heads.values():
                self.off(entity, states, now)
            all_off = all(self.ready(states.get(entity, {})) and self.reports_off(states.get(entity, {}))
                          and entity not in self.pending and entity not in self.recoveries
                          for entity in self.config.heads.values())
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

    def shutdown(self, states=None) -> list[str]:
        """Best effort across every device even when one service call fails."""
        failures = []
        snapshot = self._states if states is None else states
        old_pending = dict(self.pending)
        for entity in [*([self.config.heater_entity] if self.config.heater_entity else []), *self.config.heads.values(), *self.config.fans.values()]:
            try:
                if entity.startswith("climate."):
                    self.ha.service("climate", "set_hvac_mode", entity_id=entity, hvac_mode="off")
                else:
                    self.ha.service("fan", "turn_off", entity_id=entity)
            except Exception:
                failures.append(entity)
        self.phase, self.direction = "new", None
        self.head_cycles = {entity: HeadCycle() for entity in self.config.heads.values()}
        self.cycle_holds.clear()
        self.pending.clear()
        self.fan_failures.clear()
        self.device_issues.clear()
        self.recoveries.clear()
        self.capability_blocks.clear()
        for entity in failures:
            if entity in self.config.climate_entities:
                previous = old_pending.get(entity)
                baseline = previous.baseline if previous else self.marker(snapshot.get(entity, {}))
                self._issue(entity, "Shutdown off command failed; off acknowledgement required",
                            snapshot, baseline=baseline)
        return failures
