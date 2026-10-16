"""Desired room temperature alongside the thermostat card's comfort range."""
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .climate import RoomControl
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [RoomDesiredTemperature(entry, room_id)
                for room_id in entry.runtime_data.config.zones]
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefix = f"{entry.entry_id}_desired_"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "number" and entity.platform == DOMAIN
                and entity.unique_id.startswith(prefix) and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class RoomDesiredTemperature(RoomControl, NumberEntity):
    """Moving the desired point moves the comfort range with it."""
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_native_step = .5
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer"

    def __init__(self, entry, room_id):
        super().__init__(entry, room_id, f"desired_{room_id}")
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} desired temperature"

    @property
    def native_min_value(self):
        return self.runtime.comfort.bounds(self.room_id)[0]

    @property
    def native_max_value(self):
        return self.runtime.comfort.bounds(self.room_id)[1]

    @property
    def native_value(self):
        return self.comfort["target_f"]

    async def async_set_native_value(self, value):
        try:
            await self.runtime.comfort.async_set_target(self.room_id, value)
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
