"""Restore version-1 HA helper preferences during migration."""
import math
from datetime import time
from .legacy import NUMBERS, TIMES, TOGGLES, helper

def read(cls, states, config, strict=False):
    numbers = {}
    for name, (default, low, high, _) in NUMBERS.items():
        default = config.mode_hold_seconds / 60 if name == "mode_hold_minutes" else default
        state = states.get(helper(name))
        if state is None and not strict:
            value = default
        else:
            value = float((state or {}).get("state", "nan"))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid helper {helper(name)}")
        numbers[name] = value
    times = {}
    for name, default in TIMES.items():
        value = states.get(helper(name, "input_datetime"), {}).get("state")
        if value is None and not strict:
            value = default
        times[name] = time.fromisoformat(value or "invalid")
    # Sleep crosses midnight; morning ends before the next night begins.
    if not times["night_end"] < times["morning_end"] < times["night_start"]:
        raise ValueError("Require night_end < morning_end < night_start")
    if numbers["living_target"] + numbers["living_half_band"] > config.heater_cutoff_f:
        raise ValueError("Living target/deadband exceeds independent heater cutoff")
    toggles = {}
    for name, default in TOGGLES.items():
        value = states.get(helper(name, "input_boolean"), {}).get("state")
        if value is None and not strict:
            toggles[name] = default
        elif value in ("on", "off"):
            toggles[name] = value == "on"
        else:
            raise ValueError(f"Invalid helper {helper(name, 'input_boolean')}")
    return cls(numbers, times, toggles)

