"""Qualify and track a guarded cooling attempt with auxiliary reserve heat.

Room trend, device feedback and the electrical interlock are prerequisites,
not proof of capacity when the shared head can still be heating. Neither the
diagnostic nor the rolling state machine dispatches or renews a native lease.
"""
from datetime import datetime
import math

from .auxiliary_dispatch import manual_auxiliary
from .protection_context import emergency_magnitudes, limits
from .sensors import forecast_slope
from .temperature_guard import STOP_MARGIN_F


def _fresh_stamp(stamp, now, max_age):
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp)
            stamp = parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return False
    return (type(stamp) in (float, int) and math.isfinite(stamp)
            and 0 <= now - stamp <= max_age)


def _native_fresh(state, now, maximum_age):
    attrs = state.get("attributes", {})
    return (state.get("state") not in (None, "unknown", "unavailable")
            and not attrs.get("assumed_state") and not attrs.get("restored")
            and not (state.get("integration_repair") or {}).get("held")
            and _fresh_stamp(state.get("last_reported"), now, maximum_age))


def _room_fresh(reading, now, maximum_age):
    return (reading is not None and bool(reading.observation_id)
            and _fresh_stamp(reading.observed_at, now, maximum_age))


def observe(policy, plan, states, rooms, now, candidate_heads):
    """Snapshot of prerequisites from the current plan and qualified reports."""
    config, context = policy.config, policy.context
    name = config.auxiliary_room
    if config.legacy or not context.enabled or not name or not config.heater_entity:
        return {}
    heat_head = config.zones[name].head
    reserve = plan.head_protections.get(heat_head, {})
    if (plan.mode != "heat" or reserve.get("mode") != "heat" or reserve.get("kind") != "reserve"
            or name not in reserve.get("rooms", ())):
        return {}
    instant = now.timestamp()
    maximum_age = min(config.sensor_stale_seconds, config.auxiliary_heartbeat_timeout_seconds)
    # Require actual cooling demand, not a forecast-only call. A cached
    # candidate is rechecked and may be removed on a maintenance-only tick;
    # only a full arbitration can discover a new candidate.
    cooling_heads = sorted(head for head in candidate_heads if head != heat_head
        and not policy.head_guard.constraint(head, "cool", rooms, plan.room_targets, states)
        and any(
            zone.head == head and room in rooms
            and not plan.room_targets[room]["protection_only"]
            and policy.actual_demand(
                rooms[room], tuple(plan.room_targets[room][key] for key in ("low_f", "high_f", "target_f")), "cool")
            for room, zone in config.zones.items()))
    if not cooling_heads:
        return {}

    def measured_weighted_deficit(room, mode):
        reading, target = rooms.get(room), plan.room_targets[room]
        if reading is None or target["protection_only"]:
            return 0.
        # Compare ordinary measured deficits only, never offset or suppress
        # any emergency/reserve vote. Room priorities still reflect a window.
        if mode == "heat":
            deviation = target["low_f"] - reading.temperature
        else:
            deviation = reading.temperature - target["high_f"]
        return max(0., deviation) * target["effective_comfort_priority"][mode]

    heating_deficit = measured_weighted_deficit(name, "heat")
    cooling_deficit = max(measured_weighted_deficit(room, "cool")
        for room, zone in config.zones.items() if zone.head in cooling_heads)
    # A budget for two changeovers, not a worst-case guarantee: unresolved
    # Off acknowledgements or faults can take longer and veto any future split.
    changeover = (config.switch_confirmation_seconds + config.command_timeout_seconds
                  + max(config.off_settle_seconds, config.head_minimum_off_seconds)
                  + config.head_activation_timeout_seconds + config.poll_seconds)
    horizon = 2 * changeover
    low, _ = limits(config, context)
    floor_guard = low + STOP_MARGIN_F
    result = {
        "mode": "observe_only",
        "shared_priority": "retain_heat",
        "independent_capacity": "unproven",
        "reserve_room": name,
        "reserve_head": heat_head,
        "cooling_heads": cooling_heads,
        "weighted_heating_deficit": heating_deficit,
        "weighted_cooling_deficit": cooling_deficit,
        "stronger_cooling_by_measured_deficit": cooling_deficit > heating_deficit,
        "evaluated_at": now.isoformat(),
        "heating_floor_f": low,
        "guarded_heat_floor_f": floor_guard,
        "shared_heat_stop_f": plan.head_targets_f[heat_head],
        "minimum_changeover_and_return_budget_seconds": horizon,
        "minimum_return_to_heat_budget_seconds": changeover,
    }

    def stop(reason):
        return {**result, "blocking_reason": reason}

    if emergency_magnitudes(context):
        return stop("house_emergency")
    if (set(plan.head_targets_f) != {heat_head} or set(reserve["rooms"]) != {name}
            or any(room != name and target.get("heat_requested")
                   for room, target in plan.room_targets.items())):
        return stop("additional_shared_heat_demand")
    if any(not _native_fresh(states.get(entity, {}), instant, maximum_age)
           for entity in config.heads.values()):
        return stop("shared_head_feedback_unqualified")
    for head in cooling_heads:
        attached = [room for room, zone in config.zones.items() if zone.head == head]
        if any(not _room_fresh(rooms.get(room), instant, config.sensor_stale_seconds) for room in attached):
            return stop("cooling_room_feedback_unqualified")
        if not {"off", "cool"} <= set(states[config.heads[head]].get("attributes", {}).get("hvac_modes") or ()):
            return stop("cooling_mode_unqualified")
    if policy.auxiliary_dispatch.peak(now):
        return stop("peak_hours")
    heater = states.get(config.heater_entity, {})
    attrs = heater.get("attributes", {})
    if manual_auxiliary(config, states):
        return stop("auxiliary_manual")
    if (attrs.get("home_climate_auxiliary_blocked") is not False
            or attrs.get("home_climate_auxiliary_soc_grace_active") is not False
            or states.get(config.entity("auxiliary_probe_safe", "binary_sensor"), {}).get("state") != "on"):
        return stop("auxiliary_supply_or_probe_not_ready")
    reading = rooms.get(name)
    if not _room_fresh(reading, instant, config.sensor_stale_seconds):
        return stop("reserve_room_feedback_unqualified")
    slopes = (reading.slope_f_per_minute, forecast_slope(reading, "heat"))
    if not all(math.isfinite(slope) for slope in slopes):
        return stop("reserve_room_feedback_unqualified")
    slope = min(0., *slopes)
    projected = reading.low + slope * changeover / 60
    if not math.isfinite(projected):
        return stop("reserve_room_feedback_unqualified")
    result["observed_trend_projected_low_f"] = round(projected, 2)
    if projected <= floor_guard:
        return stop("insufficient_floor_margin")
    result["preconditions_ready"] = True
    if not plan.heater_on:
        return stop("auxiliary_heat_not_requested")
    lease = states.get(config.entity("auxiliary_authorization", "input_datetime"), {})
    if (not _native_fresh(heater, instant, maximum_age)
            or heater.get("state") != "heat" or attrs.get("hvac_action") != "heating"
            or not _fresh_stamp(lease.get("attributes", {}).get("timestamp"), instant,
                                config.auxiliary_heartbeat_timeout_seconds)):
        return stop("auxiliary_heating_not_observed_under_lease")
    progress = plan.auxiliary_policy.get("progress_f")
    if not plan.auxiliary_policy.get("progress_known") or type(progress) not in (float, int) or not math.isfinite(progress):
        return stop("qualified_recovery_trend_unavailable")
    result["fused_room_progress_f"] = round(progress, 2)
    if progress < 0:
        return stop("room_still_cooling")
    return stop("independent_capacity_not_proven")


