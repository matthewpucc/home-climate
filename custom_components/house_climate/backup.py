"""Refresh an immutable history snapshot without freezing the live writer."""
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


async def async_pre_backup(hass):
    history = getattr(hass.data.get(DOMAIN), "thermal_storage", None)
    if history is not None:
        try:
            await history.async_pre_backup()
        except Exception as exc:
            raise HomeAssistantError("Could not refresh thermal history snapshot") from exc


async def async_post_backup(hass):
    """There is no writer lock/fence for a missing or delayed post to release."""
