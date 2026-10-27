"""One explicit live-command opt-in for the fixed house operating policy."""
from __future__ import annotations

import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, NAME
from .house_policy import HOUSE_POLICY
from .configuration import entry_configuration


def _schema(live=False):
    return vol.Schema({vol.Required("live", default=live): bool})


def _live(values):
    live = values["live"]
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    return live


class HouseClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors, detail = {}, ""
        if user_input is not None:
            try:
                live = _live(user_input)
                await self.hass.async_add_executor_job(
                    entry_configuration, self.hass.config.config_dir,
                    {"policy": HOUSE_POLICY, "live": live})
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors, detail = {"base": "invalid_config"}, str(exc)
            else:
                return self.async_create_entry(
                    title=NAME, data={"policy": HOUSE_POLICY, "live": live})
        return self.async_show_form(
            step_id="user", data_schema=_schema(), errors=errors,
            description_placeholders={"detail": detail}, last_step=True)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HouseClimateOptionsFlow()


class HouseClimateOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        values = {**self.config_entry.data, **self.config_entry.options}
        errors, detail = {}, ""
        if user_input is not None:
            try:
                live = _live(user_input)
                await self.hass.async_add_executor_job(
                    entry_configuration, self.hass.config.config_dir,
                    {**values, "policy": HOUSE_POLICY, "live": live})
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors, detail = {"base": "invalid_config"}, str(exc)
            else:
                return self.async_create_entry(
                    title="", data={"policy": HOUSE_POLICY, "live": live})
        return self.async_show_form(
            step_id="init", data_schema=_schema(values.get("live", False)), errors=errors,
            description_placeholders={"detail": detail}, last_step=True)