class RollingAttempt:
    """Rolling attempt with a separate confirmed-Off observation stage.

    Callers must re-arbitrate before any shared write and supply proven
    *dedicated* heater ownership, receipt/uncertainty/power conditions through
    `ready`; the production caller obtains native proof separately from
    cached planner snapshots. Inputs use one monotonic clock. A caller must normalize acquisition
    timestamps without renewing an unchanged physical report.
    """

    def __init__(self, *, acknowledgement_timeout, evidence_maximum_age, isolation_timeout, confirmation=0):
        if any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0
               for v in (acknowledgement_timeout, evidence_maximum_age, isolation_timeout)):
            raise ValueError("Finite positive trial timeouts are required")
        if type(confirmation) not in (float, int) or not math.isfinite(confirmation) or confirmation < 0:
            raise ValueError("Guarded cooling confirmation must be finite and nonnegative")
        self.confirmation = confirmation
        self.timeout = acknowledgement_timeout
        self.maximum_age = evidence_maximum_age
        self.isolation_timeout = isolation_timeout
        self.reset()

    def reset(self):
        self.stage = "idle"
        self.offered_at = self.last_tick = self.latest = self.ack_seen = None
        self.isolating_at = self.isolated_at = self.isolated_observation = None

    def advance(self, *, now, competing, ready, heat_ack_at, reading, isolated_at=None):
        """Return stage only; never dispatch, change a plan, or renew authority.

        `competing` means shared reserve vs. stronger eligible cooling.
        `ready` includes a conservative return margin and all live safety gates.
        `heat_ack_at` is the normalized monotonic timestamp of a current
        independently verified Heat acknowledgment. `reading` is one
        qualified finite (acquisition stamp, raw low °F) pair acquired during
        this attempt (not an historical slope or capacity estimate).
        `isolated_at` is a confirmed and settled all-head Off barrier.
        """
        if not competing:
            self.reset()
            return self.stage
        valid_now = type(now) in (int, float) and math.isfinite(now) and now >= 0
        valid_reading = (isinstance(reading, tuple) and len(reading) == 2
            and all(type(v) in (int, float) and math.isfinite(v) for v in reading)
            and reading[0] >= 0)
        if (not valid_now or (self.last_tick is not None and now < self.last_tick)
                or ready is not True or not valid_reading
                or not 0 <= now - reading[0] <= self.maximum_age):
            # Rejection while idle isn't an attempted changeover. Losing
            # evidence after offering/activating latches fallback until the
            # conflict itself clears; repeated stale input can't restart it.
            if self.stage != "idle":
                self.stage = "fallback"
            return self.stage
        if self.stage == "fallback":
            return self.stage
        self.last_tick = now
        if self.stage == "idle":
            self.stage = "offered"
            self.offered_at = now
            self.latest = reading
            return self.stage
        if (reading[0] < self.latest[0]
                or (reading[0] == self.latest[0] and reading[1] != self.latest[1])
                or (reading[0] > self.latest[0] and reading[1] < self.latest[1])):
            self.stage = "fallback"
            return self.stage
        acknowledged = (type(heat_ack_at) in (int, float)
                        and self.offered_at < heat_ack_at <= now
                        and now - heat_ack_at <= self.maximum_age)
        if self.stage == "offered":
            if self.ack_seen is None and now >= self.offered_at + self.timeout:
                self.stage = "fallback"
            elif self.ack_seen is not None and not acknowledged:
                self.stage = "fallback"
            elif acknowledged:
                if self.ack_seen is None:
                    self.ack_seen = heat_ack_at
                if (now >= self.offered_at + self.confirmation
                        and reading[0] >= self.ack_seen and reading[0] > self.latest[0]):
                    self.stage, self.isolating_at = "isolating", now
                    self.latest = reading
            # No pre-attempt or repeated read can count as progress.
        elif not acknowledged:
            self.stage = "fallback"
        elif self.stage == "isolating":
            if now >= self.isolating_at + self.isolation_timeout:
                self.stage = "fallback"
            elif (type(isolated_at) in (int, float) and self.isolating_at <= isolated_at
                    and isolated_at < reading[0] <= now):
                if self.isolated_at != isolated_at or self.isolated_observation is None:
                    self.isolated_at, self.isolated_observation = isolated_at, reading[0]
                elif reading[0] > self.isolated_observation:
                    self.stage = "active"
            else:
                self.isolated_observation = None
            self.latest = reading
        else:
            self.latest = reading
        return self.stage
