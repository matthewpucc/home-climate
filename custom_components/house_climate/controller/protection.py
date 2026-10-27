"""Protection configuration independent of editable room comfort bounds."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
import math

# Deliberate draft exposure limits. These constrain the settings form and parser,
# not the separate 66–75°F comfort-card editing range.
PROTECTION_RANGES = {
    "normal_heating_floor_f": (40, 70),
    "freezing_heating_floor_f": (40, 70),
    "freezing_outdoor_below_f": (-40, 60),
    "emergency_cooling_at_f": (75, 95),
    "release_hysteresis_f": (.25, 5),
    "transition_deadband_f": (0, 10),
    "qualification_seconds": (0, 3600),
}
TIE_MODES = frozenset(("heat", "cool"))

def _finite_temperature(value, label):
    valid_type = not isinstance(value, bool) and isinstance(value, (int, float))
    try:
        finite = valid_type and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{label} must be a finite numeric temperature in Fahrenheit")
    return float(value)


@dataclass(frozen=True)
class ProtectionTemperatures:
    """Approved draft defaults; no comfort target or room priority lives here."""

    normal_heating_floor_f: float = 60
    freezing_heating_floor_f: float = 65
    freezing_outdoor_below_f: float = 32
    emergency_cooling_at_f: float = 80
    release_hysteresis_f: float = 1
    transition_deadband_f: float = 2
    qualification_seconds: float = 300
    boundary_tie_mode: str = "heat"
    unknown_outdoor_tie_mode: str = "heat"

    def __post_init__(self):
        for name, (low, high) in PROTECTION_RANGES.items():
            value = _finite_temperature(getattr(self, name), name)
            if not low <= value <= high:
                raise ValueError(f"{name} must be from {low:g} to {high:g}")
            if name == "qualification_seconds" and not value.is_integer():
                raise ValueError("qualification_seconds must be a whole number of seconds")
            object.__setattr__(self, name, value)
        if not self.normal_heating_floor_f <= self.freezing_heating_floor_f < self.emergency_cooling_at_f:
            raise ValueError("Protection temperatures require normal heating floor <= freezing floor < cooling threshold")
        if self.freezing_heating_floor_f + self.release_hysteresis_f >= self.emergency_cooling_at_f - self.release_hysteresis_f:
            raise ValueError("Protection heating and cooling release bands must not overlap")
        for name in ("boundary_tie_mode", "unknown_outdoor_tie_mode"):
            if not isinstance(getattr(self, name), str) or getattr(self, name) not in TIE_MODES:
                raise ValueError(f"{name} must be heat or cool")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError("Protection temperatures must be a settings object")
        if set(value) - {item.name for item in fields(cls)}:
            raise ValueError("Unknown protection temperature setting")
        return cls(**dict(value))

    def to_dict(self):
        return asdict(self)

    def candidate_heating_floor(self, outdoor_f):
        """Calculate a candidate from a known reading, without policy guesses.

        Unknown/stale sources must be handled explicitly by a future caller.
        A hysteresis policy may retain the higher floor after a cold interval;
        this stateless calculation intentionally does not decide that policy.
        """
        outdoor_f = _finite_temperature(outdoor_f, "Known outdoor temperature")
        return (float(self.freezing_heating_floor_f) if outdoor_f < self.freezing_outdoor_below_f
                else float(self.normal_heating_floor_f))
