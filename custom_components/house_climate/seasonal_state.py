"""Persist the selected seasonal comfort profile, never cached measurements."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)


class SeasonalState:
    def __init__(self, hass, entry_id):
        self.hass = hass
        self.store = Store(hass, 1, f"house_climate.seasonal.{entry_id}")
        self.profile = None
        self.error = None
        self._persisted = None
        self._task = None

    async def async_load(self):
        try:
            data = await self.store.async_load()
        except Exception:
            self.error = "Could not load the saved seasonal profile; using the configured initial profile"
            _LOGGER.warning(self.error, exc_info=True)
            return
        if data is None:
            return
        if not isinstance(data, dict) or data.get("profile") not in ("cool", "warm"):
            self.error = "Saved seasonal profile is invalid; using the configured initial profile"
            return
        self.profile = self._persisted = data["profile"]

    def observe(self, status):
        if not isinstance(status, dict) or status.get("profile") not in ("cool", "warm"):
            return
        self.profile = status["profile"]
        if self.profile != self._persisted and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(self._async_save())

    async def _async_save(self):
        while self.profile != self._persisted:
            profile = self.profile
            try:
                await self.store.async_save({"profile": profile})
            except Exception:
                self.error = "Could not persist the seasonal profile; a restart may use the previous profile"
                _LOGGER.warning(self.error, exc_info=True)
                return
            self._persisted = profile
            self.error = None

    async def async_close(self):
        if self._task is not None and not self._task.done():
            await asyncio.shield(self._task)
