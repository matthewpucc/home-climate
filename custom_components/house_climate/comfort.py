"""Persist dashboard comfort holds independently of controller ownership.

HA's loop serializes edits and persistence. The worker only reads an immutable
snapshot; it never writes storage or schedules callbacks.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from types import MappingProxyType

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store

from .controller.comfort import next_schedule_change
from .controller.config import Settings

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Hold:
    low_f: float
    high_f: float
    target_f: float
    expires_at: datetime | None
    fingerprint: str


class RoomComfort:
    """Room targets held until the next local schedule boundary."""

    def __init__(self, hass, config, entry_id, notify, wake_event=None):
        self.hass, self.config, self.notify = hass, config, notify
        self.wake_event = wake_event
        self.store = Store(hass, 1, f"house_climate.{entry_id}.comfort")
        self._lock = asyncio.Lock()
        self._snapshot = MappingProxyType({})
        self._remove_expiry = None
        self._expiry_task = None
        self._closed = False

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @property
    def _timezone(self):
        return self.hass.config.time_zone

    def _room(self, room_id):
        try:
            return self.config.zones[room_id]
        except (KeyError, TypeError):
            raise ValueError("Unknown configured room") from None

    def bounds(self, room_id):
        """Editable Fahrenheit limits, including the auxiliary heater cutoff."""
        self._room(room_id)
        high = self.config.high_f
        if room_id == self.config.auxiliary_room:
            high = min(high, self.config.heater_cutoff_f)
        return self.config.low_f, high

    def _fingerprint(self, room_id, config=None):
        config = config or self.config
        room = config.zones[room_id]
        data = {
            "room": asdict(room), "head": config.heads[room.head],
            "timezone": self._timezone, "limits": [config.low_f, config.high_f],
            "auxiliary_cutoff": config.heater_cutoff_f if room_id == config.auxiliary_room else None,
        }
        # Preserve fingerprints stored before report-on-change probes existed.
        for probe in data["room"]["probes"]:
            if not probe.get("report_on_change", False):
                probe.pop("report_on_change", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _states(self):
        # Only legacy profile helpers affect comfort settings. StateMachine.get
        # is the same cached accessor used by the native controller bridge.
        if not self.config.legacy:
            return {}
        from .controller.legacy import NUMBERS, TIMES, TOGGLES, helper
        result = {}
        for domain, names in (("input_number", NUMBERS), ("input_datetime", TIMES),
                              ("input_boolean", TOGGLES)):
            for name in names:
                entity = helper(name, domain)
                state = self.hass.states.get(entity)
                if state is not None:
                    result[entity] = {"state": state.state, "attributes": dict(state.attributes)}
        return result

    def _base(self, room_id, now):
        room = self._room(room_id)
        settings = Settings.read(self._states(), self.config)
        return settings, settings.profile(room, now, self._timezone)

    def _active(self, room_id, now, config=None):
        hold = self._snapshot.get(room_id)
        config = config or self.config
        if (hold is None or room_id not in config.zones
                or hold.fingerprint != self._fingerprint(room_id, config)
                or (hold.expires_at is not None and now >= hold.expires_at)):
            return None
        return hold

    def effective(self, room_id, now=None):
        """Resolve the dashboard state without changing schedule or ownership."""
        now = now or self._now()
        settings, profile = self._base(room_id, now)
        hold = self._active(room_id, now)
        if hold:
            low, high, target = hold.low_f, hold.high_f, hold.target_f
        else:
            target = settings.value(profile.target_f) + profile.target_offset_f
            if profile.comfort_enabled:
                half = settings.value(profile.half_band_f)
                low = max(self.config.low_f, settings.value(profile.low_f)
                          if profile.low_f is not None else target - half)
                high = min(self.config.high_f, settings.value(profile.high_f)
                           if profile.high_f is not None else target + half)
            else:
                # The card presents the saved/proposed comfort request while
                # inactive. Policy still enforces its full system safety range.
                half = settings.value(profile.half_band_f)
                low = settings.value(profile.low_f) if profile.low_f is not None else target - half
                high = settings.value(profile.high_f) if profile.high_f is not None else target + half
                minimum, maximum = self.bounds(room_id)
                width = min(high - low, maximum - minimum)
                low = min(max(low, minimum), maximum - width)
                high = low + width
                target = min(high, max(low, target))
        return {
            "low_f": low, "high_f": high, "target_f": target,
            "comfort_enabled": bool(hold) or profile.comfort_enabled,
            "hold": hold is not None,
            "expires_at": hold.expires_at.isoformat() if hold and hold.expires_at else None,
        }

    def settings(self, states, config, now, strict=False):
        """Worker-side provider; expired holds are ignored even if HA is busy."""
        settings = Settings.read(states, config, strict=strict)
        profiles = {}
        for room_id, room in config.zones.items():
            hold = self._active(room_id, now, config)
            if hold is None:
                continue
            base = settings.profile(room, now, self._timezone)
            profiles[room_id] = replace(
                base, low_f=hold.low_f, high_f=hold.high_f, target_f=hold.target_f,
                target_offset_f=0, comfort_enabled=True,
                half_band_f=settings.value(base.half_band_f),
                fan_percentage=settings.value(base.fan_percentage),
            )
        return replace(settings, room_profiles=profiles)

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("Temperature must be a finite number")
        return float(value)

    def _validate(self, room_id, low, high, target):
        low, high, target = (self._number(value) for value in (low, high, target))
        minimum, maximum = self.bounds(room_id)
        if not minimum <= low < high <= maximum:
            raise ValueError(f"Comfort range must be within {minimum:g}–{maximum:g}°F, with low below high")
        if not low <= target <= high:
            raise ValueError("Desired temperature must be within the comfort range")
        return low, high, target

    def _new_hold(self, room_id, low, high, target, now):
        low, high, target = self._validate(room_id, low, high, target)
        settings, _ = self._base(room_id, now)
        return _Hold(low, high, target, next_schedule_change(
            self._room(room_id), settings, now, self._timezone), self._fingerprint(room_id))

    @staticmethod
    def _data(holds):
        return {"rooms": {
            room: {**asdict(hold), "expires_at": hold.expires_at.isoformat() if hold.expires_at else None}
            for room, hold in holds.items()
        }}

    def _notify(self):
        if self.wake_event is not None:
            self.wake_event.set()
        self.notify()

    async def _commit(self, holds):
        # A failed save must not publish an edit the next startup will forget.
        await self.store.async_save(self._data(holds))
        self._snapshot = MappingProxyType(dict(holds))
        self._schedule_expiry()
        self._notify()

    def _check_open(self):
        if self._closed:
            raise ValueError("Room comfort controls are stopping")

    async def async_load(self):
        async with self._lock:
            self._check_open()
            data = await self.store.async_load()
            now, holds = self._now(), {}
            rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
            if isinstance(rooms, dict):
                for room_id, value in rooms.items():
                    try:
                        if not isinstance(value, dict) or room_id not in self.config.zones:
                            continue
                        low, high, target = self._validate(
                            room_id, value["low_f"], value["high_f"], value["target_f"])
                        expiry = value["expires_at"]
                        if expiry is not None:
                            expiry = datetime.fromisoformat(expiry)
                            if expiry.tzinfo is None or expiry <= now:
                                continue
                            expiry = expiry.astimezone(timezone.utc)
                        settings, _ = self._base(room_id, now)
                        next_change = next_schedule_change(
                            self._room(room_id), settings, now, self._timezone)
                        # A restored hold must not survive a boundary introduced
                        # by changed legacy time helpers.
                        if (next_change is None) != (expiry is None):
                            continue
                        if next_change is not None and expiry > next_change:
                            continue
                        fingerprint = self._fingerprint(room_id)
                        if value["fingerprint"] != fingerprint:
                            continue
                        holds[room_id] = _Hold(low, high, target, expiry, fingerprint)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
            if data is not None and data != self._data(holds):
                await self.store.async_save(self._data(holds))
            self._snapshot = MappingProxyType(holds)
            self._schedule_expiry()
            self._notify()

    async def async_set_range(self, room_id, low_f, high_f):
        async with self._lock:
            self._check_open()
            now = self._now()
            low, high = self._number(low_f), self._number(high_f)
            current = self.effective(room_id, now)
            target = min(high, max(low, current["target_f"]))
            hold = self._new_hold(room_id, low, high, target, now)
            await self._commit({**self._snapshot, room_id: hold})

    def _shifted_band(self, room_id, target, now):
        current = self.effective(room_id, now)
        if current["comfort_enabled"]:
            delta = target - current["target_f"]
            low, high = current["low_f"] + delta, current["high_f"] + delta
        else:
            settings, profile = self._base(room_id, now)
            half = settings.value(profile.half_band_f)
            low, high = target - half, target + half
        minimum, maximum = self.bounds(room_id)
        width = min(high - low, maximum - minimum)
        low = min(max(low, minimum), maximum - width)
        return low, low + width

    async def async_set_target(self, room_id, target_f):
        async with self._lock:
            self._check_open()
            target, now = self._number(target_f), self._now()
            minimum, maximum = self.bounds(room_id)
            if not minimum <= target <= maximum:
                raise ValueError(f"Desired temperature must be within {minimum:g}–{maximum:g}°F")
            low, high = self._shifted_band(room_id, target, now)
            hold = self._new_hold(room_id, low, high, target, now)
            await self._commit({**self._snapshot, room_id: hold})

    async def async_hold(self, room_id):
        async with self._lock:
            self._check_open()
            now = self._now()
            target = self.effective(room_id, now)["target_f"]
            minimum, maximum = self.bounds(room_id)
            target = min(maximum, max(minimum, target))
            low, high = self._shifted_band(room_id, target, now)
            hold = self._new_hold(room_id, low, high, target, now)
            await self._commit({**self._snapshot, room_id: hold})

    async def async_resume(self, room_id):
        async with self._lock:
            self._check_open()
            self._room(room_id)
            await self._commit({key: value for key, value in self._snapshot.items() if key != room_id})

    def _schedule_expiry(self):
        if self._remove_expiry:
            self._remove_expiry()
            self._remove_expiry = None
        if self._closed:
            return
        now = self._now()
        times = [hold.expires_at for hold in self._snapshot.values() if hold.expires_at is not None]
        if times:
            # A previous persistence failure may leave an already-expired entry.
            when = min(times)
            if when <= now:
                when = now + timedelta(seconds=30)
            self._remove_expiry = async_track_point_in_utc_time(self.hass, self._expiry_due, when)

    @callback
    def _expiry_due(self, _now):
        self._remove_expiry = None
        if not self._closed:
            self._expiry_task = self.hass.async_create_task(self._async_expire(), "House Climate comfort expiry")

    async def _async_expire(self):
        async with self._lock:
            if self._closed:
                return
            now = self._now()
            holds = {room: hold for room, hold in self._snapshot.items()
                     if self._active(room, now) is not None}
            if len(holds) != len(self._snapshot):
                try:
                    await self._commit(holds)
                    return
                except Exception:
                    # Read paths already exclude expired intent. Retry durable
                    # cleanup without bringing expired controls back into effect.
                    _LOGGER.exception("Could not save expired room comfort holds")
                    self._notify()
            self._schedule_expiry()

    async def async_close(self):
        async with self._lock:
            self._closed = True
            if self._remove_expiry:
                self._remove_expiry()
                self._remove_expiry = None
        if self._expiry_task:
            await self._expiry_task
