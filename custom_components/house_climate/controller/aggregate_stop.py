"""Bounded operational corroboration from aggregate AC output and native Off.

This cannot identify which attached load changed, establish electrical isolation,
or prove that a heater remains on. It only combines a caller's current native-Off
evidence with explicitly calibrated, fresh aggregate-output observations. No
outcome requests an actuator action.
"""
from collections import deque
from dataclasses import asdict, dataclass
import math


MAX_BASELINE_SAMPLES = 128


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _output_sample(now, power, reported_at, max_age_seconds):
    power = _number(power, "AC output")
    reported_at = _number(reported_at, "AC output report timestamp")
    if power < 0:
        raise ValueError("AC output must be nonnegative")
    if reported_at > now:
        raise ValueError("AC output report is future-dated")
    if now - reported_at > max_age_seconds:
        raise ValueError("AC output report is stale")
    return power, reported_at


@dataclass(frozen=True)
class AggregateStopConfig:
    """Calibration is explicit; there is no default load-drop assumption."""

    minimum_drop_w: float
    residual_ceiling_w: float
    noise_margin_w: float
    settle_seconds: float = 10.0
    timeout_seconds: float = 45.0
    max_age_seconds: float = 30.0
    minimum_samples: int = 2
    relative_drop_enabled: bool = False

    def __post_init__(self):
        for key in ("minimum_drop_w", "residual_ceiling_w", "noise_margin_w",
                    "settle_seconds", "timeout_seconds", "max_age_seconds"):
            object.__setattr__(self, key, _number(getattr(self, key), key))
        if not self.minimum_drop_w > self.noise_margin_w >= 0:
            raise ValueError("minimum_drop_w must exceed nonnegative noise_margin_w")
        if self.residual_ceiling_w <= 0:
            raise ValueError("residual_ceiling_w must be positive")
        if self.settle_seconds < 0 or self.timeout_seconds <= self.settle_seconds:
            raise ValueError("timeout_seconds must exceed nonnegative settle_seconds")
        if self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if isinstance(self.minimum_samples, bool) or not isinstance(self.minimum_samples, int) or self.minimum_samples < 2:
            raise ValueError("minimum_samples must be an integer of at least two")
        if not isinstance(self.relative_drop_enabled, bool):
            raise ValueError("relative_drop_enabled must be a boolean")
        if self.relative_drop_enabled:
            if self.minimum_samples < 3:
                raise ValueError("Relative baseline minimum_samples must be at least three")
            if self.minimum_samples > MAX_BASELINE_SAMPLES:
                raise ValueError(f"Relative baseline minimum_samples cannot exceed {MAX_BASELINE_SAMPLES}")
            if self.settle_seconds > self.max_age_seconds:
                raise ValueError("Relative baseline settle_seconds cannot exceed max_age_seconds")

    @classmethod
    def from_dict(cls, raw):
        required = {"minimum_drop_w", "residual_ceiling_w", "noise_margin_w"}
        optional = {"settle_seconds", "timeout_seconds", "max_age_seconds", "minimum_samples",
                    "relative_drop_enabled"}
        if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
            raise ValueError("Aggregate stop confirmation requires explicit drop, residual and noise calibration")
        return cls(**raw)

    def to_dict(self):
        return asdict(self)

    def create_evidence(self):
        return AggregateStopEvidence(self)

    def create_baseline(self):
        return AggregateStopBaseline(self)


