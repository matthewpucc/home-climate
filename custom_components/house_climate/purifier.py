"""Durable dashboard fan speeds; the worker consumes one immutable snapshot."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import math
from types import MappingProxyType

from homeassistant.helpers.storage import Store


class RoomPurifierSpeeds:
    def __init__(self, hass, config, entry_id, notify, wake_event):
        self.config, self.notify, self.wake_event = config, notify, wake_event
        self.store = Store(hass, 1, f"house_climate.{entry_id}.purifier_speeds")
        self.rooms = tuple(sorted({fan.room for fan in config.fan_controls.values()}))
        # Exact overrides, floor overrides, and each control's last nonzero
        # manual selection are published together only after persistence.
        self._state = self._frozen_state({}, {}, {}, {})
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def _snapshot(self):
        return self._state[0]

    @property
    def _minimums(self):
        return self._state[1]

    def _fingerprint(self, room):
        # An intentional configuration/wiring change supersedes dashboard intent.
        fans = [asdict(fan) for _, fan in sorted(self.config.fan_controls.items()) if fan.room == room]
        return hashlib.sha256(json.dumps(fans, sort_keys=True).encode()).hexdigest()

    def _validate(self, room, value):
        if room not in self.rooms:
            raise ValueError("Room has no configured purifier")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("Purifier speed must be a finite percentage from 0 to 100")

    def effective(self, room):
        if room in self._snapshot:
            return self._snapshot[room]
        values = {fan.speed_override for fan in self.config.fan_controls.values() if fan.room == room}
        return values.pop() if len(values) == 1 else None

    def minimum(self, room):
        if room in self._minimums:
            return self._minimums[room]
        values = [fan.minimum_percentage for fan in self.config.fan_controls.values() if fan.room == room]
        return max(values, default=0)

    def _validate_control(self, room, kind, value=None):
        if kind not in ("minimum", "speed"):
            raise ValueError("Unknown room fan control")
        self._validate(room, value)

    def manual_value(self, room, kind):
        """Current percentage (including Off), or None when automatic."""
        self._validate_control(room, kind)
        return self._minimums.get(room) if kind == "minimum" else self.effective(room)

    def last_manual_value(self, room, kind):
        self._validate_control(room, kind)
        return self._state[3 if kind == "minimum" else 2].get(room)

    def fan_controls(self):
        snapshot, minimums, _, _ = self._state
        return {key: replace(fan,
                             minimum_percentage=minimums.get(fan.room, fan.minimum_percentage),
                             speed_override=snapshot.get(fan.room, fan.speed_override))
                for key, fan in self.config.fan_controls.items()}

    def settings(self, settings):
        snapshot, minimums, _, _ = self._state
        return replace(settings, room_fan_overrides=dict(snapshot),
                       room_fan_minimums=dict(minimums))

    @staticmethod
    def _frozen_state(*mappings):
        return tuple(MappingProxyType(mapping) for mapping in mappings)

    def _serialized(self, values):
        return {room: {"percentage": value, "fingerprint": self._fingerprint(room)}
                for room, value in values.items()}

    def _data(self, values, minimums, prior_speeds, prior_minimums):
        # Keep the previous on-disk keys, including explicit null for exact
        # Auto, so existing overrides migrate and older builds can read them.
        result = {"rooms": self._serialized(values)}
        for key, mapping in (("minimums", minimums), ("prior_speeds", prior_speeds),
                             ("prior_minimums", prior_minimums)):
            if mapping:
                result[key] = self._serialized(mapping)
        return result

    def _read_values(self, data, key, *, allow_none=False, nonzero=False):
        values = {}
        stored = data.get(key) if isinstance(data, dict) else None
        if isinstance(stored, dict):
            for room, saved in stored.items():
                try:
                    value = saved["percentage"]
                    self._validate(room, value)
                    if (saved["fingerprint"] == self._fingerprint(room)
                            and (allow_none or value is not None)
                            and (not nonzero or value > 0)):
                        values[room] = value
                except (KeyError, TypeError, ValueError):
                    continue
        return values

    async def async_load(self):
        async with self._lock:
            data = await self.store.async_load()
            state = (self._read_values(data, "rooms", allow_none=True),
                     self._read_values(data, "minimums"),
                     self._read_values(data, "prior_speeds", nonzero=True),
                     self._read_values(data, "prior_minimums", nonzero=True))
            normalized = self._data(*state)
            if data is not None and data != normalized:
                await self.store.async_save(normalized)
            self._state = self._frozen_state(*state)

    async def async_set(self, room, value):
        """None explicitly resumes automatic policy, including configured floors."""
        await self.async_set_control(room, "speed", value)

    async def async_set_minimum(self, room, value):
        """Set a baseline without replacing automatic demand or an exact override."""
        if value is None:
            raise ValueError("Purifier minimum must be a percentage")
        await self.async_set_control(room, "minimum", value)

    async def async_set_control(self, room, kind, value):
        """Set one override. None resumes policy; explicit zero is manual."""
        async with self._lock:
            await self._async_set_control_locked(room, kind, value)

    async def async_resume_control(self, room, kind):
        """Re-enter manual mode without forgetting a prior nonzero selection."""
        async with self._lock:
            self._validate_control(room, kind)
            value = self.manual_value(room, kind) or self.last_manual_value(room, kind)
            if value is None:
                value = max(1, self.minimum(room))
            await self._async_set_control_locked(room, kind, value)

    async def _async_set_control_locked(self, room, kind, value):
        if self._closed:
            raise ValueError("Purifier controls are stopping")
        self._validate_control(room, kind, value)
        index = 1 if kind == "minimum" else 0
        state = tuple(map(dict, self._state))
        current = self.manual_value(room, kind)
        if value:
            state[index + 2][room] = value
        elif current:
            state[index + 2][room] = current
        if index == 1 and value is None:
            state[index].pop(room, None)
        else:
            state[index][room] = value
        await self.store.async_save(self._data(*state))
        self._state = self._frozen_state(*state)
        self.wake_event.set()
        self.notify()

    async def async_close(self):
        async with self._lock:
            self._closed = True
