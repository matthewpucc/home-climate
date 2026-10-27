"""One immutable decision snapshot, shared by planners and fresh head guards."""
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field


def value(context, key, default=None):
    if isinstance(context, Mapping):
        return context.get(key, default)
    return getattr(context, key, default)


def limits(config, context=None):
    settings = getattr(config, "protection", None)
    low = settings.freezing_heating_floor_f if settings is not None else config.low_f
    high = settings.emergency_cooling_at_f if settings is not None else config.high_f
    return float(value(context, "low_f", low)), float(value(context, "high_f", high))


def emergency(context, room, mode):
    return value(context, "emergencies", {}).get(room, {}).get(mode, {})


def window_affected(context, room, mode):
    return bool(value(context, "window_effects", {}).get(room, {}).get(mode, {}).get("active"))


def soft_yield(context, room, mode):
    opposing = "heat" if mode == "cool" else "cool"
    return window_affected(context, room, opposing)


def emergency_magnitudes(context):
    result = {}
    for directions in value(context, "emergencies", {}).values():
        for mode, detail in directions.items():
            if detail.get("active"):
                result[mode] = max(result.get(mode, 0), float(detail.get("violation_f", 0)))
    return result


def preferred_emergency(context):
    magnitudes = emergency_magnitudes(context)
    if not magnitudes:
        return None
    # No near-tie band: the larger measured degree violation wins. Weather
    # decides only an exact equal-magnitude tie, never ordinary room priority.
    return max(magnitudes, key=lambda mode: (magnitudes[mode], mode == value(context, "tie_mode", "heat")))


@dataclass(frozen=True)
class ProtectionContext:
    low_f: float
    high_f: float
    enabled: bool = False
    release_hysteresis_f: float = 1.5
    outdoor_status: str = "not_used"
    outdoor_temperature_f: float | None = None
    outdoor_sources: int = 0
    outdoor_regime: str = "legacy"
    outdoor_candidate: str | None = None
    tie_mode: str = "heat"
    emergencies: dict = field(default_factory=dict)
    window_effects: dict = field(default_factory=dict)
    window_states: dict = field(default_factory=dict)
    preferred_emergency_mode: str | None = None
    warnings: tuple = ()
    valid_until_monotonic: float | None = None

    def as_dict(self):
        data = asdict(self)
        # The native repair gate consumes the separate deadline. It must not
        # create recorder-facing changing timer attributes.
        data.pop("valid_until_monotonic")
        data.pop("window_states")
        return data


def initial_context(config):
    settings = getattr(config, "protection", None)
    low, high = limits(config)
    if settings is None:
        return ProtectionContext(low, high)
    return ProtectionContext(low, high, enabled=True,
                             release_hysteresis_f=settings.release_hysteresis_f,
                             outdoor_status="unknown", outdoor_regime="unknown",
                             tie_mode=settings.unknown_outdoor_tie_mode)