class AggregateStopBaseline:
    """Qualify only recent, continuously observed pre-command output reports.

    The minimum of a stable window is conservative for a later drop comparison.
    No historical peak or heater nameplate estimate is used. Overflow discards
    qualification rather than treating a truncated burst as a complete window.
    """

    def __init__(self, policy):
        if not isinstance(policy, AggregateStopConfig):
            raise TypeError("A validated AggregateStopConfig is required")
        self.policy = policy
        self._samples = deque(maxlen=MAX_BASELINE_SAMPLES)
        self._last_now = None
        self._last_seen = self._last_seen_value = None
        self._after = None

    def _reset(self, *, after=None):
        self._samples.clear()
        if after is not None:
            self._after = after if self._after is None else max(self._after, after)

    def _clock(self, timestamp, *, invalidate_stale=True):
        try:
            now = _number(timestamp, "Baseline observation timestamp")
        except ValueError:
            self._reset(after=self._last_now)
            return None
        if self._last_now is not None and now < self._last_now:
            self._reset(after=self._last_now)
            return None
        self._last_now = now
        if (invalidate_stale and self._last_seen is not None
                and now - self._last_seen > self.policy.max_age_seconds):
            # The actual observation time is the barrier: a later read of an
            # old cached report cannot conceal a known invalidity gap.
            self._reset(after=now)
        cutoff = now - self.policy.max_age_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        return now

    def observe(self, now, power, reported_at):
        """Record one source report; repeated reads do not create reports."""
        now = self._clock(now, invalidate_stale=False)
        if now is None:
            return None
        try:
            power, report = _output_sample(now, power, reported_at, self.policy.max_age_seconds)
            if self._last_seen is not None:
                if report < self._last_seen:
                    raise ValueError("AC output reports arrived out of order")
                if report == self._last_seen and power != self._last_seen_value:
                    raise ValueError("AC output changed without a new report timestamp")
        except ValueError:
            self._reset(after=now)
            return None
        if report == self._last_seen:
            return self.snapshot(now)
        if self._last_seen is not None and report - self._last_seen > self.policy.max_age_seconds:
            self._reset(after=self._last_seen + self.policy.max_age_seconds)
        self._last_seen, self._last_seen_value = report, power
        if self._after is not None and report <= self._after:
            return None
        powers = [sample[1] for sample in self._samples]
        if (len(self._samples) == MAX_BASELINE_SAMPLES or
                (powers and max(max(powers), power) - min(min(powers), power) > self.policy.noise_margin_w)):
            self._reset()
        self._samples.append((report, power))
        return self.snapshot(now)

    def snapshot(self, now):
        """Return a qualified conservative low, or None without fresh evidence."""
        now = self._clock(now)
        if now is None or len(self._samples) < self.policy.minimum_samples:
            return None
        if self._samples[-1][0] - self._samples[0][0] < self.policy.settle_seconds:
            return None
        powers = [sample[1] for sample in self._samples]
        if max(powers) - min(powers) > self.policy.noise_margin_w:
            return None
        return min(powers)


@dataclass(frozen=True)
class AggregateStopOutcome:
    status: str
    reason: str
    classification: str | None = None
    baseline_w: float | None = None
    latest_w: float | None = None
    sample_count: int = 0
    evidence_seconds: float = 0.0
    qualified_baseline_w: float | None = None

    def as_dict(self):
        return asdict(self)


