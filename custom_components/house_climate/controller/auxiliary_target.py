"""Auxiliary setpoint contract, separate from HA's rounded display value."""
from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class AuxiliaryTarget:
    command: float
    reported: float
    native: float | None = None
    ceiling_f: float | None = None
    contract: tuple = ()
    observed_native: float | None = field(default=None, compare=False)

    def matches(self, observed):
        raw = observed.get("attributes", {}).get("temperature")
        try:
            if isinstance(raw, bool) or not math.isfinite(float(raw)):
                return False
            # Legacy adapters retain strict matching; native HA uses its exact
            # rendering and also checks the unrounded entity target snapshot.
            tolerance = 1e-6 if self.contract else .05
            if abs(float(raw) - self.reported) >= tolerance:
                return False
            return not self.contract or (self.observed_native is not None
                and math.isfinite(self.observed_native)
                and abs(self.observed_native - self.native) < 1e-6)
        except (TypeError, ValueError, OverflowError):
            return False


def trusted_auxiliary_state(observed):
    attrs = observed.get("attributes", {})
    return (observed.get("state") in ("off", "heat", "fan_only")
            and not attrs.get("restored") and not attrs.get("assumed_state")
            and attrs.get("hvac_action") != "cooling"
            and not (observed.get("state") != "heat" and attrs.get("hvac_action") == "heating"))
