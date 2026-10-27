"""Stop aggravating room temperatures before the house bounds are reached.

The fixed margin reserves room for probe cadence, command acknowledgement, and
residual heating/cooling after Off. A short forecast can only stop a call; it
does not request conditioning in the opposite direction.
"""
from __future__ import annotations

import math
from collections.abc import Mapping


STOP_MARGIN_F = 1.0
STOP_LOOKAHEAD_MINUTES = 2.0


def _value(reading, key: str, default=None):
    if isinstance(reading, Mapping):
        return reading.get(key, default)
    return getattr(reading, key, default)


def projected_extreme(reading, mode: str) -> float:
    """Project the raw valid extreme using only trend that worsens this call."""
    extreme = float(_value(reading, "high" if mode == "heat" else "low"))
    slope = float(_value(reading, "slope_f_per_minute", 0))
    if not math.isfinite(extreme) or not math.isfinite(slope):
        raise ValueError("Invalid room temperature/trend")
    worsening_slope = max(0.0, slope) if mode == "heat" else min(0.0, slope)
    return extreme + worsening_slope * STOP_LOOKAHEAD_MINUTES


def opposite_limit_reached(reading, mode: str, low_f: float, high_f: float) -> bool:
    """Raw extremes act immediately, including unconfirmed ambient gradients."""
    if mode == "heat":
        return projected_extreme(reading, mode) >= high_f - STOP_MARGIN_F
    if mode == "cool":
        return projected_extreme(reading, mode) <= low_f + STOP_MARGIN_F
    return False


def heating_cutoff_reached(reading, cutoff_f: float, high_f: float) -> bool:
    """Keep the auxiliary's comfort cutoff without consuming its whole band."""
    return projected_extreme(reading, "heat") >= min(cutoff_f, high_f - STOP_MARGIN_F)
