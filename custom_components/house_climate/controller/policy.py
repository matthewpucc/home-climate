from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .config import Config, Settings
from .sensors import Reading, Sensors, forecast_slope, from_f, to_f
from .temperature_guard import heating_cutoff_reached
from .head_guard import HeadGuard, protection_requested
from .airflow import TransferAirflow
from .operating_state import OperatingState, WINDOW_COMFORT_FACTOR
from .auxiliary_dispatch import AuxiliaryDispatch, manual_auxiliary, soc_grace_observed, soc_grace_retains_heat
from .reserve_split import RollingAttempt, observe as observe_reserve_split
from .protection_context import emergency, emergency_magnitudes, initial_context, limits, window_affected


def native_target(fahrenheit: float, attrs: dict, unit: str, *, minimum_f: float | None = None) -> float:
    """Bound and quantize on the climate entity's original HA setpoint grid."""
    minimum, maximum = float(attrs["min_temp"]), float(attrs["max_temp"])
    step = float(attrs.get("target_temp_step", .5 if unit in ("C", "°C") else 1))
    value = from_f(fahrenheit, unit)
    if (not all(math.isfinite(number) for number in (minimum, maximum, step, value))
            or minimum > maximum or step <= 0):
        raise ValueError("Invalid climate temperature range")
    if minimum_f is not None:
        floor = from_f(minimum_f, unit)
        if isinstance(minimum_f, bool) or not math.isfinite(floor):
            raise ValueError("Invalid minimum climate setpoint")
        # Raising min_temp itself would shift the grid. Keep the original
        # anchor and choose the first supported rung at or above the floor.
        # Decimal arithmetic avoids an extra rung from binary float error.
        anchor, ceiling, increment = (Decimal(str(number)) for number in (minimum, maximum, step))
        floor_value = Decimal(str(minimum_f))
        if unit in ("C", "°C"):
            floor_value = (floor_value - 32) * 5 / 9
        lower = max(anchor, floor_value)
        first = int(((lower - anchor) / increment).to_integral_value(rounding=ROUND_CEILING))
        last = int(((ceiling - anchor) / increment).to_integral_value(rounding=ROUND_FLOOR))
        if first > last:
            raise ValueError(
                f"No supported setpoint at or above {minimum_f:g}°F within HA range "
                f"{minimum:g} to {maximum:g}{unit}")
        rung = max(first, min(last, round((value - minimum) / step)))
        return float(anchor + rung * increment)
    value = max(minimum, min(maximum, minimum + round((value - minimum) / step) * step))
    return round(value, 3)


def uses_whole_fahrenheit(config: Config, entity: str, unit: str) -> bool:
    return unit in ("F", "°F") and entity in config.whole_fahrenheit_heads


def head_native_target(config: Config, entity: str, mode: str | None, fahrenheit: float,
                       attrs: dict, unit: str) -> float:
    """Select one representable native head command without changing room comfort."""
    floor = config.head_cool_min_f.get(entity) if mode == "cool" else None
    if not uses_whole_fahrenheit(config, entity, unit):
        return native_target(fahrenheit, attrs, unit, minimum_f=floor)

    # This explicit equipment profile overrides the advertised step: Serin
    # standard Fahrenheit lookup can report a different value from HA's 0.5° UI.
    low, high = float(attrs["min_temp"]), float(attrs["max_temp"])
    if not all(math.isfinite(number) for number in (low, high, fahrenheit)) or low > high:
        raise ValueError("Invalid climate temperature range")
    first = math.ceil(max(low, floor) if floor is not None else low)
    last = math.floor(high)
    if first > last:
        constraint = f" and cooling minimum {floor:g}°F" if floor is not None else ""
        raise ValueError(f"No whole-Fahrenheit setpoint within HA range {low:g} to {high:g}°F{constraint}")
    nearest = int(Decimal(str(fahrenheit)).to_integral_value(rounding=ROUND_HALF_UP))
    return max(first, min(last, nearest))


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
    floor = config.head_cool_min_f.get(entity) if mode == "cool" else None
    if floor is not None:
        target = max(target, floor)
    try:
        return to_f(head_native_target(config, entity, mode, target, attrs, climate_unit), climate_unit)
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
    head_constraints: dict = field(default_factory=dict)
    head_protections: dict = field(default_factory=dict)
    unavailable_mode_requests: dict = field(default_factory=dict)
    mode_scores: dict = field(default_factory=dict)
    mode_hold_remaining_seconds: float | None = None
    recovery_inputs: str | None = None
    heater_on: bool = False
    auxiliary_fan_only: bool = False
    auxiliary_policy: dict = field(default_factory=dict)
    reserve_split: dict = field(default_factory=dict)
    reserve_split_ticket: dict | None = None
    reserve_split_stop_cooling: bool = False
    reserve_split_stop_shared: bool = False
    seasonal: dict = field(default_factory=dict)
    fans: dict[str, int] = field(default_factory=dict)
    fan_head_assists: dict[str, str] = field(default_factory=dict)
    fan_airflow: dict[str, dict] = field(default_factory=dict)
    fan_speed_overrides: dict[str, float | None] = field(default_factory=dict)
    fan_minimum_percentages: dict[str, float] = field(default_factory=dict)
    outdoor_source_evidence: dict = field(default_factory=dict)
    rooms: dict = field(default_factory=dict)
    room_targets: dict = field(default_factory=dict)
    head_fan_investigation: dict = field(default_factory=dict)
    window_states: dict = field(default_factory=dict)
    protection_context: dict = field(default_factory=dict)
    decision_valid_until_monotonic: float | None = None
    window_authorizations: dict = field(default_factory=dict)
    window_authorization_heads: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        public = asdict(self)
        for key in ("reserve_split_ticket", "reserve_split_stop_cooling", "reserve_split_stop_shared"):
            public.pop(key)
        return public


