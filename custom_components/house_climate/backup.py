"""Quiesce evidence writes, including replacement runtimes, during HA backups."""
import asyncio
import threading

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .thermal_storage import BACKUP_FENCE

_STATE = "house_climate_history_backup_state"


def _state(hass):
    return hass.data.setdefault(_STATE, {"lock": asyncio.Lock(), "writer": None})


async def async_pre_backup(hass):
    state = _state(hass)
    async with state["lock"]:
        if state["writer"] is not None:
            raise HomeAssistantError("Thermal history backup already active")
        fence = hass.data.setdefault(BACKUP_FENCE, threading.Event())
        fence.set()
        history = getattr(hass.data.get(DOMAIN), "thermal_storage", None)
        try:
            if history is not None:
                await history.async_pre_backup()
            state["writer"] = history or False
        except RuntimeError as exc:
            fence.clear()
            raise HomeAssistantError(str(exc)) from exc
        except BaseException:
            fence.clear()
            raise


async def async_post_backup(hass):
    state = _state(hass)
    async with state["lock"]:
        history = state["writer"]
        try:
            if history:
                await history.async_post_backup()
        finally:
            state["writer"] = None
            hass.data.setdefault(BACKUP_FENCE, threading.Event()).clear()
