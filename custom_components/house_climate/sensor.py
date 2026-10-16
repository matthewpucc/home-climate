"""Expose controller decisions in HA even while running in shadow mode."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SIGNAL_STATUS


async def async_setup_entry(hass, entry, async_add_entities):
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_room_"
    current = {f"{prefix}{key}" for key in entry.runtime_data.config.zones}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.startswith(prefix) and entity.unique_id not in current:
            registry.async_remove(entity.entity_id)
    async_add_entities([ControllerStatus(entry), *[RoomTemperature(entry, key) for key in entry.runtime_data.config.zones]])


class ControllerStatus(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Controller status"
    _attr_icon = "mdi:home-thermometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry):
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_status"
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
    def native_value(self):
        return self.runtime.status["event"]

    @property
    def extra_state_attributes(self):
        # Exclude changing timestamps and raw probes to limit recorder churn.
        status = self.runtime.status
        return {key: status[key] for key in
                ("mode", "phase", "reasons", "warnings", "head_targets_f",
                 "heater_on", "auxiliary_fan_only", "error",
                 "last_error", "last_fault_at", "fault_count") if key in status}


class RoomTemperature(ControllerStatus):
    """Fused room temperature; room IDs survive display-name changes."""
    _attr_entity_category = None
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_suggested_display_precision = 1

    def __init__(self, entry, room_id):
        super().__init__(entry)
        self.room_id = room_id
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} temperature"
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        area = self.runtime.config.zones[self.room_id].area_id
        if area:
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=area)

    @property
    def available(self):
        return self.room_id in self.runtime.status.get("rooms", {})

    @property
    def native_value(self):
        return self.runtime.status.get("rooms", {}).get(self.room_id, {}).get("temperature")

    @property
    def extra_state_attributes(self):
        reading = self.runtime.status.get("rooms", {}).get(self.room_id, {})
        return {"room_id": self.room_id, "independent_sources": reading.get("sources", 0)}
