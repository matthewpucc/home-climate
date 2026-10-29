"""Bounded evidence for a sustained real-watt wall-load budget.

Input and whole-circuit measurements overlap and are never added or combined
to establish duration. This module neither measures RMS current nor controls
equipment. Its caller supplies a validated voltage-derived budget and handles
independent battery/output protections and all shutdown/recovery decisions.
"""
from dataclasses import asdict, dataclass
import math
from collections.abc import Mapping


ROLES = ("input", "circuit")


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A finite numeric value is required")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("A finite numeric value is required") from None
    if not math.isfinite(value):
        raise ValueError("A finite numeric value is required")
    return value


@dataclass(frozen=True)
class PowerReport:
    source: str
    watts: float
    reported_at: float


@dataclass(frozen=True)
class WallPowerSourceState:
    role: str
    status: str
    reason: str
    source: str | None
    latest_w: float | None
    reported_at: float | None
    first_reported_at: float | None
    report_count: int
    evidence_seconds: float
    minimum_w: float | None


@dataclass(frozen=True)
class WallPowerState:
    budget_w: float | None
    hold_seconds: float
    minimum_reports: int
    sources: tuple[WallPowerSourceState, ...]

    @property
    def qualified_sources(self):
        return tuple(source.role for source in self.sources if source.status == "qualified")

    @property
    def pending_sources(self):
        return tuple(source.role for source in self.sources if source.status == "pending")

    @property
    def unavailable_sources(self):
        return tuple(source.role for source in self.sources if source.status == "unavailable")

    def as_dict(self):
        return {
            "budget_w": self.budget_w,
            "hold_seconds": self.hold_seconds,
            "minimum_reports": self.minimum_reports,
            "qualified_sources": list(self.qualified_sources),
            "pending_sources": list(self.pending_sources),
            "unavailable_sources": list(self.unavailable_sources),
            "sources": {source.role: asdict(source) for source in self.sources},
        }


@dataclass
class _Source:
    source: str | None = None
    last_reported: float | None = None
    last_watts: float | None = None
    last_observed: float | None = None
    first_reported: float | None = None
    first_observed: float | None = None
    minimum_w: float | None = None
    count: int = 0
    after: float | None = None
    status: str = "unavailable"
    reason: str = "Waiting for a source report"

    def clear_streak(self):
        self.first_reported = self.first_observed = self.minimum_w = None
        self.count = 0

    def invalidate(self, after, reason):
        self.clear_streak()
        if after is not None:
            self.after = after if self.after is None else max(self.after, after)
        self.status, self.reason = "unavailable", reason


