"""Durable, bounded fan-trial intent, independent of HA and actuator I/O.

The runtime must save a reserved stage *before* issuing its one service call.
Neither a reservation nor a service receipt proves physical fan operation.
Loading this ledger never grants permission to resume an interrupted trial.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
import math
import re


MAX_TRIAL_SECONDS = 12 * 60 * 60
STAGES = frozenset({
    "enrolled", "request_reserved", "active", "restore_reserved",
    "complete", "needs_attention", "cancelled",
})


def _epoch(value, name):
    # Epoch seconds represent UTC unambiguously; accepting bool here would
    # accidentally turn a configuration flag into a valid timestamp.
    if (type(value) not in (int, float) or not 0 <= value <= 253402300799
            or not math.isfinite(value)):
        raise ValueError(f"{name} must be finite UTC epoch seconds")


def _text(value, name, maximum=256):
    if (not isinstance(value, str) or not value.strip()
            or value != value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"Invalid {name}")


def _json_generation(value, depth=0):
    """Allow only bounded JSON values, not Python's permissive NaN encoder."""
    if depth > 8:
        raise ValueError("Ownership generation is too deeply nested")
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        # Lease identifiers do not require arbitrary precision. Bound integers
        # before float conversion or JSON encoding (both may reject huge ints).
        if (type(value) is int and not -(2 ** 63) <= value < 2 ** 63) or not math.isfinite(value):
            raise ValueError("Ownership generation must be finite")
        return
    if isinstance(value, str):
        if len(value) > 256:
            raise ValueError("Ownership generation string is too long")
        return
    if type(value) is list and len(value) <= 32:
        for child in value:
            _json_generation(child, depth + 1)
        return
    if type(value) is dict and len(value) <= 32 and all(type(k) is str for k in value):
        for key, child in value.items():
            _json_generation(key, depth + 1)
            _json_generation(child, depth + 1)
        return
    raise ValueError("Ownership generation must be a bounded JSON value")


@dataclass(frozen=True)
class FanTrial:
    trial_id: str
    entity_id: str
    mode: str
    baseline: str
    starts_at: float
    ends_at: float
    binding_fingerprint: str
    ownership_generation: object
    sleep_sensitive: bool = True
    overnight_authorized: bool = False
    stage: str = "enrolled"
    reason: str = "enrolled"

    def validate(self, now):
        """Validate structure, not authority; expired rows remain inspectable."""
        _epoch(now, "now")
        _epoch(self.starts_at, "starts_at")
        _epoch(self.ends_at, "ends_at")
        if not 0 < self.ends_at - self.starts_at <= MAX_TRIAL_SECONDS:
            raise ValueError("Fan trial must have a positive duration of at most 12 hours")
        for name in ("trial_id", "mode", "baseline", "reason"):
            _text(getattr(self, name), name)
        _text(self.binding_fingerprint, "binding_fingerprint", 4096)
        if (not isinstance(self.entity_id, str)
                or re.fullmatch(r"climate\.[a-z0-9_]+", self.entity_id) is None
                or len(self.entity_id) > 256):
            raise ValueError("Fan trial requires one climate entity")
        if self.mode == self.baseline:
            raise ValueError("Trial mode must differ from the baseline")
        if type(self.sleep_sensitive) is not bool or type(self.overnight_authorized) is not bool:
            raise ValueError("Trial window flags must be booleans")
        if self.sleep_sensitive and self.overnight_authorized:
            raise ValueError("Sleep-sensitive heads cannot authorize overnight experimentation")
        if not isinstance(self.stage, str) or self.stage not in STAGES:
            raise ValueError("Unknown fan trial stage")
        _json_generation(self.ownership_generation)
        if len(json.dumps(self.ownership_generation, allow_nan=False)) > 4096:
            raise ValueError("Ownership generation is too large")
        return self

    def expired(self, now):
        self.validate(now)
        return now >= self.ends_at

    @classmethod
    def from_dict(cls, data):
        """Decode storage without repairing malformed or future-schema data."""
        if type(data) is not dict:
            raise ValueError("Fan trial storage must contain an object")
        # All fields are required in persisted rows, including conservative
        # flags. Missing values are corruption, not permission to use defaults.
        if set(data) != {field.name for field in fields(cls)}:
            raise ValueError("Fan trial storage fields do not match the schema")
        trial = cls(**data)
        return trial.validate(trial.starts_at)

    def after_restart(self):
        """Never replay a reserved write or silently restore an old lease.

        Interrupted writes may have reached the equipment. Runtime ownership
        generations restart too, so equality with a persisted generation alone
        is not enough to prove continued ownership. Require explicit review.
        """
        self.validate(self.starts_at)
        if self.stage == "enrolled":
            return replace(self, stage="cancelled", reason="restart_before_dispatch")
        if self.stage in {"request_reserved", "active", "restore_reserved"}:
            return replace(self, stage="needs_attention", reason="restart_requires_restoration_review")
        return self