class AggregateStopEvidence:
    """Evaluate one stop attempt using constant-size evidence.

    All times are numeric epoch seconds from the same wall clock. The caller
    supplies the timestamp of a currently valid native Off report, or None when
    Off is not currently established. Advancing report timestamps, not repeated
    reads of cached values, establish the settling interval.
    """

    def __init__(self, policy):
        if not isinstance(policy, AggregateStopConfig):
            raise TypeError("A validated AggregateStopConfig is required")
        self.policy = policy
        self._started = self._last_now = None
        self._baseline = self._latest = None
        self._relative_baseline = None
        self._last_seen = self._last_seen_value = None
        self._off_at = self._last_off = None
        self._window_after = None
        self._terminal = False
        self._reset_window()
        self.outcome = AggregateStopOutcome("unknown", "Stop attempt has not begun")

    def _reset_window(self, *, after=None):
        self._first_post = self._last_post = None
        self._low = self._high = None
        self._samples = 0
        if after is not None:
            self._window_after = after if self._window_after is None else max(self._window_after, after)

    def _result(self, status, reason, classification=None):
        if (status == "pending" and self._started is not None and self._last_now is not None
                and self._last_now - self._started >= self.policy.timeout_seconds):
            self._terminal = True
            self._reset_window()
            status, reason, classification = "unknown", "Aggregate confirmation timed out", None
        span = 0.0 if self._first_post is None else self._last_post - self._first_post
        self.outcome = AggregateStopOutcome(status, reason, classification,
            self._baseline, self._latest, self._samples, span, self._relative_baseline)
        return self.outcome

    def _sample(self, now, power, reported_at):
        return _output_sample(now, power, reported_at, self.policy.max_age_seconds)

    def begin(self, timestamp, power, reported_at, *, qualified_baseline_w=None):
        """Capture a fresh pre-command baseline; invalid baselines need a new attempt."""
        self._started = self._last_now = None
        self._baseline = self._latest = None
        self._relative_baseline = None
        self._last_seen = self._last_seen_value = None
        self._off_at = self._last_off = None
        self._window_after = None
        self._terminal = False
        self._reset_window()
        try:
            now = _number(timestamp, "Stop timestamp")
            self._started = self._last_now = now
            self._baseline, report = self._sample(now, power, reported_at)
            if qualified_baseline_w is not None:
                qualified = _number(qualified_baseline_w, "Qualified pre-command baseline")
                if not 0 <= qualified <= self._baseline:
                    raise ValueError("Qualified pre-command baseline must not exceed fresh begin output")
                self._relative_baseline = qualified
        except ValueError as exc:
            self._terminal = True
            return self._result("unknown", str(exc))
        self._last_seen, self._last_seen_value = report, self._baseline
        self._window_after = now
        if (self.policy.relative_drop_enabled and self._relative_baseline is None
                and self._baseline > self.policy.residual_ceiling_w + self.policy.noise_margin_w):
            self._terminal = True
            return self._result("unknown", "Qualified recent pre-command baseline is unavailable")
        return self._result("pending", "Waiting for native Off and fresh AC output reports")

    def observe(self, now, power, reported_at, native_off_after=None):
        """Return operational evidence, never a conclusion about isolated heater power."""
        if self._terminal or self._started is None:
            return self.outcome
        try:
            now = _number(now, "Observation timestamp")
        except ValueError as exc:
            self._terminal = True
            self._reset_window()
            return self._result("unknown", str(exc))
        if now < self._last_now:
            self._terminal = True
            self._reset_window()
            return self._result("unknown", "Observation clock moved backwards")
        self._last_now = now
        if now - self._started >= self.policy.timeout_seconds and self.outcome.status != "corroborated":
            self._terminal = True
            self._reset_window()
            return self._result("unknown", "Aggregate confirmation timed out")
        try:
            power, report = self._sample(now, power, reported_at)
            if report < self._last_seen:
                raise ValueError("AC output reports arrived out of order")
            if report == self._last_seen and power != self._last_seen_value:
                raise ValueError("AC output changed without a new report timestamp")
        except ValueError as exc:
            self._latest = None
            self._reset_window(after=now)
            return self._result("unknown", str(exc))
        self._latest = power
        if report > self._last_seen + self.policy.max_age_seconds:
            # A polling stall must not hide a gap in valid output evidence.
            self._reset_window(after=self._last_seen + self.policy.max_age_seconds)
        self._last_seen, self._last_seen_value = report, power
        try:
            off = _number(native_off_after, "Native Off report timestamp")
            if not self._started < off <= now:
                raise ValueError("Native Off must be reported after the stop request")
            if now - off > self.policy.max_age_seconds:
                raise ValueError("Native Off report is stale")
            if self._last_off is not None and off < self._last_off:
                raise ValueError("Native Off reports arrived out of order")
        except ValueError as exc:
            self._off_at = self._last_off = None
            self._reset_window(after=now)
            return self._result("unknown", str(exc))
        # A newer unchanged Off report refreshes liveness without moving the
        # barrier past output samples already collected while Off was known.
        if self._last_off is not None and off > self._last_off + self.policy.max_age_seconds:
            self._reset_window()
            self._off_at = off
        self._off_at = off if self._off_at is None else self._off_at
        self._last_off = off
        if report <= max(self._off_at, self._window_after):
            self._reset_window()
            return self._result("pending", "Waiting for an AC output report after native Off and any evidence gap")
        ceiling = self.policy.residual_ceiling_w + self.policy.noise_margin_w
        already_low = self._baseline <= ceiling
        drop_baseline = self._relative_baseline if self.policy.relative_drop_enabled else self._baseline
        credible_drop = (drop_baseline is not None
                        and drop_baseline - power >= self.policy.minimum_drop_w + self.policy.noise_margin_w)
        relative = self.policy.relative_drop_enabled and not already_low
        if not ((already_low and power <= ceiling) or
                (credible_drop and (relative or power <= ceiling))):
            self._reset_window()
            return self._result("pending", "Aggregate output does not yet corroborate Off; other loads may explain it")
        if self._last_post is None or report > self._last_post:
            low = power if self._low is None else min(power, self._low)
            high = power if self._high is None else max(power, self._high)
            if self._first_post is None or high - low > self.policy.noise_margin_w:
                self._reset_window()
                self._first_post = report
                low = high = power
            self._low, self._high = low, high
            self._last_post = report
            self._samples += 1
        span = self._last_post - self._first_post
        if self._samples >= self.policy.minimum_samples and span >= self.policy.settle_seconds:
            if relative:
                return self._result("corroborated", "Native Off is corroborated by a stable relative aggregate-output drop",
                                    "relative_drop")
            return self._result("corroborated", "Native Off is corroborated by stable low aggregate AC output",
                                "already_low" if already_low else "drop")
        return self._result("pending", "Waiting for distinct stable low-output reports")
