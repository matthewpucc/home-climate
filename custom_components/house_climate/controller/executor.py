from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .ha import HAError
from .policy import Plan, call_target_f, native_target
from .sensors import from_f, to_f


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
    activation_since: float | None = None


@dataclass
class ClimateRecovery:
    baseline: tuple
    reason: str
    off_receipt: bool = False
    last_attempt: float | None = None
    command: tuple | None = None


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
    call_target_f: float | None = None
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
        self.head_stop_reasons: dict[str, dict] = {}
        self._observed_head_modes: dict[str, str | None] = {}
        self._acknowledged_head_modes: dict[str, str] = {}
        self._off_requested_since_activation: set[str] = set()
        self._diagnostic_plan: Plan | None = None
        self._shared_stop_context: tuple[str, str] | None = None

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
        head_activation = (entity in self.head_cycles and service == "set_temperature"
                           and data.get("hvac_mode") in ("heat", "cool"))
        same_activation = (head_activation and pending is not None
                           and pending.command[0] == "set_temperature"
                           and dict(pending.command[1]).get("hvac_mode") == data["hvac_mode"])
        activation_since = now if head_activation else None
        if same_activation:
            # A new target must not renew the budget for an activation that
            # still has not been confirmed. A previous target arriving on this
            # same poll does complete that command and earns a new budget.
            previous_data = dict(pending.command[1])
            try:
                previous_matches = (
                    observed.get("state") == previous_data["hvac_mode"]
                    and abs(float(observed.get("attributes", {}).get("temperature"))
                            - float(previous_data["temperature"])) < .05
                    and (not pending.fresh or self.marker(observed) != pending.baseline))
            except (KeyError, ValueError, TypeError, OverflowError):
                previous_matches = False
            if pending.command == command or not previous_matches:
                activation_since = (pending.activation_since if pending.activation_since is not None
                                    else pending.since)
        acknowledged = matches(observed) and (
            pending is None or not pending.fresh or self.marker(observed) != pending.baseline)
        if not force and acknowledged and (pending is None or pending.command == command):
            self.pending.pop(entity, None)
            return True
        if not force and pending and (pending.command == command or same_activation):
            started = activation_since if head_activation else pending.since
            timeout = (self.config.head_activation_timeout_seconds if head_activation
                       else self.config.command_timeout_seconds)
            if now - started >= timeout:
                # A replacement target has not been dispatched yet. Report the
                # actual outstanding command, which recovery will preserve.
                raise CommandTimeout(self._command_timeout_message(
                    entity, pending.command[0], dict(pending.command[1]), observed))
            if pending.command == command:
                return False
        # A transport exception may occur after dispatch. Preserve the command
        # before calling HA so a later off supersedes the uncertain activation.
        self.pending[entity] = Pending(command, now, self.marker(observed), fresh,
                                       activation_since=activation_since)
        if service == "set_hvac_mode" and data.get("hvac_mode") == "off":
            self._record_controller_head_stop(entity, states, previous_pending=pending)
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
                pending.since if is_off else None, pending.command if pending else None)
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
                "kind": ("recovery" if entity in self.recoveries
                         else block.get("kind", "capability") if block else "capability"),
            }
            pending = self.pending.get(entity)
            recovery = self.recoveries.get(entity)
            command = recovery.command if recovery and recovery.command else pending.command if pending else None
            if command:
                devices[entity]["pending_command"] = {"service": command[0], "data": dict(command[1])}
            if block:
                devices[entity]["missing_modes"] = block["missing_modes"]
        pending_heads = {}
        for entity in self.head_cycles:
            pending = self.pending.get(entity)
            if pending is None or pending.activation_since is None or entity in self.recoveries:
                continue
            command = dict(pending.command[1])
            if pending.command[0] != "set_temperature" or command.get("hvac_mode") not in ("heat", "cool"):
                continue
            observed = self._states.get(entity, {})
            elapsed = max(0.0, now - pending.activation_since)
            timeout = self.config.head_activation_timeout_seconds
            pending_heads[entity] = {
                "requested_mode": command["hvac_mode"],
                "requested_target_temperature": self._finite_temperature(command.get("temperature")),
                "reported_mode": observed.get("state", "missing"),
                "reported_target_temperature": self._finite_temperature(
                    observed.get("attributes", {}).get("temperature")),
                "temperature_unit": self.unit, "elapsed_seconds": elapsed,
                "remaining_seconds": max(0.0, timeout - elapsed), "timeout_seconds": timeout,
            }
        remaining = (max(0, self.config.off_settle_seconds - (now - self.all_off_since))
                     if self.phase == "settling" and self.all_off_since is not None else None)
        return {"phase": self.phase, "waiting_devices": self.waiting_devices,
                "settle_remaining_seconds": remaining, "devices": devices,
                "pending_head_commands": pending_heads,
                "cycle_holds": {entity: dict(hold) for entity, hold in self.cycle_holds.items()},
                "head_stop_reasons": {entity: dict(reason) for entity, reason in self.head_stop_reasons.items()}}

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

    def target(self, entity: str, fahrenheit: float, states: dict, heater: bool = False,
               *, mode: str | None = None) -> float:
        attrs = states.get(entity, {}).get("attributes", {})
        floor = (self.config.head_cool_min_f.get(entity)
                 if not heater and mode == "cool" and entity in self.head_cycles else None)
        try:
            result = native_target(fahrenheit, attrs, self.unit, minimum_f=floor)
            if heater and not float(attrs["min_temp"]) <= from_f(fahrenheit, self.unit) <= float(attrs["max_temp"]):
                raise ControlFault("Configured high heater setpoint is outside its supported range")
            return result
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            if floor is not None:
                raise ControlFault(f"Cooling minimum {floor:g}°F cannot be applied to {entity}: {exc}") from None
            raise ControlFault(f"Missing/invalid climate temperature range: {entity}") from None

    def _command_timeout_message(self, entity, service, data, observed):
        message = f"Command acknowledgement timed out: {entity}"
        if not entity.startswith("climate."):
            return message
        attrs = observed.get("attributes", {})
        wanted = self._finite_temperature(data.get("temperature"))
        actual = self._finite_temperature(attrs.get("temperature"))
        desired = f"{wanted:g}{self.unit}" if wanted is not None else "unchanged"
        reported = f"{actual:g}{self.unit}" if actual is not None else "unknown"
        message += (f"; requested service={service} mode={data.get('hvac_mode')} target={desired}"
                    f"; reported mode={observed.get('state')} target={reported}")
        stamp = observed.get("last_reported") or observed.get("last_updated")
        if stamp:
            message += f"; last report={stamp}"
            try:
                instant = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if instant.utcoffset() is not None:
                    age = (datetime.now(timezone.utc) - instant).total_seconds()
                    message += f" ({age:.0f}s ago)"
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            message += "; last report=unknown"
        return message

    @staticmethod
    def _finite_temperature(value):
        try:
            number = float(value)
            return number if not isinstance(value, bool) and math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _record_head_stop(self, entity, kind, reason, states, requested_mode, requested_target=None):
        observed = states.get(entity, {})
        attrs = observed.get("attributes", {})
        reported = observed.get("last_reported") or observed.get("last_updated")
        self.head_stop_reasons[entity] = {
            "kind": kind, "reason": reason, "requested_mode": requested_mode,
            "requested_target_temperature": self._finite_temperature(requested_target),
            "observed_mode": observed.get("state"),
            "observed_target_temperature": self._finite_temperature(attrs.get("temperature")),
            "observed_current_temperature": self._finite_temperature(attrs.get("current_temperature")),
            "temperature_unit": self.unit,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "reported_at": str(reported) if reported is not None else None,
        }

    def _record_controller_head_stop(self, entity, states, *, reason=None, kind=None, previous_pending=None):
        if entity not in self.head_cycles:
            return
        plan = self._diagnostic_plan
        key = next(key for key, candidate in self.config.heads.items() if candidate == entity)
        requested_mode = plan.mode if plan and key in plan.head_targets_f else "off"
        previous = self.head_stop_reasons.get(entity, {})
        requested_target = self.head_cycles[entity].target_native
        if requested_target is None:
            requested_target = previous.get("requested_target_temperature")
        command = previous_pending.command if previous_pending else None
        recovery = self.recoveries.get(entity)
        if (not command or command[0] != "set_temperature") and recovery and recovery.command:
            command = recovery.command
        previous_command = dict(command[1]) if command else {}
        if command and command[0] == "set_temperature":
            requested_mode = previous_command.get("hvac_mode", requested_mode)
            requested_target = previous_command.get("temperature", requested_target)
        elif entity in self._acknowledged_head_modes:
            requested_mode = self._acknowledged_head_modes[entity]
        elif previous.get("requested_mode") in ("heat", "cool"):
            requested_mode = previous["requested_mode"]
        if reason is None:
            inhibition = plan.inhibited_heads.get(key) if plan else None
            if inhibition:
                kind, reason = "safety", f"Off requested: {inhibition}"
            elif entity in self.recoveries:
                kind, reason = "recovery", f"Off requested to reconcile device state: {self.recoveries[entity].reason}"
            elif entity in self.capability_blocks:
                block = self.capability_blocks[entity]
                kind, reason = block.get("kind", "capability"), f"Off requested: {block['reason']}"
            elif self._shared_stop_context:
                kind, reason = self._shared_stop_context
            else:
                kind, reason = "no_demand", "Off requested: room no longer needs the shared heat-pump call"
        self._off_requested_since_activation.add(entity)
        self._record_head_stop(entity, kind or "controller", reason, states, requested_mode, requested_target)

    def _observe_head_stops(self, plan, states):
        """Retain unexplained off observations without attributing their cause."""
        for key, entity in self.config.heads.items():
            observed = states.get(entity, {})
            mode = observed.get("state") if self.ready(observed) else None
            previous = self._observed_head_modes.get(entity)
            requested = plan.mode if key in plan.head_targets_f else "off"
            if (previous in ("heat", "cool") and self.reports_off(observed)
                    and requested == previous
                    and self._acknowledged_head_modes.get(entity) == previous
                    and entity not in self.pending and entity not in self.recoveries
                    and entity not in self._off_requested_since_activation):
                self._record_head_stop(entity, "reported_off",
                    f"Head reported off while {previous} was still requested",
                    states, requested, self.head_cycles[entity].target_native)
            self._observed_head_modes[entity] = mode

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
                cycle.target_f, cycle.call_target_f, cycle.target_native = None, None, None
                cycle.profile_signature = None

    def _acknowledge_head_run(self, entity, mode, target_f, call_target, native_value, now):
        cycle = self.head_cycles.get(entity)
        if cycle is None:
            return
        if cycle.mode != mode or cycle.run_since is None:
            cycle.mode, cycle.run_since = mode, now
            self._off_requested_since_activation.discard(entity)
        self._acknowledged_head_modes[entity] = mode
        cycle.off_since = None
        cycle.target_f, cycle.call_target_f, cycle.target_native = target_f, call_target, native_value
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
                or cycle.target_f is None or cycle.call_target_f is None or cycle.profile_signature is None
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
                temperature = float(reading["temperature"])
                if not all(math.isfinite(value) for value in
                           (low, high, lower, upper, temperature)):
                    return False
                if self.direction == "cool" and (temperature <= lower or low <= self.config.low_f + .25):
                    return False
                if self.direction == "heat" and (temperature >= upper or high >= self.config.high_f - .25):
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
            plan.head_call_targets_f[key] = cycle.call_target_f
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

    def climate(self, entity, mode, target_f, states, now, heater=False, *, comfort_target_f=None):
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
        floor_configured = (not heater and mode == "cool" and entity in self.head_cycles
                            and entity in self.config.head_cool_min_f)
        if not heater and not floor_configured and self._head_off_guard(entity, states, now):
            return
        try:
            target = self.target(entity, target_f, states, heater, mode=mode)
        except ControlFault as exc:
            if not floor_configured:
                raise
            # This is a configuration/metadata constraint, not a failed
            # device command. Keep the issue local and exclude automatic reload.
            self.capability_blocks[entity] = {
                "kind": "configuration", "reason": str(exc), "missing_modes": [],
            }
            self._sync_issue(entity)
            self.off(entity, states, now, preserve_capability=True)
            return
        if floor_configured and self._head_off_guard(entity, states, now):
            return

        def matches(observed):
            try:
                return observed.get("state") == mode and abs(float(observed.get("attributes", {}).get("temperature")) - target) < .05
            except (ValueError, TypeError):
                return False

        acknowledged = self._climate_ensure(
            entity, "set_temperature", {"hvac_mode": mode, "temperature": target}, matches, states, now)
        if acknowledged:
            comfort = target_f if comfort_target_f is None else comfort_target_f
            self._acknowledge_head_run(entity, mode, comfort, to_f(target, self.unit), target, now)

    def begin_stop(self, states, now, *, reason="Shared-system all-off reconciliation", kind="reconciliation"):
        self._shared_stop_context = (kind, reason)
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
                # Dyson integrations treat set_percentage as speed-only.
                # Request power explicitly when supported; speed-only fans
                # retain their set_percentage activation contract.
                service = "turn_on" if attrs["supported_features"] & 32 else "set_percentage"
                data = {"percentage": target}

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
            requested = dict(command[1]).get("percentage", percentage) if command else percentage
            target = f"{requested}%" if percentage else "off"
            reason = str(exc).removesuffix(f": {entity}")
            message = (f"{reason}; requested {target}, reported {observed.get('state')} "
                       f"({observed.get('attributes', {}).get('percentage')}%)")
            failure = FanRetry(command, message, now + delay, attempts)
            self.fan_failures[entity] = failure
        if failure:
            plan.warnings.append(f"Circulation fan {entity}: {failure.message}; retrying independently")

    def step(self, plan: Plan, states: dict, now: float):
        self._diagnostic_plan = plan
        self._observe_head_stops(plan, states)
        self.cycle_holds.clear()
        self._observe_head_cycles(states, now)
        self._retain_minimum_runs(plan, states, now)
        plan.head_call_targets_f = {key: value for key, value in plan.head_call_targets_f.items()
                                    if key in plan.head_targets_f}
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
            self._observe_head_stops(plan, refreshed)
            self._refresh_needed = False
            self.auxiliary(plan, refreshed, now)
            self._observe_head_cycles(refreshed, now)
            self.shared(plan, refreshed, now)
        self._observe_head_cycles(self._states, now)
        for entity, hold in self.cycle_holds.items():
            if entity in self.head_stop_reasons:
                hold['stop_reason'] = self.head_stop_reasons[entity]['reason']
                hold['stop_kind'] = self.head_stop_reasons[entity]['kind']
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
            self.begin_stop(states, now, reason="Startup all-off reconciliation", kind="startup")
            return
        if self.phase == "running":
            incompatible = any(self.available(states.get(entity, {}))
                               and states[entity]["state"] not in ("off", self.direction)
                               for entity in self.config.heads.values())
            if plan.mode != self.direction or incompatible:
                if incompatible:
                    kind, reason = "reconciliation", "Shared system reported a conflicting HVAC mode"
                elif plan.mode == "off":
                    kind, reason = "no_demand", "Off requested: no eligible shared-system demand"
                else:
                    kind, reason = "changeover", f"Shared direction changing from {self.direction} to {plan.mode}"
                self.begin_stop(states, now, reason=reason, kind=kind)
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
                self.begin_stop(states, now, reason="Shared head changed state during off settling")
                return
            if plan.mode == "off" or now - self.all_off_since < self.config.off_settle_seconds:
                return
            self.phase, self.direction = "running", plan.mode
            self._shared_stop_context = None
        if self.phase == "running":
            # Off commands first; every active head is then set to the same direction.
            for key, entity in self.config.heads.items():
                if key not in plan.head_targets_f:
                    self.off(entity, states, now)
            for key, target in plan.head_targets_f.items():
                entity = self.config.heads[key]
                call = plan.head_call_targets_f.get(key)
                if call is None:
                    call = call_target_f(self.config, entity, plan.mode, target, states, self.unit)
                try:
                    # Fresh capability limits may differ from the cached policy
                    # snapshot; this never recalculates bias from a changing sensor.
                    floor = self.config.head_cool_min_f.get(entity) if plan.mode == "cool" else None
                    call = to_f(native_target(call, states.get(entity, {}).get("attributes", {}),
                                              self.unit, minimum_f=floor), self.unit)
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
                plan.head_call_targets_f[key] = call
                self.climate(entity, plan.mode, call, states, now, comfort_target_f=target)

    def shutdown(self, states=None, *, reason="Controller shutdown") -> list[str]:
        """Best effort across every device even when one service call fails."""
        failures = []
        snapshot = self._states if states is None else states
        old_pending = dict(self.pending)
        for entity in [*([self.config.heater_entity] if self.config.heater_entity else []), *self.config.heads.values(), *self.config.fans.values()]:
            try:
                if entity.startswith("climate."):
                    self._record_controller_head_stop(entity, snapshot, reason=reason, kind="shutdown",
                                                      previous_pending=old_pending.get(entity))
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
