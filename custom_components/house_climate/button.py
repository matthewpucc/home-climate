"""Explicit recovery controls for the integrations that own heat-pump heads."""
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SIGNAL_STATUS


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [ReloadHeadIntegration(entry, key) for key in entry.runtime_data.config.heads]
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefix = f"{entry.entry_id}_reload_"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "button" and entity.platform == DOMAIN
                and entity.unique_id.startswith(prefix) and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class ReloadHeadIntegration(ButtonEntity):
    """Reload a head's owning config entry, which can include other devices."""
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:reload"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, head_key):
        self.runtime = entry.runtime_data
        self.head_entity_id = self.runtime.config.heads[head_key]
        names = [room.name or key for key, room in self.runtime.config.zones.items()
                 if room.head == head_key]
        label = ", ".join(names) or head_key.replace("_", " ").title()
        self._attr_name = f"{label} reload integration"
        self._attr_unique_id = f"{entry.entry_id}_reload_{head_key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "House climate controller",
            "manufacturer": "House climate",
            "model": "Shared heat-pump controller",
        }

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._updated))

    @callback
    def _updated(self):
        self.async_write_ha_state()

    @property
    def available(self):
        return not self.runtime.stop_event.is_set()

    @property
    def extra_state_attributes(self):
        return {"climate_entity_id": self.head_entity_id,
                "reload_scope": "All devices in the owning integration entry"}

    async def async_press(self):
        if self.runtime.stop_event.is_set():
            raise HomeAssistantError("Controller is stopping")
        try:
            await self.runtime.recovery.async_reload_entity(self.head_entity_id)
        except (RuntimeError, ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
