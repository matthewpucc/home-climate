"""Room comfort ranges for standard Home Assistant thermostat cards."""
from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_STATUS

PRESET_SCHEDULE = "Schedule"
PRESET_TEMPORARY = "Temporary"


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [RoomClimate(entry, room_id) for room_id in entry.runtime_data.config.zones]
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefix = f"{entry.entry_id}_climate_"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "climate" and entity.platform == DOMAIN
                and entity.unique_id.startswith(prefix) and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class RoomControl:
    """Shared identity and updates for dashboard comfort controls."""
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, room_id, key):
        self.runtime = entry.runtime_data
        self.room_id = room_id
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "House climate controller",
            "manufacturer": "House climate",
            "model": "Shared heat-pump controller",
        }

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._updated))
        area = self.runtime.config.zones[self.room_id].area_id
        if area and er.async_get(self.hass).async_get(self.entity_id):
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=area)

    @callback
    def _updated(self):
        self.async_write_ha_state()

    @property
    def available(self):
        # Editing a comfort request does not activate equipment. Keep the
        # controls usable in shadow mode and while probes/heads are offline.
        return not self.runtime.stop_event.is_set()

    @property
    def comfort(self):
        return self.runtime.comfort.effective(self.room_id)

    @property
    def extra_state_attributes(self):
        config, status = self.runtime.config, self.runtime.status
        room = config.zones[self.room_id]
        comfort = self.comfort
        return {
            "room_id": self.room_id,
            "comfort_enabled": comfort["comfort_enabled"],
            "override_expires_at": comfort["expires_at"],
            "control_enabled": status.get("control_enabled", status.get("event") == "live"),
            "shadow_mode": status.get("event") == "shadow" or not self.runtime.live,
            "head_entity_id": config.heads[room.head],
            "rooms_sharing_head": [zone.name or key for key, zone in config.zones.items()
                                   if zone.head == room.head],
        }


class RoomClimate(RoomControl, ClimateEntity):
    """A room's comfort request; shared equipment remains centrally managed."""
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_precision = .1
    # The standard thermostat card draws the range when only RANGE is
    # advertised. The desired point is a separate temperature number.
    _attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                                | ClimateEntityFeature.PRESET_MODE)
    _attr_hvac_modes = [HVACMode.HEAT_COOL]
    _attr_hvac_mode = HVACMode.HEAT_COOL
    _attr_target_temperature_step = .5
    _attr_preset_modes = [PRESET_SCHEDULE, PRESET_TEMPORARY]

    def __init__(self, entry, room_id):
        super().__init__(entry, room_id, f"climate_{room_id}")
        room = self.runtime.config.zones[room_id]
        self._attr_name = room.name or room_id

    @property
    def min_temp(self):
        return self.runtime.comfort.bounds(self.room_id)[0]

    @property
    def max_temp(self):
        return self.runtime.comfort.bounds(self.room_id)[1]

    @property
    def current_temperature(self):
        return self.runtime.status.get("rooms", {}).get(self.room_id, {}).get("temperature")

    @property
    def target_temperature_low(self):
        return self.comfort["low_f"]

    @property
    def target_temperature_high(self):
        return self.comfort["high_f"]

    @property
    def preset_mode(self):
        return PRESET_TEMPORARY if self.comfort["hold"] else PRESET_SCHEDULE

    @property
    def hvac_action(self):
        status, config = self.runtime.status, self.runtime.config
        if status.get("event") != "live" or status.get("control_enabled") is False:
            return HVACAction.OFF
        room = config.zones[self.room_id]
        entities = [config.heads[room.head]]
        if self.room_id == config.auxiliary_room and config.heater_entity:
            entities.append(config.heater_entity)
        actions = set()
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                continue
            action = state.attributes.get("hvac_action")
            if action in (HVACAction.HEATING, HVACAction.COOLING):
                actions.add(HVACAction(action))
            elif action == HVACAction.IDLE:
                actions.add(HVACAction.IDLE)
        active = actions - {HVACAction.IDLE}
        if len(active) == 1:
            return next(iter(active))
        if not active and HVACAction.IDLE in actions:
            return HVACAction.IDLE
        # A target or a mode is intent, not proof that equipment is running.
        return None

    @property
    def extra_state_attributes(self):
        return {**super().extra_state_attributes,
                "desired_temperature_f": self.comfort["target_f"]}

    async def async_set_temperature(self, **kwargs):
        if "temperature" in kwargs:
            raise ServiceValidationError("Use the room's desired temperature control for a single target")
        if kwargs.get("hvac_mode") not in (None, HVACMode.HEAT_COOL):
            raise ServiceValidationError("Room comfort controls use Heat/Cool; the controller chooses equipment mode")
        if not any(key in kwargs for key in ("target_temp_low", "target_temp_high")):
            raise ServiceValidationError("Specify a comfort temperature range")
        comfort = self.comfort
        try:
            await self.runtime.comfort.async_set_range(
                self.room_id, kwargs.get("target_temp_low", comfort["low_f"]),
                kwargs.get("target_temp_high", comfort["high_f"]))
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode == PRESET_SCHEDULE:
            await self.runtime.comfort.async_resume(self.room_id)
        elif preset_mode == PRESET_TEMPORARY:
            await self.runtime.comfort.async_hold(self.room_id)
        else:
            raise ServiceValidationError("Choose Schedule or Temporary")

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode != HVACMode.HEAT_COOL:
            raise ServiceValidationError("Room comfort controls use Heat/Cool; use Controller enabled to pause control")
