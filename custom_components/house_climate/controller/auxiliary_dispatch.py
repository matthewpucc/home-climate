"""Deterministic supplementary heat decisions from qualified room observations.

No I/O, inferred electrical supply, learned thermal model, or actuator timer
overrides live here. Report-based observations are not represented as physical
acquisition timestamps. Repeated planner reads never extend observation history.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time
import math
from zoneinfo import ZoneInfo


def manual_auxiliary(config, states):
    heater = states.get(config.heater_entity, {}) if config.heater_entity else {}
    return bool(heater.get("attributes", {}).get("home_climate_manual")
                or states.get(config.entity("auxiliary_manual"), {}).get("state") == "on")


@dataclass(frozen=True)
class AuxiliaryDecision:
    requested: bool
    reason: str
    peak: bool
    stop_target_f: float
    progress_known: bool = False
    progress_f: float | None = None
    outside_observed_seconds: float = 0
    wait_remaining_seconds: float | None = None

    def as_dict(self):
        return asdict(self)


class AuxiliaryDispatch:
    def __init__(self, config):
        self.config = config
        self.settings = getattr(config, "auxiliary_policy", None)
        self.active = False
        self.samples = deque()
        self.sources = None
        self.marker = None
        self.outside_since = None
        self.band = None

    def setting(self, name, default):
        return getattr(self.settings, name, default)

    def peak(self, now):
        local = now.astimezone(ZoneInfo(self.config.timezone)).time().replace(tzinfo=None)
        start = time.fromisoformat(self.setting("peak_start", "17:00"))
        end = time.fromisoformat(self.setting("peak_end", "21:00"))
        return start <= local < end if start < end else local >= start or local < end

    def reset_evidence(self):
        self.samples.clear()
        self.sources = self.marker = self.outside_since = None

    def observe(self, reading, now, low):
        stamp = getattr(reading, "observed_at", None)
        marker = getattr(reading, "observation_id", ())
        if (not marker or isinstance(stamp, bool) or not isinstance(stamp, (float, int))
                or not math.isfinite(stamp) or stamp > now.timestamp()
                or now.timestamp() - stamp > self.config.sensor_stale_seconds):
            self.reset_evidence()
            return
        sources = tuple(item[0] for item in marker)
        if self.sources != sources or (self.samples and stamp < self.samples[-1][0]):
            self.reset_evidence()
        self.sources = sources
        if marker == self.marker:
            return
        self.marker = marker
        if self.samples and stamp - self.samples[-1][0] > self.config.sensor_stale_seconds:
            self.samples.clear()
            self.outside_since = None
        # The oldest contributing source can remain unchanged while another
        # source reports. Do not manufacture elapsed evidence for the fusion.
        if self.samples and stamp <= self.samples[-1][0]:
            return
        self.samples.append((stamp, reading.temperature))
        cutoff = stamp - self.setting("progress_window_seconds", 600) - self.config.sensor_stale_seconds
        while len(self.samples) > 2 and self.samples[1][0] < cutoff:
            self.samples.popleft()
        if low - reading.temperature >= self.setting("peak_deficit_f", 1):
            if self.outside_since is None:
                self.outside_since = stamp
        else:
            self.outside_since = None

    def progress(self):
        if not self.samples:
            return None
        last_stamp, last_value = self.samples[-1]
        cutoff = last_stamp - self.setting("progress_window_seconds", 600)
        previous = next((point for point in reversed(self.samples) if point[0] <= cutoff), None)
        if previous is None:
            return None
        if previous[0] == cutoff:
            return last_value - previous[1]
        following = next((point for point in self.samples if point[0] > cutoff), None)
        if following is None or following[0] - previous[0] > min(
                self.config.sensor_stale_seconds, self.setting("progress_window_seconds", 600)):
            return None
        # Estimate the boundary between qualified observations; never call an
        # older, e.g. twenty-minute difference a ten-minute recovery amount.
        fraction = (cutoff - previous[0]) / (following[0] - previous[0])
        boundary_value = previous[1] + fraction * (following[1] - previous[1])
        return last_value - boundary_value

    def decide(self, reading, now, band, *, available=True, manual=False,
               heat_pump_selected=False, cooling_waiting=False, emergency=False,
               comfort_enabled=True, allow_start=True):
        low, high, target = band
        peak = self.peak(now)
        inside = min(self.setting("stop_inside_band_f", .5), max(0, (high - low) / 2))
        stop = low + inside if peak or not comfort_enabled else min(high, max(low + inside, target))
        if self.band != band:
            self.outside_since = None
            self.band = band
        if reading is not None:
            self.observe(reading, now, low)
        else:
            self.reset_evidence()
        progress = self.progress()
        elapsed = (max(0, self.samples[-1][0] - self.outside_since)
                   if self.samples and self.outside_since is not None else 0)
        wait = self.setting("peak_wait_seconds", 900)

        def result(requested, reason):
            if requested and not self.active and not allow_start:
                requested, reason = False, "Eligible supplementary heat awaits the next comfort decision"
            self.active = requested
            return AuxiliaryDecision(requested, reason, peak, stop,
                progress is not None, progress, elapsed,
                max(0, wait - elapsed) if peak and self.outside_since is not None else None)

        if manual:
            self.reset_evidence()
            return result(False, "Auxiliary belongs to explicit manual control")
        if not available or reading is None:
            return result(False, "Auxiliary awaits qualified room and device/supply evidence")
        if reading.temperature >= stop:
            return result(False, "Auxiliary stopping point reached")
        if self.active:
            return result(True, "Completing supplementary heat to its stopping point")
        if emergency:
            return result(True, "Auxiliary heat supports room protection")
        deficit = low - reading.temperature
        if deficit <= 0:
            return result(False, "Room is inside its scheduled band")
        if peak:
            if deficit >= self.setting("urgent_deficit_f", 2):
                return result(True, "Peak auxiliary: urgent measured comfort deficit")
            if deficit < self.setting("peak_deficit_f", 1):
                return result(False, "Peak efficiency: comfort deficit below the assistance threshold")
            if progress is None:
                return result(False, "Peak efficiency: recovery trend is unknown")
            if progress >= self.setting("progress_f", .5):
                return result(False, "Peak efficiency: room is making meaningful recovery")
            if elapsed < wait:
                return result(False, "Peak efficiency: observing the sustained comfort deficit")
            return result(True, "Peak auxiliary: sustained deficit without meaningful recovery")
        # Off-peak permits any measured out-of-band deficit. Prefer the heat
        # pump when it can serve this room; assistance is useful while cooling
        # must serve another room, or when measured recovery is inadequate.
        if not heat_pump_selected:
            return result(True, "Auxiliary supplies local heat while the shared system cannot serve it")
        if cooling_waiting:
            return result(True, "Auxiliary assists completion while other rooms await cooling")
        if progress is not None and progress < self.setting("progress_f", .5):
            return result(True, "Auxiliary supplements measured slow heat-pump recovery")
        return result(False, "Heat pump first; supplementary recovery evidence is not yet required or qualified")