class WallPowerBudget:
    """Require distinct high reports with both source and observation duration.

All timestamps use the same numeric wall clock. Memory is constant-sized: two
source records, counters capped at minimum_reports, and no history arrays. A
new runtime starts empty. The caller must invalidate any known bad event that
could otherwise be hidden by later coalesced cached-state reads.
    """

    def __init__(self, max_age_seconds=180, hold_seconds=30, minimum_reports=3, *, roles=ROLES):
        self.max_age_seconds = _finite(max_age_seconds)
        self.hold_seconds = _finite(hold_seconds)
        if self.max_age_seconds <= 0 or self.hold_seconds < 0:
            raise ValueError("Positive report age and nonnegative hold duration are required")
        if (isinstance(minimum_reports, bool) or not isinstance(minimum_reports, int)
                or minimum_reports < 3):
            raise ValueError("At least three distinct reports are required")
        self.minimum_reports = minimum_reports
        if (not isinstance(roles, (tuple, list)) or not roles
                or any(not isinstance(role, str) or role not in ROLES for role in roles)
                or len(set(roles)) != len(roles)):
            raise ValueError("Choose distinct supported wall-power roles")
        self.roles = tuple(roles)
        self._sources = {role: _Source() for role in self.roles}
        self._last_now = None
        self._budget = None

    def _invalidate_all(self, after, reason):
        for source in self._sources.values():
            source.invalidate(after, reason)

    def _clock(self, value):
        try:
            now = _finite(value)
        except ValueError:
            self._invalidate_all(self._last_now, "Invalid observation clock")
            return None
        if self._last_now is not None and now < self._last_now:
            # Keep the last known timeline as a barrier. A clock reversal must
            # not make earlier source reports look like new elapsed duration.
            self._invalidate_all(self._last_now, "Observation clock moved backwards")
            return None
        self._last_now = now
        return now

    def invalidate(self, role, now):
        """Fence an invalid or low event before a later cached read hides it."""
        if role not in self._sources:
            raise ValueError("Only configured source roles are supported")
        now = self._clock(now)
        if now is not None:
            self._sources[role].invalidate(now, "Source evidence was invalidated")

    def observe(self, now, budget_w, reports):
        """Observe coherent cached state; duplicate report timestamps add nothing."""
        if not isinstance(reports, Mapping) or set(reports) - set(self.roles):
            raise ValueError("Only configured source roles are supported")
        now = self._clock(now)
        if now is None:
            return self._snapshot()
        try:
            budget = _finite(budget_w)
            if budget <= 0:
                raise ValueError("A positive budget is required")
        except ValueError:
            self._budget = None
            self._invalidate_all(now, "A valid fresh voltage-derived budget is required")
            return self._snapshot()
        self._budget = budget
        for role, source in self._sources.items():
            self._observe_source(source, reports.get(role), now, budget)
        return self._snapshot()

    def _observe_source(self, source, report, now, budget):
        try:
            if not isinstance(report, PowerReport) or not isinstance(report.source, str) or not report.source.strip():
                raise ValueError("Missing or invalid source report")
            watts, stamp = _finite(report.watts), _finite(report.reported_at)
            if watts < 0:
                raise ValueError("Power cannot be negative")
            if stamp > now:
                raise ValueError("Source report is future-dated")
            if now - stamp > self.max_age_seconds:
                raise ValueError("Source report is stale")
            if source.source != report.source:
                source.source = report.source
                source.last_reported = source.last_watts = source.last_observed = None
                source.clear_streak()
                # Identity changes cannot erase a known invalidity barrier.
            if source.last_reported is not None:
                if stamp < source.last_reported:
                    raise ValueError("Source reports arrived out of order")
                if stamp == source.last_reported and watts != source.last_watts:
                    raise ValueError("Power changed without a new report timestamp")
        except ValueError as exc:
            source.invalidate(now, str(exc))
            return
        if source.after is not None and stamp <= source.after:
            source.status, source.reason = "unavailable", "Source report predates an invalidity barrier"
            return
        is_new = source.last_reported is None or stamp > source.last_reported
        if (is_new and source.last_reported is not None
                and stamp - source.last_reported > self.max_age_seconds):
            source.invalidate(source.last_reported + self.max_age_seconds,
                              "Source reports contain a validity gap")
        if source.minimum_w is not None and source.minimum_w < budget:
            # Raised voltage must not retain earlier evidence below the new
            # threshold. Small voltage changes wholly below the streak's low
            # retain qualification rather than resetting on every jitter.
            source.clear_streak()
        if is_new:
            source.last_reported, source.last_watts, source.last_observed = stamp, watts, now
        if watts < budget:
            source.clear_streak()
            source.status, source.reason = "normal", "Reported power is below the wall budget"
            return
        if is_new:
            if source.first_reported is None:
                source.first_reported, source.first_observed = stamp, now
                source.minimum_w = watts
            else:
                source.minimum_w = min(source.minimum_w, watts)
            source.count = min(self.minimum_reports, source.count + 1)
        span = self._span(source)
        qualified = source.count >= self.minimum_reports and span >= self.hold_seconds
        source.status = "qualified" if qualified else "pending"
        source.reason = ("Sustained wall-power budget exceedance" if qualified
                         else "Waiting for distinct reports and sustained wall-power evidence")

    @staticmethod
    def _span(source):
        if source.first_reported is None:
            return 0.0
        # Old but still-fresh cached evidence cannot backdate a new runtime's
        # observation. Polling after a last report cannot finish either span.
        return min(source.last_reported - source.first_reported,
                   source.last_observed - source.first_observed)

    def _snapshot(self):
        return WallPowerState(
            self._budget, self.hold_seconds, self.minimum_reports,
            tuple(WallPowerSourceState(
                role, source.status, source.reason, source.source, source.last_watts,
                source.last_reported, source.first_reported, source.count,
                self._span(source), source.minimum_w)
                for role, source in self._sources.items()))
