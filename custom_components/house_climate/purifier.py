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
        self._snapshot = MappingProxyType({})
        self._lock = asyncio.Lock()
        self._closed = False

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

    def fan_controls(self):
        snapshot = self._snapshot
        return {key: replace(fan, speed_override=snapshot[fan.room])
                if fan.room in snapshot else fan
                for key, fan in self.config.fan_controls.items()}

    def settings(self, settings):
        return replace(settings, room_fan_overrides=dict(self._snapshot))

    def _data(self, values):
        return {"rooms": {room: {"percentage": value, "fingerprint": self._fingerprint(room)}
                          for room, value in values.items()}}

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
            normalized = self._data(values)
            if data is not None and data != normalized:
                await self.store.async_save(normalized)
            self._snapshot = MappingProxyType(values)

    async def async_set(self, room, value):
        """None explicitly resumes automatic policy, including configured floors."""
        async with self._lock:
            if self._closed:
                raise ValueError("Purifier controls are stopping")
            self._validate(room, value)
            values = {**self._snapshot, room: value}
            await self.store.async_save(self._data(values))
            self._snapshot = MappingProxyType(values)
            self.wake_event.set()
            self.notify()

    async def async_close(self):
        async with self._lock:
            self._closed = True
