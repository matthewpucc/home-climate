"""Native Home Assistant lifecycle for the home climate controller."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.start import async_at_started
import voluptuous as vol

from .configuration import generate_configuration, entry_configuration, validate_actuators
from .const import CONF_LIVE, DOMAIN, NAME
from .runtime import ControllerRuntime

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON, Platform.CLIMATE, Platform.NUMBER]


async def async_setup_entry(hass, entry):
    if DOMAIN in hass.data:
        raise ConfigEntryError("Another home climate controller is already loaded")
    values = {**entry.data, **entry.options}
    live = values.get(CONF_LIVE, False)
    bindings = None
    try:
        if values.get("policy") == "house":
            from .controller.bindings import load_bindings
            # Use one private wiring snapshot for both room control and the
            # electrical interlock; never mix two versions of the file.
            bindings = await hass.async_add_executor_job(load_bindings, hass.config.config_dir)
            config = await hass.async_add_executor_job(
                entry_configuration, hass.config.config_dir, values, bindings)
        else:
            config = await hass.async_add_executor_job(
                entry_configuration, hass.config.config_dir, values)
        validate_actuators(hass, config)
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
    # Refresh only the old default title; user-assigned titles remain theirs.
    if getattr(entry, "title", None) in {"House Climate", "House climate", "House climate controller"}:
        hass.config_entries.async_update_entry(entry, title=NAME)
    runtime = ControllerRuntime(
        hass, config, live, house_policy=values.get("policy") == "house",
        power_bindings=bindings.power if bindings is not None else None)
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

    async def commission_heater_relay(call):
        # A registered action is not permission to run during unload, Preview,
        # or against a replacement runtime. The guard checks physical evidence
        # and controller ownership again immediately before dispatch.
        if (hass.data.get(DOMAIN) is not runtime or runtime.stop_event.is_set()
                or runtime.power_guard is None):
            raise HomeAssistantError("Dedicated heater relay commissioning is unavailable")
        if not runtime.live:
            raise HomeAssistantError("Enable live equipment commands before commissioning the dedicated relay")
        try:
            await runtime.power_guard.async_commission_relay(confirm=call.data["confirm"])
        except (RuntimeError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc

    hass.services.async_register(DOMAIN, "generate_package", generate_package)
    hass.services.async_register(
        DOMAIN, "commission_heater_relay", commission_heater_relay,
        schema=vol.Schema({vol.Required("confirm"): vol.All(bool, vol.In((True,)))}),
    )
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, runtime.async_stop))
    entry.async_on_unload(async_at_started(hass, runtime.async_start))
    return True


async def async_unload_entry(hass, entry):
    runtime = entry.runtime_data
    if not await runtime.async_stop(wait_for_ack=True):
        return False
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.services.async_remove(DOMAIN, "generate_package")
        hass.services.async_remove(DOMAIN, "commission_heater_relay")
        hass.data.pop(DOMAIN, None)
    return unloaded


async def async_update_options(hass, entry):
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    runtime.async_schedule_options_reload(entry.entry_id)


async def async_migrate_entry(hass, entry):
    if entry.version > 3:
        return False
    if entry.version < 3:
        from .comfort import async_migrate_house_holds
        from .controller.bindings import load_bindings
        from .house_policy import house_configuration

        values = {**entry.data, **entry.options}
        previous = await hass.async_add_executor_job(
            entry_configuration, hass.config.config_dir, values)
        bindings = await hass.async_add_executor_job(load_bindings, hass.config.config_dir)
        fixed = house_configuration(previous.to_dict(), bindings=bindings)
        # Save migrated holds before replacing the policy/fingerprints. A failed
        # storage write leaves the old entry intact so migration can be retried.
        await async_migrate_house_holds(hass, entry.entry_id, previous, fixed)
        data, options = dict(entry.data), dict(entry.options)
        # Options can contain the newest room identities. Save the effective
        # snapshot before the minimal options form replaces those legacy options.
        data["configuration"] = previous.to_dict()
        data["policy"] = options["policy"] = "house"
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=3)
    return True
