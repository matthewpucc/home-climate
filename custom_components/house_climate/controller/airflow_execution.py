"""Independent profile acknowledgement queue; speed commands keep their own queue."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .ha import HAActivationDeferred, HAError


def reported(state):
    attrs = state.get("attributes", {})
    return {name: attrs.get(name) for name in (
        "angle_low", "angle_high", "oscillating", "oscillation_enabled", "direction", "percentage")}


def matches_profile(state, profile, outlet="switchable"):
    attrs = state.get("attributes", {})
    return (state.get("state") in ("on", "off") and not attrs.get("restored")
            and not attrs.get("assumed_state") and not state.get("restored")
            and type(attrs.get("angle_low")) is int and type(attrs.get("angle_high")) is int
            and attrs.get("angle_low") == profile.lower_angle
            and attrs.get("angle_high") == profile.upper_angle
            and (outlet == "calibrated_fixed" or attrs.get("direction") == profile.direction)
            and attrs.get("oscillating") is True
            # hass-dyson's oscillating attribute can be optimistic. This extra
            # attribute comes from its reported product-state oson field.
            and attrs.get("oscillation_enabled") is True)


def report_marker(state):
    try:
        value = datetime.fromisoformat((state.get("last_reported") or state.get("last_updated")).replace("Z", "+00:00"))
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def at_minimum(state, minimum):
    if minimum == 0:
        return state.get("state") == "off"
    attrs = state.get("attributes", {})
    try:
        step = float(attrs.get("percentage_step", 1))
        value = attrs["percentage"]
        if not math.isfinite(step) or not 0 < step <= 100 or isinstance(value, bool):
            return False
        target = min(100, math.floor(max(1, math.ceil((math.ceil(minimum) - 1e-7) / step)) * step + 1e-7))
        return state.get("state") == "on" and minimum <= value <= target
    except (TypeError, ValueError, KeyError):
        return False


@dataclass
class ProfilePending:
    profile: str
    identity: tuple
    phase: str = "reduce"
    since: float = 0
    baseline: datetime | None = None
    error: str | None = None
    retry_at: float = 0
    attempts: int = 0
    verified_at: float | None = None


class AirflowExecution:
    def __init__(self, executor):
        self.executor = executor
        self.pending: dict[str, ProfilePending] = {}
        self.verified = {}

    def apply(self, fan, percentage, plan, states, now):
        entity, minimum = fan.entity_id, math.ceil(fan.minimum_percentage)
        intent = plan.fan_airflow.get(entity, {})
        name = intent.get("profile")
        state = states.get(entity, {})
        profile = fan.airflow.profiles.get(name)
        identity = (*tuple(state.get("airflow_identity") or ()),
                    state.get("integration_repair", {}).get("epoch"),
                    state.get("attributes", {}).get("supported_features"),
                    fan.airflow.outlet, fan.airflow.calibration_key, profile)
        status = intent.setdefault("execution", {})
        status.update(reported=reported(state), requested_percentage=percentage,
                      physical_bearing_measured=False,
                      outlet_control=fan.airflow.outlet,
                      outlet_evidence=("Accepted per-device physical sweep calibration; outlet unmeasured at runtime"
                          if fan.airflow.outlet == "calibrated_fixed" else "Requires fresh reported forward outlet"),
                      direction_telemetry_applicable=fan.airflow.outlet == "switchable",
                      last_verified_command=self.verified.get(entity))
        pending = self.pending.get(entity)
        if name is None:
            # Retain the physical angle, never the old positive authorization.
            self.pending.pop(entity, None)
            status.update(phase="minimum", effective_percentage=minimum)
            return minimum, None
        if pending is None or pending.profile != name or pending.identity != identity:
            if pending is not None and pending.identity != identity:
                self.verified.pop(entity, None)
            pending = self.pending[entity] = ProfilePending(name, identity)
        status.update(phase=pending.phase, error=pending.error,
                      last_verified_command=self.verified.get(entity), effective_percentage=minimum)
        api = self.executor.ha
        authorize = getattr(api, "airflow_authorization", None)
        send = getattr(api, "airflow_service", None)
        if not callable(authorize) or not callable(send):
            pending.error = "Direction requires the native guarded airflow adapter; configure it before enabling directional boosts"
            status.update(error=pending.error, phase="unsupported")
            plan.warnings.append(f"Airflow {entity}: {pending.error}; purifier minimum retained")
            return minimum, None
        try:
            ticket = authorize(entity, plan, states, now)
            if pending.phase == "verified":
                if matches_profile(state, profile, fan.airflow.outlet):
                    status.update(phase="verified", effective_percentage=percentage,
                                  last_verified_command=self.verified.get(entity))
                    return percentage, ticket if percentage > minimum else None
                pending.phase = "reduce"
                pending.error = "Reported sweep/outlet no longer matches; boost withheld"
                status.update(phase=pending.phase, error=pending.error)
            if now < pending.retry_at:
                status.update(phase="retry", error=pending.error, retry_in_seconds=pending.retry_at - now)
                return minimum, None
            if pending.phase == "reduce":
                if not at_minimum(state, minimum) or entity in self.executor.pending:
                    return minimum, None
                # A timed-out earlier operation can still complete. Never aim
                # again or boost until its actual service lifetime has ended.
                if not api.airflow_jobs_clear(entity):
                    raise HAError("Prior fan operation still running or uncertain; wait for cleanup")
                pending.baseline = max(report_marker(state) or datetime.min.replace(tzinfo=timezone.utc),
                                       datetime.now(timezone.utc))
                pending.since = now
                if fan.airflow.outlet == "calibrated_fixed":
                    send(ticket, "set_airflow_profile", lower_angle=profile.lower_angle, upper_angle=profile.upper_angle)
                    pending.phase = "angles"
                else:
                    send(ticket, "set_direction", direction=profile.direction)
                    pending.phase = "outlet"
            elif pending.phase in ("outlet", "angles"):
                marker = report_marker(state)
                fresh = (marker is not None and pending.baseline is not None
                         and pending.baseline < marker <= datetime.now(timezone.utc))
                if now - pending.since >= self.executor.config.command_timeout_seconds:
                    raise HAError(f"Fresh {pending.phase} acknowledgement timed out")
                if pending.phase == "outlet" and fresh and state.get("attributes", {}).get("direction") == profile.direction:
                    pending.baseline = max(marker, datetime.now(timezone.utc))
                    send(ticket, "set_airflow_profile", lower_angle=profile.lower_angle, upper_angle=profile.upper_angle)
                    pending.phase = "angles"
                    pending.since = now
                elif pending.phase == "angles" and fresh and matches_profile(state, profile, fan.airflow.outlet):
                    pending.phase, pending.error, pending.attempts = "verified", None, 0
                    pending.verified_at = now
                    self.verified[entity] = {
                        "profile": name, "lower_angle": profile.lower_angle, "upper_angle": profile.upper_angle,
                        "direction": profile.direction, "outlet_control": fan.airflow.outlet,
                        "oscillating": profile.oscillating,
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                        "evidence": ("Fresh matching bounds/oscillation; accepted fixed-outlet calibration; direction telemetry not applicable"
                            if fan.airflow.outlet == "calibrated_fixed" else
                            "Fresh matching HA settings; physical bearing unmeasured")}
                    # Boost on the next fresh plan tick, never in the completion
                    # callback of an old angle command.
            status.update(phase=pending.phase, error=pending.error,
                          last_verified_command=self.verified.get(entity))
        except HAActivationDeferred as exc:
            status.update(error=str(exc), phase="deferred")
        except HAError as exc:
            pending.attempts += 1
            pending.phase, pending.error = "reduce", str(exc)
            pending.retry_at = now + min(300, max(15, self.executor.config.command_timeout_seconds)
                                         * 2 ** min(pending.attempts - 1, 5))
            status.update(error=pending.error, phase="retry", retry_in_seconds=pending.retry_at - now)
        if status.get("error"):
            plan.warnings.append(f"Airflow {entity}: {status['error']}; purifier minimum retained")
        return minimum, None