class Policy:
    """Pure decision layer. Never performs I/O or assumes a command succeeded."""

    def __init__(self, config: Config, clock=None):
        self.config = config
        self.fan_controls = config.fan_controls
        self.sensors = Sensors(config)
        self.head_guard = HeadGuard(config)
        self.calls: dict[tuple[str, str], bool] = {}
        self.forecast_pending: set[tuple[str, str]] = set()
        self.head_requests: dict[str, str] = {}
        self.selected_mode = "off"
        self.last_direction: str | None = None
        self.mode_since: datetime | None = None
        self.candidate: tuple[str, datetime] | None = None
        self.heater_on = False
        self.auxiliary_dispatch = AuxiliaryDispatch(config)
        self._auxiliary_fan_enabled = False
        self.seasonal_evaluator = None
        self.seasonal_status = None
        self.transfer_on: dict[str, bool] = {}
        self.transfer_airflow = TransferAirflow(config)
        from .directed_airflow import DirectedAirflow
        self.directed_airflow = DirectedAirflow(self)
        self.operating = OperatingState(config, clock=clock)
        self.context = initial_context(config)
        self._protection_only = frozenset()
        self._profiles = {name: zone.profile for name, zone in config.zones.items()}
        self.reserve_attempt = RollingAttempt(
            confirmation=config.switch_confirmation_seconds,
            acknowledgement_timeout=config.command_timeout_seconds + 2 * config.poll_seconds,
            evidence_maximum_age=min(config.sensor_stale_seconds, config.forecast_minutes * 60 or config.sensor_stale_seconds),
            isolation_timeout=(config.command_timeout_seconds + config.off_settle_seconds
                               + 3 * config.poll_seconds + config.sensor_stale_seconds))
        self._reserve_clock_offset = None
        self._reserve_native_identity = None
        self._reserve_probe_sources = None
        self._reserve_cooling_heads = ()

    def seasonal_settings(self, states, now, settings):
        policy = getattr(self.config, "seasonal_policy", None)
        if policy is None or not policy.enabled:
            return settings
        from .outdoor import read_outdoor
        from .seasonal import SeasonalEvaluator
        if self.seasonal_evaluator is None:
            self.seasonal_evaluator = SeasonalEvaluator(policy)
            self.seasonal_evaluator.restore({"profile": settings.seasonal_profile})
        self.seasonal_status = self.seasonal_evaluator.update(read_outdoor(self.config, states, now), now)
        return replace(settings, seasonal_profile=self.seasonal_status.profile)

    def update_auxiliary(self, plan, states, rooms, now, *, allow_start=True):
        """Use the same bounds and evidence for decisions and maintenance."""
        name = self.config.auxiliary_room
        reading = rooms.get(name)
        target = plan.room_targets.get(name)
        if not self.config.heater_entity or target is None:
            plan.heater_on = plan.auxiliary_fan_only = self.heater_on = False
            return
        native = states.get(self.config.heater_entity, {})
        attrs = native.get("attributes", {})
        modes = attrs.get("hvac_modes") or ()
        present = native.get("state") not in (None, "unknown", "unavailable")
        manual = manual_auxiliary(self.config, states)
        grace = soc_grace_observed(native)
        available = bool(present and (not modes or {"off", "heat"} <= set(modes))
                         and (not self.config.legacy and soc_grace_retains_heat(native) if grace
                              else not attrs.get("home_climate_auxiliary_blocked")))
        band = target["low_f"], target["high_f"], target["target_f"]
        low, high = limits(self.config, self.context)
        protective = bool(emergency(self.context, name, "heat").get("active")
                          or (reading and reading.confirmed_low < low + 1))
        head = self.config.zones[name].head
        cooling_waiting = any(key != name and detail.get("cool_requested")
                              for key, detail in plan.room_targets.items())
        if target.get("protection_only") and not protective:
            self.auxiliary_dispatch.active = False
        decision = self.auxiliary_dispatch.decide(reading, now, band,
            available=available, manual=manual,
            heat_pump_selected=plan.mode == "heat" and head in plan.head_targets_f,
            cooling_waiting=cooling_waiting, emergency=protective,
            comfort_enabled=target["comfort_enabled"], allow_start=allow_start and not grace,
            protection_only=target.get("protection_only", False))
        plan.auxiliary_policy = decision.as_dict()
        plan.auxiliary_policy["context"] = [*band, not target["comfort_enabled"], decision.peak]
        if target.get("protection_only"):
            plan.auxiliary_policy["context"].append("protection_only")
        plan.auxiliary_policy["stop_revision"] = attrs.get("home_climate_auxiliary_stop_revision", 0)
        if attrs.get("home_climate_auxiliary_blocked"):
            plan.auxiliary_policy["reason"] = attrs.get("home_climate_auxiliary_reason") or decision.reason
        requested = decision.requested
        if reading and heating_cutoff_reached(reading, decision.stop_target_f, high):
            requested = False
            self.auxiliary_dispatch.active = False
            plan.auxiliary_policy.update(requested=False, reason="Fresh room temperature/forecast reaches the auxiliary stop boundary")
        if grace and requested:
            plan.auxiliary_policy.update(
                soc_grace_retaining=True,
                reason="Retaining already-running auxiliary heat during the bounded battery-reading grace")
        plan.heater_on = self.heater_on = requested
        if (manual or not present or reading is None or reading.low <= low + .25
                or grace or attrs.get("home_climate_auxiliary_fan_blocked")):
            plan.auxiliary_fan_only = False
        elif plan.heater_on:
            plan.auxiliary_fan_only = False
        elif allow_start:
            plan.auxiliary_fan_only = bool(
                self._auxiliary_fan_enabled and target.get("cool_requested") and "fan_only" in modes)

    def _apply_reserve_split(self, plan, states, rooms, now, tick, eligible, proof, isolated_at, climate_unit):
        """Apply a supervised internal plan only with the native proof contract."""
        report = plan.reserve_split
        attempt = self.reserve_attempt
        room = self.config.auxiliary_room
        reading = rooms.get(room)
        head = self.config.zones[room].head if room else None
        reserve = (self.head_guard.protection(head, "heat", rooms, plan.room_targets)
                   if head and reading is not None else None)
        # An emergency, missing evidence, or temporarily ineligible head is
        # not affirmative proof that the underlying conflict has cleared.
        contested = bool(report and report["stronger_cooling_by_measured_deficit"])
        old_cooling_rooms = [key for key, zone in self.config.zones.items()
                             if zone.head in self._reserve_cooling_heads]
        def cooling_cleared(key):
            current = rooms.get(key)
            return (current is not None and current.observed_at is not None
                    and 0 <= now.timestamp() - current.observed_at <= self.config.sensor_stale_seconds
                    and not self.actual_demand(current, tuple(plan.room_targets[key][field]
                                               for field in ("low_f", "high_f", "target_f")), "cool"))

        old_cooling_clear = bool(old_cooling_rooms) and all(map(cooling_cleared, old_cooling_rooms))
        emergencies = emergency_magnitudes(self.context)
        continuing_uncertain = attempt.stage != "idle" and not report and (
            reading is None or (not old_cooling_clear and (reserve is not None or bool(emergencies))))
        competing = contested or continuing_uncertain
        clock_offset = now.timestamp() - tick
        sources = tuple(item[0] for item in reading.observation_id) if reading is not None else ()
        if attempt.stage == "idle":
            self._reserve_clock_offset = clock_offset
            self._reserve_native_identity = None
            self._reserve_probe_sources = sources
            self._reserve_cooling_heads = tuple(report.get("cooling_heads", ()))
        identity_keys = ("generation", "auxiliary_generation", "stop_revision", "stop_epoch")
        repair_epochs = proof.get("repair_epochs") if proof else None
        identity = (tuple(proof[key] for key in identity_keys) + (repair_epochs,)
                    if proof and proof.get("ready") is True and isinstance(repair_epochs, tuple)
                    and all(type(proof.get(key)) is int for key in identity_keys) else None)
        offset = self._reserve_clock_offset
        source = (reading.observed_at - offset, reading.low) if (
            reading is not None and reading.observed_at is not None and offset is not None) else None
        reported = proof.get("heat_reported_at") if proof else None
        ack = (reported - offset if identity is not None and offset is not None
               and type(reported) in (int, float) else None)
        ready = bool(contested and report.get("preconditions_ready")
                     and sources and sources == self._reserve_probe_sources
                     and plan.heater_on and not self.context.confirmed_open_rooms
                     and offset is not None and abs(clock_offset - offset) <= self.config.poll_seconds
                     and (attempt.stage in ("idle", "offered")
                          or (identity is not None and identity == self._reserve_native_identity)))
        stage = attempt.advance(now=tick, competing=competing, ready=ready,
                                heat_ack_at=ack, reading=source, isolated_at=isolated_at)
        if stage != "idle":
            report["mode"] = stage
        if stage == "fallback":
            plan.reserve_split_stop_cooling = True
            if plan.mode == "cool" and (reading is None or reserve is not None) and "cool" not in emergencies:
                # Losing the protected room's readings cannot let the same
                # ordinary Cool escape via a baseline without that room.
                plan.mode, plan.head_targets_f, plan.head_call_targets_f, plan.head_protections = "off", {}, {}, {}
            plan.reasons.append("Auxiliary split proof failed; restoring ordinary protective arbitration")
            return
        if stage not in ("isolating", "active"):
            return
        if stage == "isolating" and self._reserve_native_identity is None:
            self._reserve_native_identity = identity
        if identity is None or identity != self._reserve_native_identity:
            attempt.stage = "fallback"
            plan.reserve_split_stop_cooling = True
            report["mode"] = "fallback"
            return
        heads = {head: eligible["cool"][head] for head in report["cooling_heads"]} if stage == "active" else {}
        expiry = tick + self.config.poll_seconds
        proof_expiry = proof.get("expires_at_monotonic")
        if type(proof_expiry) in (int, float) and math.isfinite(proof_expiry):
            expiry = min(expiry, proof_expiry)
        if self.context.valid_until_monotonic is not None:
            expiry = min(expiry, self.context.valid_until_monotonic)
        plan.reserve_split_ticket = {
            "heads": tuple(self.config.heads[head] for head in heads),
            "expires_at_monotonic": expiry,
            **{key: proof[key] for key in identity_keys},
            "repair_epochs": repair_epochs,
            "attempt_id": attempt.offered_at,
            # Refuse newly colder raw feedback between the worker decision and
            # actual HA dispatch, even if it has not crossed the house limit.
            "floor_f": max(report["guarded_heat_floor_f"], reading.low - 1e-6),
            "reserve_room": room,
            "probe_sources": sources,
            "changeover_seconds": report["minimum_return_to_heat_budget_seconds"],
        }
        if stage == "isolating":
            report["blocking_reason"] = "awaiting_two_distinct_post_isolation_samples"
            # Explicitly suppress minimum-run prolongation while proving the
            # auxiliary can maintain temperature with the shared unit Off.
            plan.reserve_split_stop_shared = True
            plan.mode, plan.head_targets_f, plan.head_call_targets_f, plan.head_protections = "off", {}, {}, {}
            plan.reasons.append("Auxiliary heat acknowledged; waiting for independent room feedback with shared heads Off")
            return
        plan.mode, plan.head_targets_f, plan.head_protections = "cool", heads, {}
        plan.head_call_targets_f = {
            head: call_target_f(self.config, self.config.heads[head], "cool", target, states, climate_unit)
            for head, target in heads.items()}
        report["shared_priority"] = "guarded_cooling"
        report["independent_capacity"] = "observed_for_current_attempt_only"
        report.pop("blocking_reason", None)
        plan.reasons.append("Auxiliary supports reserve from rolling post-isolation evidence")

    def trim_transfer_fans(self, plan, states, rooms, now):
        """An ambient gradient cannot override possible conditioned discharge."""
        self.transfer_airflow.observe(states, now, plan)
        for key, fan in self.fan_controls.items():
            if fan.strategy != "transfer":
                continue
            destination = rooms.get(fan.room)
            target = plan.room_targets.get(fan.room, {}).get("target_f")
            if destination is None or target is None:
                continue
            source_head = self.config.zones[fan.source_room].head
            direction = self.transfer_airflow.opposing_direction(
                source_head, destination.temperature, target)
            if direction:
                minimum = math.ceil(fan.purification_minimum)
                if plan.fans.get(fan.entity_id, 0) > minimum:
                    label = self.config.zones[fan.room].name or fan.room
                    warning = (f"Transfer fan {fan.entity_id}: {direction} at its source could move "
                               f"{label} away from its {target:g}°F target; "
                               + (f"transfer boost stopped; purification minimum {minimum}% retained"
                                  if minimum else "fan stopped"))
                    if warning not in plan.warnings:
                        plan.warnings.append(warning)
                plan.fans[fan.entity_id] = minimum
                plan.fan_head_assists.pop(fan.entity_id, None)
                self.transfer_on[key] = False

    def band(self, name: str, settings: Settings, now: datetime) -> tuple[float, float, float]:
        profile = settings.profile(self.config.zones[name], now, self.config.timezone, name)
        low, high = limits(self.config, self.context)
        target = settings.value(profile.target_f) + profile.target_offset_f
        half = settings.value(profile.half_band_f)
        if name in settings.protection_only_rooms:
            return low, high, (low + high) / 2
        return (max(low, settings.value(profile.low_f) if profile.low_f is not None else target - half),
                min(high, settings.value(profile.high_f) if profile.high_f is not None else target + half), target)

    def demand(self, name: str, reading: Reading, band: tuple, priority: float, *, running_modes=None,
               update=True) -> dict:
        LOW, HIGH = limits(self.config, self.context)
        low, high, target = band
        heat_drift = forecast_slope(reading, "heat") * self.config.forecast_minutes
        cool_drift = forecast_slope(reading, "cool") * self.config.forecast_minutes
        future_low, future_high = reading.confirmed_low + heat_drift, reading.confirmed_high + cool_drift
        # Comfort follows the fused room estimate. Persistent probe extremes
        # remain independent evidence for system-limit protection below.
        temperature = reading.temperature
        heat_edge, cool_edge = max(low, LOW + 1), min(high, HIGH - 1)
        stops = self.stop_targets(band, comfort_enabled=self._profiles[name].comfort_enabled,
                                  protection_only=name in self._protection_only)
        heat_stop, cool_stop = stops["heat"], stops["cool"]
        heat = self.calls.get((name, "heat"), False)
        cool = self.calls.get((name, "cool"), False)
        previously_active = {"heat": heat, "cool": cool}
        if running_modes is not None:
            # A requested call that has not started has no target-reaching
            # hysteresis to retain after its original evidence clears.
            if (name, "heat") in self.forecast_pending and "heat" not in running_modes:
                heat = False
            if (name, "cool") in self.forecast_pending and "cool" not in running_modes:
                cool = False
        heat = (temperature < heat_stop or future_low < LOW + 1) if heat else (temperature < heat_edge or future_low < LOW + 1)
        cool = (temperature > cool_stop or future_high > HIGH - 1) if cool else (temperature > cool_edge or future_high > HIGH - 1)
        if name in self._protection_only:
            # No ordinary band/target hysteresis survives entering Protection Only.
            heat = protection_requested(self.config, reading, "heat", context=self.context, name=name)
            cool = protection_requested(self.config, reading, "cool", context=self.context, name=name)
        if update:
            self.calls[name, "heat"], self.calls[name, "cool"] = heat, cool
            for mode, active in (("heat", heat), ("cool", cool)):
                key = (name, mode)
                actual = self.actual_demand(reading, band, mode)
                if not active or actual or (running_modes is not None and mode in running_modes):
                    self.forecast_pending.discard(key)
                elif not actual and not previously_active[mode]:
                    self.forecast_pending.add(key)
        output = {}
        for mode, active, amount, violation, guard in (
            ("heat", heat, heat_stop - min(temperature, temperature + heat_drift), LOW - reading.confirmed_low, LOW + 1 - future_low),
            ("cool", cool, max(temperature, temperature + cool_drift) - cool_stop, reading.confirmed_high - HIGH, future_high - (HIGH - 1)),
        ):
            amount = max(.05, amount) if active else 0
            urgent = emergency(self.context, name, mode)
            actual_emergency = bool(urgent.get("active"))
            measured_violation = float(urgent.get("violation_f", 0))
            protective = actual_emergency or violation > 0 or guard > 0
            effective_priority = priority
            if window_affected(self.context, name, mode) and not protective:
                effective_priority *= WINDOW_COMFORT_FACTOR
            severity = (1 + measured_violation if actual_emergency else 0) if self.context.enabled else max(0, violation)
            output[mode] = {
                "active": active, "target": heat_stop if mode == "heat" else cool_stop,
                "score": (severity, max(0, guard), effective_priority if active else 0, amount),
                "emergency": actual_emergency, "violation_f": measured_violation,
                "base_priority": priority,
            }
        return output

    def stop_targets(self, band, *, comfort_enabled=True, protection_only=False):
        low, high, target = band
        if protection_only:
            minimum, maximum = limits(self.config, self.context)
            margin = self.context.release_hysteresis_f if self.context.enabled else 1.5
            return {"heat": minimum + margin, "cool": maximum - margin}
        if not comfort_enabled:
            # Limits-only periods defend this room's scheduled bounds without
            # driving it to the ordinary comfort target. A small inward margin
            # avoids toggling at an exact boundary, including narrow bands.
            margin = min(.5, max(0.0, (high - low) / 2))
            return {"heat": low + margin, "cool": high - margin}
        if self.context.enabled:
            minimum, maximum = limits(self.config, self.context)
            margin = self.context.release_hysteresis_f
            return {"heat": max(target, minimum + margin) if comfort_enabled else minimum + margin,
                    "cool": min(target, maximum - margin) if comfort_enabled else maximum - margin}
        return {"heat": target if low > self.config.low_f else self.config.low_f + 1.5,
                "cool": target if high < self.config.high_f else self.config.high_f - 1.5}

    def actual_demand(self, reading, band, mode):
        low, high = limits(self.config, self.context)
        if mode == "heat":
            return (reading.temperature < max(band[0], low + 1)
                    or reading.confirmed_low <= low + 1)
        return (reading.temperature > min(band[1], high - 1)
                or reading.confirmed_high >= high - 1)

    def unavailable_request(self, head, mode, rooms, targets, states, climate_unit="°F"):
        """Fresh unmet demand without changing call latches or eligibility."""
        entity = self.config.heads[head]
        attrs = states.get(entity, {}).get("attributes", {})
        modes = set(attrs.get("hvac_modes") or ())
        missing = sorted({"off", mode} - modes)
        if not modes or not missing or self.head_guard.constraint(head, mode, rooms, targets, states):
            return None
        floor = self.config.head_cool_min_f.get(entity) if mode == "cool" else None
        if floor is not None or uses_whole_fahrenheit(self.config, entity, climate_unit):
            try:
                head_native_target(self.config, entity, mode,
                                   floor if floor is not None else to_f(float(attrs["min_temp"]), climate_unit),
                                   attrs, climate_unit)
            except ValueError:
                return None
            except (KeyError, TypeError, OverflowError):
                pass
        requests = []
        protection = self.head_guard.protection(head, mode, rooms, targets)
        for name, zone in self.config.zones.items():
            if zone.head != head or name not in rooms or name not in targets:
                continue
            target = targets[name]
            band = (target["low_f"], target["high_f"], target["target_f"])
            demand = self.demand(name, rooms[name], band, target["priority"],
                                 running_modes={states.get(entity, {}).get("state")}, update=False)[mode]
            if protection and name in protection["rooms"]:
                score = demand["score"]
                demand["active"] = True
                demand["score"] = (score[0], max(.05, score[1]), target["priority"], max(.05, score[3]))
            if demand["active"]:
                requests.append(demand)
        if not requests:
            return None
        chosen = max(requests, key=lambda request: request["score"])
        score = chosen["score"]
        return {"score": score, "target_f": chosen["target"], "missing_modes": missing,
                "emergency": chosen.get("emergency", False), "violation_f": chosen.get("violation_f", 0)}

    def decide(self, states: dict, now: datetime, settings: Settings, climate_unit: str = "°F", *, context=None,
               monotonic_now=None, reserve_proof_supported=False, reserve_proof=None,
               reserve_isolated_at=None) -> Plan:
        settings = self.seasonal_settings(states, now, settings)
        self.fan_controls = {
            key: replace(fan,
                         minimum_percentage=settings.room_fan_minimums.get(fan.room, fan.minimum_percentage),
                         speed_override=settings.room_fan_overrides.get(fan.room, fan.speed_override))
            for key, fan in self.config.fan_controls.items()}
        self.context = context if context is not None else self.operating.environment(states, now)
        LOW, HIGH = limits(self.config, self.context)
        profiles = {key: settings.profile(room, now, self.config.timezone, key) for key, room in self.config.zones.items()}
        self._profiles = profiles
        self._protection_only = settings.protection_only_rooms
        bands = {name: self.band(name, settings, now) for name in self.config.zones}
        rooms, warnings = self.sensors.read(states, now, climate_unit,
                                            targets={name: self.stop_targets(band, comfort_enabled=profiles[name].comfort_enabled, protection_only=name in self._protection_only)
                                                     for name, band in bands.items()},
                                            calls=self.calls, head_requests=self.head_requests, protection=self.context)
        if context is None:
            self.context = self.operating.rooms(self.context, states, rooms, bands, profiles, settings, now)
            if self.context.enabled:
                deadline = self.sensors.confirmation_deadline(now, self.operating.clock())
                if deadline is not None:
                    current = self.context.valid_until_monotonic
                    self.context = replace(self.context, valid_until_monotonic=deadline if current is None else min(current, deadline))
        self.head_guard.context = self.context
        plan = Plan(rooms={k: asdict(v) for k, v in rooms.items()}, warnings=warnings,
                    fan_speed_overrides={fan.entity_id: fan.speed_override
                                         for fan in self.fan_controls.values()},
                    fan_minimum_percentages={fan.entity_id: fan.minimum_percentage
                                             for fan in self.fan_controls.values()})
        if self.operating.outdoor_observation is not None:
            sampled_at, samples = self.operating.outdoor_observation
            plan.outdoor_source_evidence = {"sampled_at": sampled_at,
                                            "sources": [asdict(sample) for sample in samples]}
        if self.seasonal_status is not None:
            plan.seasonal = self.seasonal_status.as_dict()
        plan.window_states = self.context.window_states
        plan.protection_context = self.context.as_dict()
        plan.decision_valid_until_monotonic = self.context.valid_until_monotonic
        plan.warnings.extend(self.context.warnings)
        # Purification is independent of thermal demand/probe availability.
        # Thermal transfer may boost this floor but cannot remove it.
        plan.fans = {fan.entity_id: math.ceil(fan.purification_minimum)
                     for fan in self.fan_controls.values()}
        demands = {name: self.demand(name, reading, bands[name], profiles[name].priority,
                                    running_modes={states.get(self.config.heads[self.config.zones[name].head], {}).get("state")})
                   for name, reading in rooms.items()}
        # Report the exact active profile used for this decision. Presentation
        # must not re-evaluate schedules in a different timezone or helper snapshot.
        plan.room_targets = {
            name: {"low_f": band[0], "high_f": band[1], "target_f": band[2],
                   "comfort_enabled": profiles[name].comfort_enabled and name not in self._protection_only,
                   "protection_only": name in self._protection_only,
                   "priority": profiles[name].priority,
                   "effective_comfort_priority": {
                       mode: profiles[name].priority * (WINDOW_COMFORT_FACTOR if window_affected(self.context, name, mode) else 1)
                       for mode in ("heat", "cool")},
                   "heat_requested": demands.get(name, {}).get("heat", {}).get("active", False),
                   "cool_requested": demands.get(name, {}).get("cool", {}).get("active", False)}
            for name, band in bands.items()
        }
        self.head_guard.observe(rooms, plan.room_targets, states)
        for (_head, mode), protection in self.head_guard.protections.items():
            for name in protection["rooms"]:
                # A recovering house call remains a real request even when an
                # improving forecast would otherwise clear ordinary demand.
                request = demands[name][mode]
                previously_requested = request["active"]
                request["active"] = True
                score = request["score"]
                request["score"] = (score[0], max(.05, score[1]), profiles[name].priority,
                                    max(.05, score[3]))
                plan.room_targets[name][f"{mode}_requested"] = True
                self.calls[name, mode] = True
                reading = rooms[name]
                actual = self.actual_demand(reading, bands[name], mode)
                if (not previously_requested and not actual
                        and name not in protection.get("started_rooms", protection["rooms"])):
                    self.forecast_pending.add((name, mode))
        for key in list(self.calls):
            if key[0] not in rooms:
                self.calls.pop(key)
                self.forecast_pending.discard(key)

        # Auxiliary capabilities do not remove this room's heat-pump demand.
        auxiliary = rooms.get(self.config.auxiliary_room)
        heater_available = bool(self.config.heater_entity) and states.get(
            self.config.heater_entity, {}).get("state") not in (None, "unknown", "unavailable")
        heater_modes = states.get(self.config.heater_entity, {}).get("attributes", {}).get("hvac_modes") or []
        heater_can_heat = heater_available and (not heater_modes or {"off", "heat"} <= set(heater_modes))
        if auxiliary and heater_available and not heater_can_heat and demands[self.config.auxiliary_room]["heat"]["active"]:
            plan.warnings.append(
                "Auxiliary heat requested but unavailable (HA modes: " + ", ".join(heater_modes) + ")")
        fan_enabled = settings.toggles["auxiliary_fan_enabled"]
        self._auxiliary_fan_enabled = fan_enabled
        if (auxiliary and heater_available and fan_enabled
                and demands[self.config.auxiliary_room]["cool"]["active"] and "fan_only" not in heater_modes):
            plan.warnings.append("Auxiliary heater does not expose fan_only; circulation skipped")

        for key, fan in self.fan_controls.items():
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
            if fan.room in self._protection_only:
                active = False
                self.transfer_on[key] = False
            plan.fans[fan.entity_id] = fan.requested_percentage(percentage if active else 0)

        eligible = {"heat": {}, "cool": {}}
        scores = {"heat": (0., 0., 0., 0.), "cool": (0., 0., 0., 0.)}
        for head in self.config.heads:
            names = [name for name, zone in self.config.zones.items() if zone.head == head]
            if any(name not in rooms for name in names):
                continue
            for mode in ("heat", "cool"):
                requests = []
                protection = self.head_guard.protection(head, mode, rooms, plan.room_targets)
                for name in names:
                    if demands[name][mode]["active"]:
                        requests.append(demands[name][mode])
                if not requests:
                    continue
                modes = states.get(self.config.heads[head], {}).get("attributes", {}).get("hvac_modes") or []
                if modes and not {"off", mode} <= set(modes):
                    # Keep the unmet request visible to recovery after mode
                    # filtering, but never prioritize a call blocked by current
                    # room/house constraints. This is evidence, not eligibility.
                    if missing_request := self.unavailable_request(
                            head, mode, rooms, plan.room_targets, states, climate_unit):
                        plan.unavailable_mode_requests.setdefault(head, {})[mode] = missing_request
                    labels = ", ".join(self.config.zones[name].name or name for name in names)
                    plan.warnings.append(
                        f"{labels}: {mode} requested but unavailable (HA modes: {', '.join(modes)})")
                    continue
                entity = self.config.heads[head]
                floor = self.config.head_cool_min_f.get(entity) if mode == "cool" else None
                if floor is not None or uses_whole_fahrenheit(self.config, entity, climate_unit):
                    attrs = states.get(entity, {}).get("attributes", {})
                    try:
                        # Missing range metadata is handled at execution.
                        # Only a known range/grid conflict removes this call
                        # from shared arbitration.
                        lower, upper = float(attrs["min_temp"]), float(attrs["max_temp"])
                        if not all(math.isfinite(value) for value in (lower, upper)) or lower > upper:
                            raise ValueError("Invalid climate temperature range")
                        test_target = floor if floor is not None else to_f(lower, climate_unit)
                        if not uses_whole_fahrenheit(self.config, entity, climate_unit):
                            native_target(test_target, attrs, climate_unit)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        pass
                    else:
                        try:
                            head_native_target(self.config, entity, mode, test_target, attrs, climate_unit)
                        except ValueError as exc:
                            plan.warnings.append(f"{head}: {mode}ing unavailable because {exc}")
                            continue
                constraint = self.head_guard.constraint(head, mode, rooms, plan.room_targets, states)
                if constraint:
                    plan.head_constraints.setdefault(head, {})[mode] = constraint
                    plan.warnings.append(constraint["reason"])
                    continue
                chosen = max(requests, key=lambda request: request["score"])
                score = chosen["score"]
                protection = self.head_guard.protection(head, mode, rooms, plan.room_targets)
                if protection:
                    # Keep a recovering protection call above soft comfort until
                    # its originating room reaches its normal stop target.
                    score = (score[0], max(.05, score[1]), score[2], score[3])
                eligible[mode][head] = chosen["target"]
                # Worst room wins in each safety/priority tier; room count cannot outvote a limit.
                scores[mode] = tuple(max(a, b) for a, b in zip(scores[mode], score))

        available = [mode for mode in ("heat", "cool") if eligible[mode]]
        emergencies = emergency_magnitudes(self.context)
        if len(emergencies) == 2:
            plan.warnings.append(
                f"Simultaneous protection emergencies: heat {emergencies['heat']:g}°F and cool {emergencies['cool']:g}°F beyond bounds; "
                f"{self.context.preferred_emergency_mode} is preferred by violation magnitude or qualified outdoor tie rule; "
                "shared equipment can serve only one direction and opposing hard guards remain")
        preferred = self.context.preferred_emergency_mode
        if preferred and not eligible[preferred]:
            plan.warnings.append(
                f"Preferred {preferred} protection is not currently feasible; unavailable modes or opposing hard guards remain in force")
        def rank(mode):
            score = scores[mode]
            if self.context.enabled and score[0] >= 1:
                # The first tier is 1 + measured violation, including an exact
                # 80F cooling trip with zero degrees of excess. Weather wins
                # only an exact equal-magnitude tie; comfort priority cannot.
                return score[0], mode == self.context.tie_mode, score[1:], mode == self.selected_mode
            return score[0], False, score[1:], mode == self.selected_mode
        winner = max(available, key=rank) if available else "off"
        if all(eligible.values()):
            plan.warnings.append("Simultaneous heat/cool demand: shared equipment cannot satisfy both at once")
        if winner != "off" and self.last_direction and winner != self.last_direction:
            if not self.candidate or self.candidate[0] != winner:
                self.candidate = (winner, now)
            held = (now - self.mode_since).total_seconds() if self.mode_since else float("inf")
            candidate_age = (now - self.candidate[1]).total_seconds()
            confirmed = candidate_age >= self.config.switch_confirmation_seconds
            # House protection may bypass comfort dwell, but never hardware off-time.
            emergency = scores[winner][0] > scores[self.last_direction][0] or (
                scores[winner][1] > scores[self.last_direction][1]
                and any(self.head_guard.protection(head, winner, rooms, plan.room_targets)
                        for head in eligible[winner]))
            old_score = scores[self.last_direction]
            # For comfort-only switches require a meaningful advantage, not tiny fluctuations.
            advantage = scores[winner][:3] > old_score[:3] or scores[winner][3] > old_score[3] + .5
            if self.context.enabled and scores[winner][0] >= 1:
                # Detection and ranking are immediate. Two already-active
                # opposing emergencies still confirm a changed direction to
                # avoid repeated stop/start intent from alternating readings.
                # Emergency arbitration bypasses the ordinary comfort dwell.
                if old_score[0] >= 1 and not confirmed:
                    winner = self.last_direction if eligible[self.last_direction] else "off"
                    plan.reasons.append("Confirming changed emergency direction; both protection requests remain active")
            elif not emergency and (held < settings["mode_hold_minutes"] * 60 or not confirmed or not advantage):
                winner = self.last_direction if eligible[self.last_direction] else "off"
                plan.reasons.append("Holding shared direction to avoid frequent mode changes")
                if winner == "off" and advantage:
                    # Both timers run in parallel. This is only the earliest
                    # policy eligibility, not a prediction of equipment startup.
                    plan.mode_hold_remaining_seconds = max(
                        settings["mode_hold_minutes"] * 60 - held,
                        self.config.switch_confirmation_seconds - candidate_age,
                    )
        else:
            self.candidate = None
        if winner != "off" and winner != self.last_direction:
            self.last_direction, self.mode_since = winner, now
            self.candidate = None
        self.selected_mode = winner
        plan.mode = winner
        plan.mode_scores = {mode: score for mode, score in scores.items()}
        plan.head_targets_f = eligible.get(winner, {})
        self.head_requests = {head: winner for head in plan.head_targets_f}
        plan.head_protections = {
            head: protection for head in plan.head_targets_f
            if (protection := self.head_guard.protection(head, winner, rooms, plan.room_targets))
        }
        plan.head_call_targets_f = {
            key: call_target_f(self.config, self.config.heads[key], winner, target, states, climate_unit)
            for key, target in plan.head_targets_f.items()
        }
        self.update_auxiliary(plan, states, rooms, now)
        plan.reserve_split = observe_reserve_split(self, plan, states, rooms, now, eligible["cool"])
        if reserve_proof_supported or self.reserve_attempt.stage != "idle":
            tick = self.operating.clock() if monotonic_now is None else monotonic_now
            self._apply_reserve_split(plan, states, rooms, now, tick, eligible, reserve_proof,
                                      reserve_isolated_at, climate_unit)
            winner = plan.mode
            self.head_requests = {head: winner for head in plan.head_targets_f}
        if (plan.mode != "off" or plan.reserve_split_stop_shared or plan.reserve_split_stop_cooling
                or plan.reserve_split_ticket):
            plan.mode_hold_remaining_seconds = None
        # A transfer fan can distribute conditioned air before the source
        # area's ambient probe reflects the head's output. Only assist a head
        # that survived the complete shared-mode arbitration and safety checks.
        for key, fan in self.fan_controls.items():
            if fan.strategy != "transfer" or fan.room in self._protection_only:
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
                plan.fans[fan.entity_id] = fan.requested_percentage(settings.value(fan.percentage))
                if plan.fans[fan.entity_id]:
                    plan.fan_head_assists[fan.entity_id] = head
                self.transfer_on[key] = True
        self.trim_transfer_fans(plan, states, rooms, now)
        self._airflow_settings = settings
        self.directed_airflow.update(plan, rooms, now)
        if winner != "off":
            plan.reasons.append(f"{winner}: " + ", ".join(plan.head_targets_f))
        else:
            plan.reasons.append("No eligible shared-system call")
        if plan.heater_on:
            plan.reasons.append("Auxiliary heating using external room probes")
        elif plan.auxiliary_fan_only:
            plan.reasons.append("Auxiliary heater circulating air in fan-only mode")
        from .recovery_inputs import signature
        plan.recovery_inputs = signature(self.config, states, now, settings, climate_unit)
        from .window_authorization import refresh_window_authorizations
        tick = self.operating.clock() if monotonic_now is None else monotonic_now
        refresh_window_authorizations(plan, self.config, states, now, settings,
            monotonic_now=tick, expires_at_monotonic=tick + self.config.decision_interval_seconds,
            climate_unit=climate_unit)
        from .head_fan_investigation import investigation_snapshot
        plan.head_fan_investigation = investigation_snapshot(self.config, plan, states, now)
        return plan
