"""One atomic form for live permission and independent protection settings."""
from __future__ import annotations

import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_THERMAL_MCP, DOMAIN, NAME
from .house_policy import HOUSE_POLICY
from .configuration import entry_configuration, entry_policy_settings
from .controller.protection import PROTECTION_RANGES, ProtectionTemperatures


class _ProtectionNumber(selector.NumberSelector):
    """Keep booleans and numeric strings from becoming safety temperatures."""

    def __call__(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise vol.Invalid("Enter a numeric protection setting")
        return super().__call__(value)


def _schema(live=False, protection=None, policy_settings=None, *, thermal_mcp=False):
    protection = protection or ProtectionTemperatures()
    data = {vol.Required("live", default=live): bool,
            vol.Optional(CONF_THERMAL_MCP, default=thermal_mcp): bool}
    for name, (minimum, maximum) in PROTECTION_RANGES.items():
        seconds = name == "qualification_seconds"
        step = 1 if seconds else .25 if name in ("release_hysteresis_f", "transition_deadband_f") else .5
        data[vol.Required(name, default=getattr(protection, name))] = _ProtectionNumber(
            selector.NumberSelectorConfig(min=minimum, max=maximum, step=step,
                mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s" if seconds else "°F"))
    for name in ("boundary_tie_mode", "unknown_outdoor_tie_mode"):
        data[vol.Required(name, default=getattr(protection, name))] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=[{"label": "Heating", "value": "heat"},
                                                   {"label": "Cooling", "value": "cool"}],
                                          mode=selector.SelectSelectorMode.DROPDOWN))
    policy_settings = policy_settings or {}
    for name in ("rooms", "auxiliary_policy", "seasonal_policy", "purifiers", "fallback"):
        data[vol.Optional(name, default=policy_settings.get(name, {}))] = selector.ObjectSelector()
    for name, default, minimum in (("heartbeat_timeout_seconds", 300, 60),
                                    ("auxiliary_heartbeat_timeout_seconds", 120, 30)):
        data[vol.Optional(name, default=policy_settings.get(name, default))] = _ProtectionNumber(
            selector.NumberSelectorConfig(min=minimum, max=900, step=1,
                mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s"))
    return vol.Schema(data)


def _live(values):
    live = values["live"]
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    return live


def _values(user_input, current, policy_settings=None, *, thermal_mcp=False, keep_thermal_mcp=False):
    protection = ProtectionTemperatures.from_dict({
        **current.to_dict(), **{key: value for key, value in user_input.items() if key in current.to_dict()}})
    policy = dict(policy_settings or {})
    for key in ("rooms", "auxiliary_policy", "seasonal_policy", "purifiers", "fallback",
                "heartbeat_timeout_seconds", "auxiliary_heartbeat_timeout_seconds"):
        if key in user_input:
            selected = user_input[key]
            if key == "rooms" and isinstance(selected, dict):
                rooms = dict(policy.get("rooms", {}))
                for room, edits in selected.items():
                    if not isinstance(edits, dict):
                        raise ValueError("Room policy must be an object")
                    rooms[room] = {**rooms.get(room, {}), **edits}
                    if isinstance(edits.get("profile"), dict):
                        rooms[room]["profile"] = {**policy.get("rooms", {}).get(room, {}).get("profile", {}),
                                                  **edits["profile"]}
                policy[key] = rooms
            elif isinstance(selected, dict) and isinstance(policy.get(key), dict):
                policy[key] = {**policy[key], **selected}
            else:
                policy[key] = selected
    thermal_mcp = user_input.get(CONF_THERMAL_MCP, thermal_mcp)
    if type(thermal_mcp) is not bool:
        raise ValueError("Thermal history MCP permission must be a boolean")
    result = {"policy": HOUSE_POLICY, "live": _live(user_input),
              "protection": protection.to_dict(), "policy_settings": policy}
    # An absent setting is safely off; retain an explicit false when it must
    # override a true value already stored in the entry's original data.
    if thermal_mcp or keep_thermal_mcp:
        result[CONF_THERMAL_MCP] = thermal_mcp
    return result


class HouseClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors, detail = {}, ""
        protection = ProtectionTemperatures()
        policy = {}
        try:
            policy = await self.hass.async_add_executor_job(
                entry_policy_settings, self.hass.config.config_dir,
                {"policy": HOUSE_POLICY, "live": False})
        except (OSError, KeyError, TypeError, ValueError) as exc:
            errors, detail = {"base": "invalid_config"}, str(exc)
        if user_input is not None:
            try:
                values = _values(user_input, protection, policy)
                await self.hass.async_add_executor_job(
                    entry_configuration, self.hass.config.config_dir, values)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors, detail = {"base": "invalid_config"}, str(exc)
            else:
                return self.async_create_entry(
                    title=NAME, data=values)
        return self.async_show_form(
            step_id="user", data_schema=_schema(protection=protection, policy_settings=policy), errors=errors,
            description_placeholders={"detail": detail}, last_step=True)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HouseClimateOptionsFlow()


class HouseClimateOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        values = {**self.config_entry.data, **self.config_entry.options}
        errors, detail = {}, ""
        protection = ProtectionTemperatures()
        policy = values.get("policy_settings", {})
        try:
            protection = ProtectionTemperatures.from_dict(values.get("protection", {}))
            policy = await self.hass.async_add_executor_job(
                entry_policy_settings, self.hass.config.config_dir, values)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            errors, detail = {"base": "invalid_config"}, str(exc)
        if user_input is not None:
            try:
                selected = _values(user_input, protection, policy,
                    thermal_mcp=values.get(CONF_THERMAL_MCP, False),
                    keep_thermal_mcp=self.config_entry.data.get(CONF_THERMAL_MCP) is True)
                await self.hass.async_add_executor_job(
                    entry_configuration, self.hass.config.config_dir,
                    {**values, **selected})
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors, detail = {"base": "invalid_config"}, str(exc)
            else:
                return self.async_create_entry(
                    title="", data=selected)
        return self.async_show_form(
            step_id="init", data_schema=_schema(values.get("live", False), protection, policy,
                thermal_mcp=values.get(CONF_THERMAL_MCP, False)), errors=errors,
            description_placeholders={"detail": detail}, last_step=True)
