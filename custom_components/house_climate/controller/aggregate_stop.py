"""Bounded operational corroboration from aggregate AC output and native Off.

This cannot identify which attached load changed, establish electrical isolation,
or prove that a heater remains on. It only combines a caller's current native-Off
evidence with explicitly calibrated, fresh aggregate-output observations. No
outcome requests an actuator action.
"""
from dataclasses import asdict, dataclass
import math


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

    @classmethod
    def from_dict(cls, raw):
        required = {"minimum_drop_w", "residual_ceiling_w", "noise_margin_w"}
        optional = {"settle_seconds", "timeout_seconds", "max_age_seconds", "minimum_samples"}
        if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
            raise ValueError("Aggregate stop confirmation requires explicit drop, residual and noise calibration")
        return cls(**raw)

    def to_dict(self):
        return asdict(self)

    def create_evidence(self):
        return AggregateStopEvidence(self)


@dataclass(frozen=True)
class AggregateStopOutcome:
    status: str
    reason: str
    classification: str | None = None
    baseline_w: float | None = None
    latest_w: float | None = None
    sample_count: int = 0
    evidence_seconds: float = 0.0

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
            self._baseline, self._latest, self._samples, span)
        return self.outcome

    def _sample(self, now, power, reported_at):
        power = _number(power, "AC output")
        reported_at = _number(reported_at, "AC output report timestamp")
        if power < 0:
            raise ValueError("AC output must be nonnegative")
        if reported_at > now:
            raise ValueError("AC output report is future-dated")
        if now - reported_at > self.policy.max_age_seconds:
            raise ValueError("AC output report is stale")
        return power, reported_at

    def begin(self, timestamp, power, reported_at):
        """Capture a fresh pre-command baseline; invalid baselines need a new attempt."""
        self._started = self._last_now = None
        self._baseline = self._latest = None
        self._last_seen = self._last_seen_value = None
        self._off_at = self._last_off = None
        self._window_after = None
        self._terminal = False
        self._reset_window()
        try:
            now = _number(timestamp, "Stop timestamp")
            self._started = self._last_now = now
            self._baseline, report = self._sample(now, power, reported_at)
        except ValueError as exc:
            self._terminal = True
            return self._result("unknown", str(exc))
        self._last_seen, self._last_seen_value = report, self._baseline
        self._window_after = now
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
        credible_drop = self._baseline - power >= self.policy.minimum_drop_w + self.policy.noise_margin_w
        if power > ceiling or not (already_low or credible_drop):
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
            return self._result("corroborated", "Native Off is corroborated by stable low aggregate AC output",
                                "already_low" if already_low else "drop")
        return self._result("pending", "Waiting for distinct stable low-output reports")
