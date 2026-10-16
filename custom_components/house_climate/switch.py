"""Device-page control switch; no separately installed helpers for native rooms."""
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, SIGNAL_STATUS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ControllerEnabled(entry)])


class ControllerEnabled(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Controller enabled"
    _attr_icon = "mdi:power"
    _attr_should_poll = False

    def __init__(self, entry):
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "House climate controller",
            "manufacturer": "House climate",
            "model": "Shared heat-pump controller",
        }

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._updated))
        if not self.runtime.controls:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self.runtime.config.entity("enabled")], self._updated))

    @callback
    def _updated(self, _event=None):
        self.async_write_ha_state()

    @property
    def available(self):
        return not self.runtime.stop_event.is_set()

    @property
    def is_on(self):
        if self.runtime.controls:
            return self.runtime.controls.enabled
        return self.hass.states.is_state(self.runtime.config.entity("enabled"), "on")

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        try:
            if self.runtime.stop_event.is_set():
                raise HomeAssistantError("Controller is stopping")
            if self.runtime.controls:
                await self.runtime.controls.async_set_enabled(enabled)
            else:
                if enabled and not self.runtime.live:
                    raise HomeAssistantError("Enable live commands in Configure → System settings first")
                entity = self.runtime.config.entity("enabled")
                if self.hass.states.get(entity) is None:
                    raise HomeAssistantError("Save the room configuration in Configure to use native controls")
                await self.hass.services.async_call(
                    "input_boolean", "turn_on" if enabled else "turn_off",
                    {"entity_id": entity}, blocking=True,
                )
            self.runtime.wake_event.set()
        except (RuntimeError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
