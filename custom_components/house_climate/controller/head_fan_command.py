"""Pure, single-attempt fan command lifecycle; not connected to live dispatch.

A HA report corroborates only the requested setting. It never proves a device
poll, physical fan speed, or delivered capacity. Do not use this state machine
as authorization: a future runtime adapter must serialize with HVAC/Off work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math

from .head_fan_investigation import experiment_window, fan_evidence


@dataclass(frozen=True)
class FanContext:
    """A runtime-supplied ownership/plan generation, not a manufacturer API."""
    generation: str
    authorized: bool = False
    owned: bool = False
    safe: bool = False
    hvac_pending: bool = True
    # Resolved by a future adapter from every room served by this head.
    # Conservative default protects unclassified/shared sleeping spaces.
    sleep_sensitive: bool = True
    overnight_authorized: bool = False
    trial_ends_at: datetime | None = None


@dataclass(frozen=True)
class FanResult:
    status: str
    reason: str
    physical_confirmation: str = "unknown"


class FanCommand:
    """One bounded request, never automatic resend or restore after cancellation.

    All elapsed times must come from the same monotonic clock. HA sample IDs
    must identify state events, not repeated reads or Prometheus scrapes.
    A caller must consume a returned request once and report its outcome;
    ambiguous outcomes are observed, never blindly retried.
    """

    def __init__(self, *, settle_seconds: float = 10, deadline_seconds: float = 60):
        for value in (settle_seconds, deadline_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Finite durations required")
        if not 0 <= settle_seconds < deadline_seconds:
            raise ValueError("Settlement must precede deadline")
        self.settle_seconds = settle_seconds
        self.deadline_seconds = deadline_seconds
        self.result = FanResult("idle", "not_requested")
        self._start = None
        self._last_time = None
        self._context = None
        self._mode = None
        self._baseline = None
        self._first_match = None
        self._dispatch_at = None
        self._last_event_at = None
        self._hvac_mode = None
        self._sample_ids = set()

    def _valid_time(self, elapsed):
        valid = (not isinstance(elapsed, bool) and isinstance(elapsed, (int, float))
                 and math.isfinite(elapsed) and
                 (self._last_time is None or elapsed >= self._last_time))
        if valid:
            self._last_time = elapsed
        return valid

    def _gate(self, context, state, wall_now, timezone, budget):
        if not isinstance(context, FanContext) or not isinstance(context.generation, str) or not context.generation:
            return "invalid_context"
        flags = (context.authorized, context.owned, context.safe,
                 context.hvac_pending, context.sleep_sensitive,
                 context.overnight_authorized)
        if any(type(flag) is not bool for flag in flags):
            return "invalid_context"
        if not context.authorized or not context.owned:
            return "not_authorized_or_owned"
        if not context.safe or context.hvac_pending:
            return "safety_or_hvac_work"
        if state.get("state") not in ("heat", "cool"):
            return "not_conditioning"
        end = context.trial_ends_at
        overnight = context.overnight_authorized and not context.sleep_sensitive
        if end is not None or overnight:
            try:
                if (not isinstance(end, datetime) or end.tzinfo is None
                        or end.utcoffset() is None or not isinstance(wall_now, datetime)
                        or wall_now.tzinfo is None or wall_now.utcoffset() is None):
                    return "invalid_trial_end"
                if end.timestamp() - wall_now.timestamp() <= budget + 300:
                    return "insufficient_trial_restoration_time"
            except (ValueError, OverflowError, TypeError):
                return "invalid_trial_end"
        window = experiment_window(wall_now, timezone, trial_seconds=budget)
        if window["cutoff"] is None or (not overnight and not window["eligible"]):
            return "outside_experiment_window"
        return None

    def request(self, mode, state, context, *, elapsed, wall_now: datetime,
                timezone: str, sample_id: str):
        """Return a service payload once, or None. Does not execute anything."""
        if self.result.status != "idle":
            return None
        if not isinstance(state, dict) or not self._valid_time(elapsed):
            self.result = FanResult("blocked", "invalid_evidence")
            return None
        reason = self._gate(context, state, wall_now, timezone, self.deadline_seconds)
        evidence = fan_evidence(state)
        if reason or not isinstance(sample_id, str) or not sample_id:
            self.result = FanResult("blocked", reason or "missing_sample_identity")
            return None
        if mode not in evidence["advertised_modes"] or evidence["reported_mode"] is None:
            self.result = FanResult("blocked", "unsupported_or_unknown_mode")
            return None
        if mode == evidence["reported_mode"]:
            self.result = FanResult("unchanged", "already_reported")
            return None
        self._start = elapsed
        self._context = context.generation
        self._mode = mode
        self._baseline = evidence["reported_mode"]
        self._hvac_mode = state["state"]
        self._sample_ids.add(sample_id)
        self.result = FanResult("dispatch_pending", "request_reserved")
        return {"fan_mode": mode}

    def dispatched(self, *, outcome: str, elapsed: float):
        """Service completion is not an acknowledgment of device behavior."""
        if self.result.status != "dispatch_pending":
            return
        if not self._valid_time(elapsed):
            self.cancel("invalid_or_reversed_clock")
            return
        if elapsed - self._start >= self.deadline_seconds:
            self.result = FanResult("unconfirmed", "deadline_expired")
            return
        # Conservative evidence boundary: service completion, not send start.
        self._dispatch_at = elapsed
        if outcome not in ("accepted", "uncertain", "rejected"):
            raise ValueError("Explicit service outcome required")
        self.result = (FanResult("failed", "service_rejected") if outcome == "rejected"
                       else FanResult("settling", "service_" + outcome))

    def observe(self, state, context, *, elapsed, wall_now: datetime,
                timezone: str, sample_id: str, event_at: float):
        if self.result.status not in ("dispatch_pending", "settling"):
            return self.result
        if not isinstance(state, dict) or not self._valid_time(elapsed):
            return self.cancel("invalid_or_reversed_clock")
        if not isinstance(context, FanContext) or context.generation != self._context:
            return self.cancel("ownership_or_plan_changed")
        if state.get("state") != self._hvac_mode:
            return self.cancel("hvac_direction_changed")
        reason = self._gate(context, state, wall_now, timezone, 0)
        if reason:
            return self.cancel(reason)
        evidence = fan_evidence(state)
        reported = evidence["reported_mode"]
        if self._mode not in evidence["advertised_modes"] or reported is None:
            return self.cancel("capability_or_report_unavailable")
        if reported not in (self._baseline, self._mode):
            return self.cancel("external_mode_change")
        if elapsed - self._start >= self.deadline_seconds:
            self.result = FanResult("unconfirmed", "deadline_expired")
            return self.result
        if not isinstance(sample_id, str) or not sample_id or sample_id in self._sample_ids:
            return self.result
        self._sample_ids.add(sample_id)
        # Do not let unlimited event identifiers grow this bounded request.
        if len(self._sample_ids) > 256:
            return self.cancel("excessive_report_events")
        if (self.result.status == "dispatch_pending" or isinstance(event_at, bool)
                or not isinstance(event_at, (int, float)) or not math.isfinite(event_at)
                or event_at > elapsed or event_at <= self._dispatch_at
                or (self._last_event_at is not None and event_at <= self._last_event_at)):
            return self.result
        self._last_event_at = event_at
        if reported != self._mode:
            self._first_match = None
        elif self._first_match is None:
            self._first_match = event_at
        elif event_at - self._first_match >= self.settle_seconds:
            self.result = FanResult("reported_consistent", "cached_ha_reports_only")
        return self.result

    def cancel(self, reason="cancelled"):
        self.result = FanResult("cancelled", reason)
        return self.result
