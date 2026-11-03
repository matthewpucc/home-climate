"""Explicit recovery controls for the integrations that own heat-pump heads."""
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DEVICE_NAME, NAME, DOMAIN, SIGNAL_STATUS
from .climate import RoomControl


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [ReloadHeadIntegration(entry, key) for key in entry.runtime_data.config.heads]
    entities.extend(ResumeRoomSchedule(entry, room_id)
                    for room_id in entry.runtime_data.config.zones)
    if (getattr(entry.runtime_data, "controls", None) is not None
            and getattr(entry.runtime_data.config, "fallback", None) is not None):
        entities.append(RetryRecoverySettings(entry))
    guard = getattr(entry.runtime_data, "power_guard", None)
    if guard is not None and (getattr(guard.bindings, "emergency_ac_switch", None)
                              or getattr(guard.bindings, "heater_switch", None)
                              or guard.diagnostics.get("manual_recovery_required")):
        entities.append(AcknowledgeHeaterRecovery(entry))
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefix = f"{entry.entry_id}_reload_"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "button" and entity.platform == DOMAIN
                and (entity.unique_id.startswith((prefix, f"{entry.entry_id}_resume_schedule_"))
                     or entity.unique_id == f"{entry.entry_id}_heater_power_recovery")
                and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class ResumeRoomSchedule(RoomControl, ButtonEntity):
    """One-room release action supported by Assist/MCP's button press intent."""
    _attr_icon = "mdi:calendar-refresh"

    def __init__(self, entry, room_id):
        super().__init__(entry, room_id, f"resume_schedule_{room_id}")
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} resume schedule"

    async def async_press(self):
        if self.runtime.stop_event.is_set():
            raise HomeAssistantError("Room comfort controls are stopping")
        try:
            await self.runtime.comfort.async_resume(self.room_id)
        except (ValueError, OSError) as exc:
            raise HomeAssistantError(str(exc)) from exc


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
            "name": DEVICE_NAME,
            "manufacturer": NAME,
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


class AcknowledgeHeaterRecovery(ReloadHeadIntegration):
    """Acknowledge completed manual recovery without energizing any equipment."""
    _attr_icon = "mdi:shield-check"

    def __init__(self, entry):
        self.runtime = entry.runtime_data
        self._attr_name = "Acknowledge heater power recovery"
        self._attr_unique_id = f"{entry.entry_id}_heater_power_recovery"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)}, "name": DEVICE_NAME,
            "manufacturer": NAME, "model": "Shared heat-pump controller",
        }

    @property
    def available(self):
        # Manual acknowledgement cannot energize equipment. Keep it usable
        # during deferred unload so verified recovery can release ownership.
        guard = self.runtime.power_guard
        return guard is not None and not guard._closed

    @property
    def extra_state_attributes(self):
        return {"action": "Acknowledge verified manual recovery only; never turns power On"}

    async def async_press(self):
        try:
            await self.runtime.power_guard.async_acknowledge_emergency()
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc


class RetryRecoverySettings(ReloadHeadIntegration):
    """Explicit approval to retry a durable, incomplete terminal preset."""
    _attr_icon = "mdi:restore-alert"

    def __init__(self, entry):
        self.runtime = entry.runtime_data
        self._attr_name = "Retry recovery settings"
        self._attr_unique_id = f"{entry.entry_id}_retry_recovery_settings"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)},
            "name": DEVICE_NAME, "manufacturer": NAME, "model": "Shared heat-pump controller"}

    @property
    def available(self):
        return self.runtime.controls is not None and self.runtime.controls.handoff is not None

    @property
    def extra_state_attributes(self):
        return {"action": "Retry saved head/fan recovery settings after fresh head Off; auxiliary faults remain protected"}

    async def async_press(self):
        try:
            await self.runtime.controls.async_retry_handoff()
        except (RuntimeError, ValueError, OSError) as exc:
            raise HomeAssistantError(str(exc)) from exc
