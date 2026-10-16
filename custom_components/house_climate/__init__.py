"""Native Home Assistant lifecycle for the house climate controller."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.start import async_at_started

from .configuration import generate_configuration, entry_configuration
from .const import CONF_LIVE, DOMAIN
from .runtime import ControllerRuntime

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON, Platform.CLIMATE, Platform.NUMBER]


async def async_setup_entry(hass, entry):
    if DOMAIN in hass.data:
        raise ConfigEntryError("Another house climate controller is already loaded")
    values = {**entry.data, **entry.options}
    live = values.get(CONF_LIVE, False)
    try:
        config = await hass.async_add_executor_job(
            entry_configuration, hass.config.config_dir, values
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigEntryError(f"Invalid controller configuration: {exc}") from exc
    if live:
        owned = hass.states.get(config.entity("heater_owned"))
        heartbeat = hass.states.get(config.entity("heartbeat", "input_datetime"))
        if owned and owned.state == "on":
            try:
                timestamp = float(heartbeat.attributes["timestamp"])
                recent = not math.isfinite(timestamp) or datetime.now(timezone.utc).timestamp() - timestamp < 120
            except (AttributeError, KeyError, TypeError, ValueError):
                recent = True
            if recent:
                raise ConfigEntryNotReady("An existing controller lease is active; stop the add-on or standalone controller first")
    runtime = ControllerRuntime(hass, config, live)
    await runtime.async_setup_controls(entry.entry_id)
    entry.runtime_data = runtime
    hass.data[DOMAIN] = runtime
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await runtime.async_stop()
        hass.data.pop(DOMAIN, None)
        raise
    async def generate_package(_call):
        await hass.async_add_executor_job(
            generate_configuration, runtime.config, hass.config.config_dir
        )

    hass.services.async_register(DOMAIN, "generate_package", generate_package)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, runtime.async_stop))
    entry.async_on_unload(async_at_started(hass, runtime.async_start))
    return True


async def async_unload_entry(hass, entry):
    runtime = entry.runtime_data
    if not await runtime.async_stop():
        return False
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.services.async_remove(DOMAIN, "generate_package")
        hass.data.pop(DOMAIN, None)
    return unloaded


async def async_update_options(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass, entry):
    if entry.version > 2:
        return False
    if entry.version == 1:
        # Keep the original file and live helpers authoritative until the user saves
        # the room editor. Merely upgrading must not reset an existing thermostat.
        hass.config_entries.async_update_entry(entry, version=2)
    return True
