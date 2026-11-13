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
        self._state = (MappingProxyType({}), MappingProxyType({}))
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

    def fan_controls(self):
        snapshot, minimums = self._state
        return {key: replace(fan,
                             minimum_percentage=minimums.get(fan.room, fan.minimum_percentage),
                             speed_override=snapshot.get(fan.room, fan.speed_override))
                for key, fan in self.config.fan_controls.items()}

    def settings(self, settings):
        snapshot, minimums = self._state
        return replace(settings, room_fan_overrides=dict(snapshot),
                       room_fan_minimums=dict(minimums))

    def _data(self, values, minimums=None):
        result = {"rooms": {room: {"percentage": value, "fingerprint": self._fingerprint(room)}
                            for room, value in values.items()}}
        if minimums:
            result["minimums"] = {room: {"percentage": value, "fingerprint": self._fingerprint(room)}
                                  for room, value in minimums.items()}
        return result

    async def async_load(self):
        async with self._lock:
            data = await self.store.async_load()
            values = {}
            rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
            if isinstance(rooms, dict):
                for room, saved in rooms.items():
                    try:
                        self._validate(room, saved["percentage"])
                        if saved["fingerprint"] == self._fingerprint(room):
                            values[room] = saved["percentage"]
                    except (KeyError, TypeError, ValueError):
                        continue
            minimums = {}
            saved_minimums = data.get("minimums", {}) if isinstance(data, dict) else {}
            if isinstance(saved_minimums, dict):
                for room, saved in saved_minimums.items():
                    try:
                        value = saved["percentage"]
                        self._validate(room, value)
                        if value is not None and saved["fingerprint"] == self._fingerprint(room):
                            minimums[room] = value
                    except (KeyError, TypeError, ValueError):
                        continue
            normalized = self._data(values, minimums)
            if data is not None and data != normalized:
                await self.store.async_save(normalized)
            self._state = (MappingProxyType(values), MappingProxyType(minimums))

    async def async_set(self, room, value):
        """None explicitly resumes automatic policy, including configured floors."""
        async with self._lock:
            if self._closed:
                raise ValueError("Purifier controls are stopping")
            self._validate(room, value)
            values = {**self._snapshot, room: value}
            await self.store.async_save(self._data(values, self._minimums))
            self._state = (MappingProxyType(values), self._minimums)
            self.wake_event.set()
            self.notify()

    async def async_set_minimum(self, room, value):
        """Set a baseline without replacing automatic demand or an exact override."""
        async with self._lock:
            if self._closed:
                raise ValueError("Purifier controls are stopping")
            self._validate(room, value)
            if value is None:
                raise ValueError("Purifier minimum must be a percentage")
            minimums = {**self._minimums, room: value}
            await self.store.async_save(self._data(self._snapshot, minimums))
            self._state = (self._snapshot, MappingProxyType(minimums))
            self.wake_event.set()
            self.notify()

    async def async_close(self):
        async with self._lock:
            self._closed = True
