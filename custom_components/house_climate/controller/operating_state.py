"""Qualified outdoor protection and measured open-window comfort effects."""
from collections.abc import Mapping
from dataclasses import asdict, replace
import math
import time
from zoneinfo import ZoneInfo

from .outdoor import read_outdoor
from .protection_context import initial_context, preferred_emergency
from .windows import CLOSE_DEBOUNCE_SECONDS, OPEN_DEBOUNCE_SECONDS, WindowContacts

WINDOW_EFFECT_SECONDS = 300
WINDOW_DEFICIT_F = .5
WINDOW_COMFORT_FACTOR = .25


def _number(reading, key):
    raw = reading.get(key) if isinstance(reading, Mapping) else getattr(reading, key)
    result = float(raw)
    if isinstance(raw, bool) or not math.isfinite(result):
        raise ValueError("Invalid temperature")
    return result


class OperatingState:
    """No I/O, command changes, or assumptions about compressor operation."""

    def __init__(self, config, clock=None):
        self.config = config
        self.clock = clock or time.monotonic
        self.contacts = WindowContacts(config, clock=self.clock)
        self.regime = None
        self.candidate = None
        self.candidate_since = None
        self.last_tick = None
        self._last_outdoor_wall = None
        self._outdoor_coverage_until = None
        self.room_keys = {}
        self.effects = {}
        self.active_emergencies = set()
        self.previous_emergencies = {}

    def environment(self, states, now, *, monotonic_now=None):
        tick = self.clock() if monotonic_now is None else monotonic_now
        context = initial_context(self.config)
        settings = getattr(self.config, "protection", None)
        if settings is None:
            return context
        outside = read_outdoor(self.config, states, now)
        wall = now.timestamp() if now.tzinfo is not None else None
        backwards = (self.last_tick is not None and tick < self.last_tick
                     or self._last_outdoor_wall is not None and wall is not None
                     and wall < self._last_outdoor_wall)
        self.last_tick = tick
        self._last_outdoor_wall = wall
        # next_expiry fences the first possible policy change (for example,
        # losing a colder alias). Continuous evidence may remain valid longer
        # through another selected source, so retain the union's final horizon.
        # A fresh report after that horizon cannot erase a gap hidden between
        # polls. Intervals touching exactly at expiry have no uncovered instant.
        coverage_gap = (outside.status == "known" and self._outdoor_coverage_until is not None
                        and min(start for start, _end in outside.coverage_intervals)
                        > self._outdoor_coverage_until)
        if backwards or coverage_gap:
            self.regime = self.candidate = self.candidate_since = None
        warnings = list(outside.warnings)
        deadline = None
        if outside.status != "known":
            self.regime = self.candidate = self.candidate_since = None
            self._outdoor_coverage_until = None
            warnings.append("Outdoor temperature is untrusted; conservative heating floor is active")
            regime, tie = "unknown", settings.unknown_outdoor_tie_mode
        else:
            self._outdoor_coverage_until = max(end for _start, end in outside.coverage_intervals)
            temperature = outside.temperature_f
            reference, width = settings.freezing_outdoor_below_f, settings.transition_deadband_f
            desired = ("cold" if temperature < reference and temperature <= reference - width else
                       "warm" if temperature > reference and temperature >= reference + width else None)
            if desired is None or desired == self.regime:
                self.candidate = self.candidate_since = None
            else:
                if desired != self.candidate or backwards:
                    self.candidate, self.candidate_since = desired, tick
                due = self.candidate_since + settings.qualification_seconds
                if tick >= due:
                    self.regime, self.candidate, self.candidate_since = desired, None, None
                else:
                    deadline = due
            regime = self.regime or "unqualified"
            tie = ("cool" if regime == "warm" else "heat" if regime == "cold"
                   else settings.boundary_tie_mode)
            if regime == "unqualified":
                warnings.append("Outdoor regime is not qualified; conservative heating floor is active")
        if outside.next_expiry is not None:
            expiry = tick + max(0, outside.next_expiry - now.timestamp())
            deadline = expiry if deadline is None else min(deadline, expiry)
        floor = (settings.normal_heating_floor_f if regime == "warm"
                 else settings.freezing_heating_floor_f)
        return replace(context, low_f=floor, outdoor_status=outside.status,
                       outdoor_temperature_f=outside.temperature_f, outdoor_sources=outside.sources,
                       outdoor_selected_role=outside.selected_role,
                       outdoor_regime=regime, outdoor_candidate=self.candidate, tie_mode=tie,
                       warnings=tuple(warnings), valid_until_monotonic=deadline)

    def rooms(self, context, states, readings, bands, profiles, settings, now, *, monotonic_now=None):
        tick = self.clock() if monotonic_now is None else monotonic_now
        contacts = self.contacts.observe(states, now, monotonic_now=tick)
        deadline = context.valid_until_monotonic

        def consider(due):
            nonlocal deadline
            if due is not None and due > tick:
                deadline = due if deadline is None else min(deadline, due)

        for saved in self.contacts.rooms.values():
            if saved.candidate:
                duration = OPEN_DEBOUNCE_SECONDS if saved.candidate == "open" else CLOSE_DEBOUNCE_SECONDS
                consider(saved.since_tick + duration)
        effects, emergencies = {}, {}
        local = now.astimezone(ZoneInfo(self.config.timezone)).time()
        for name, zone in self.config.zones.items():
            profile = profiles[name]
            active_period = ("hold" if name in settings.room_profiles else
                             next((index for index, period in enumerate(zone.schedules)
                                   if settings.active(period, local)), "base"))
            key = (bands[name], active_period, repr(asdict(profile)))
            if self.room_keys.get(name) != key:
                self.effects[name] = {}
                self.room_keys[name] = key
            reading = readings.get(name)
            window = contacts.get(name)
            valid_window = bool(window and window["state"] == "open" and profile.comfort_enabled)
            try:
                temperature = _number(reading, "temperature")
                low, high, _target = bands[name]
                deficits = {"heat": low - temperature, "cool": temperature - high}
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                valid_window = False
                deficits = {}
            stored = self.effects.setdefault(name, {})
            room_effects = {}
            for mode in ("heat", "cool"):
                deficit = deficits.get(mode, 0)
                if not valid_window or deficit < WINDOW_DEFICIT_F:
                    stored.pop(mode, None)
                    continue
                since = stored.setdefault(mode, tick)
                if tick < since:
                    since = stored[mode] = tick
                due = since + WINDOW_EFFECT_SECONDS
                active = tick >= due
                if not active:
                    consider(due)
                room_effects[mode] = {
                    "active": active, "deficit_f": deficit,
                    "comfort_priority_factor": WINDOW_COMFORT_FACTOR if active else 1,
                    "reason": ("Sustained measured deficit with a confirmed open window"
                               if active else "Qualifying sustained measured window effect"),
                }
            if room_effects:
                effects[name] = room_effects
            if not context.enabled:
                continue
            for mode, field in (("heat", "confirmed_low"), ("cool", "confirmed_high")):
                pair = (name, mode)
                try:
                    current = _number(reading, field)
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    if pair in self.active_emergencies:
                        emergencies.setdefault(name, {})[mode] = {
                            **self.previous_emergencies[pair], "measured": False,
                            "reason": "Prior emergency unresolved; room temperature is unavailable"}
                    continue
                bound = context.low_f if mode == "heat" else context.high_f
                violation = max(0, bound - current if mode == "heat" else current - bound)
                measured = current < bound if mode == "heat" else current >= bound
                release = (bound + context.release_hysteresis_f if mode == "heat"
                           else bound - context.release_hysteresis_f)
                released = current >= release if mode == "heat" else current <= release
                active = measured or pair in self.active_emergencies and not released
                if active:
                    self.active_emergencies.add(pair)
                    detail = {"active": True, "measured": measured,
                              "recovering": not measured, "violation_f": violation,
                              "threshold_f": bound, "release_f": release,
                              "reason": "Measured emergency" if measured else "Emergency recovery hysteresis"}
                    emergencies.setdefault(name, {})[mode] = detail
                    self.previous_emergencies[pair] = dict(detail)
                else:
                    self.active_emergencies.discard(pair)
                    self.previous_emergencies.pop(pair, None)
        confirmed_open = tuple(sorted(name for name, contact in contacts.items()
                                      if contact["state"] == "open"))
        result = replace(context, emergencies=emergencies, window_effects=effects,
                         window_states=contacts, confirmed_open_rooms=confirmed_open,
                         valid_until_monotonic=deadline)
        return replace(result, preferred_emergency_mode=preferred_emergency(result))
