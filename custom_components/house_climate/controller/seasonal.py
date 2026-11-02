"""Source-separated outdoor averaging for comfort, independent of freeze limits."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class SeasonalStatus:
    profile: str
    average_f: float | None = None
    coverage_seconds: float = 0
    selected_role: str | None = None
    source_ids: tuple[str, ...] = ()
    degraded: bool = True
    reason: str = "Awaiting qualified outdoor history"
    timestamp_kind: str = "unknown"
    evidence_status: str = "unavailable"

    def as_dict(self):
        return asdict(self)


class SeasonalEvaluator:
    """Integrate accepted held values over time, not a count of cached polls.

    Histories begin when this evaluator actually observes a source. They are
    never backfilled from a recent cache timestamp. A valid unchanged source
    can cover time until its original expiry, but polling does not extend that
    expiry. Weather fallback requires explicit physical observation time.
    Only the selected profile persists; restart rebuilds averaging evidence.
    """

    def __init__(self, settings):
        self.settings = settings
        self.profile = settings.initial_profile
        self.history = {}
        self.last_tick = None

    def dump(self):
        return {"profile": self.profile}

    def restore(self, value):
        if isinstance(value, dict) and value.get("profile") in ("cool", "warm"):
            self.profile = value["profile"]
            return True
        return False

    def _average(self, history, now):
        window = float(self.settings.average_seconds)
        start, covered, total = now - window, 0.0, 0.0
        while len(history) > 1 and history[1][0] <= start:
            history.popleft()
        cursor = start
        for index, (seen, value, expiry, _identity) in enumerate(history):
            end = min(now, expiry, history[index + 1][0] if index + 1 < len(history) else now)
            left = max(start, seen)
            if end <= left:
                continue
            if left > cursor + 1e-6:
                # Only continuous coverage immediately preceding now counts.
                covered = total = 0.0
            duration = end - max(left, cursor)
            if duration > 0:
                covered += duration
                total += duration * value
            cursor = max(cursor, end)
        if cursor < now - 1e-6:
            return None, 0.0
        return (total / covered if covered > 0 else None), covered

    def update(self, reading, now: datetime):
        if not self.settings.enabled:
            return SeasonalStatus(self.profile, degraded=False,
                                  reason="Seasonal comfort selection is disabled",
                                  evidence_status="disabled")
        if now.tzinfo is None:
            return SeasonalStatus(self.profile, reason="Outdoor history requires timezone-aware time")
        tick = now.timestamp()
        if self.last_tick is not None and tick < self.last_tick:
            self.history.clear()
        self.last_tick = tick
        selected, unverified_weather, valid_keys = [], False, set()
        for sample in reading.source_samples:
            if (sample.valid_until <= tick or not math.isfinite(sample.temperature_f)
                    or sample.reported_at > tick):
                continue
            if sample.entity_id.startswith("weather.") and sample.measurement_at is None:
                if sample.role == reading.selected_role:
                    unverified_weather = True
                continue
            key = (sample.role, sample.source)
            valid_keys.add(key)
            history = self.history.setdefault(key, deque())
            identity = (sample.measurement_at, sample.reported_at, sample.temperature_f)
            if not history or history[-1][3] != identity or history[-1][2] <= tick:
                # A new report can extend source validity. Its evidence starts
                # here, never at a retroactive provider or cached timestamp.
                if history and tick == history[-1][0]:
                    history.pop()
                history.append((tick, sample.temperature_f, sample.valid_until, identity))
            if sample.role == reading.selected_role:
                selected.append((sample, *self._average(history, tick)))
        for key, history in self.history.items():
            if key not in valid_keys and history:
                start, value, expiry, identity = history[-1]
                history[-1] = (start, value, min(expiry, tick), identity)
        # Discard long-expired source histories to bound retained data.
        window = float(self.settings.average_seconds)
        self.history = {key: history for key, history in self.history.items()
                        if history and history[-1][2] >= tick - window}
        ids = tuple(sorted(sample.source for sample, _average, _coverage in selected))
        role = reading.selected_role
        degraded = role != "primary"
        if reading.status != "known" or not selected or unverified_weather:
            reason = ("Weather fallback lacks a physical observation timestamp"
                      if unverified_weather else "No qualified outdoor source; retaining last seasonal profile")
            return SeasonalStatus(self.profile, selected_role=role, source_ids=ids, reason=reason,
                                  evidence_status="unverified" if unverified_weather else "unavailable")
        coverage = min(covered for _sample, _average, covered in selected)
        averages = [average for _sample, average, _covered in selected if average is not None]
        average = min(averages) if len(averages) == len(selected) else None
        kind = ("device_observation" if all(sample.measurement_at is not None
                                          for sample, _average, _coverage in selected) else "ha_report")
        if average is None or coverage < window - 1e-6:
            return SeasonalStatus(self.profile, average, coverage, role, ids, degraded,
                                  "Building continuous outdoor averaging history", kind, "warming")
        if average > self.settings.warm_above_f:
            self.profile = "warm"
        elif average < self.settings.cool_below_f:
            self.profile = "cool"
        reason = ("Regional observation fallback supplies seasonal comfort"
                  if degraded else "Local outdoor average supplies seasonal comfort")
        return SeasonalStatus(self.profile, average, coverage, role, ids, degraded, reason, kind, "qualified")
