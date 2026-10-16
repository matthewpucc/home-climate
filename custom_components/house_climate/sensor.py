"""Readable plans on the device page; planned actions never imply device feedback."""
from math import ceil

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SIGNAL_STATUS
from .controller.sensors import from_f


def short(text):
    """HA limits string states to 255 characters."""
    return text if len(text) <= 255 else text[:254] + "…"


def plan_warnings(status):
    return [*status.get("warnings", []),
            *(f"Circulation fan {entity}: waiting for off; retrying independently"
              for entity in status.get("pending_fans", []))]


def has_plan(status):
    return (status.get("event") in ("shadow", "live")
            and status.get("mode") in ("off", "heat", "cool")
            and "head_targets_f" in status)


def inactive_message(status):
    if status.get("event") in ("fault", "worker_failed"):
        return "Controller fault: " + (status.get("error") or "See Home Assistant logs")
    return {
        "waiting_for_home_assistant": "Waiting for Home Assistant",
        "started": "Starting; waiting for the first plan",
        "stopped": "Controller stopped",
        "stopping": "Stopping owned devices",
    }.get(status.get("event"), "Waiting for a plan")


def plan_prefix(status):
    return "Would " if status.get("event") == "shadow" else "Plan: "


def phase_message(status):
    """Describe the actual shared-head wait without guessing a timer."""
    phase = status.get("phase")
    if phase == "new":
        return "Confirming all heat-pump heads are off"
    if phase == "stopping":
        return "Waiting for all heat-pump heads off"
    if phase == "settling":
        remaining = status.get("settle_remaining_seconds")
        if remaining is None:
            return "Settling before heat-pump activation"
        seconds = max(0, ceil(remaining))
        if seconds == 0 and status.get("mode") == "off":
            return "Ready; no heat-pump demand"
        return f"Settling: {seconds}s remaining"
    return None


def climate_label(config, entity_id):
    """Use the configured room names, including every area sharing a head."""
    names = [room.name or key for key, room in config.zones.items()
             if config.heads[room.head] == entity_id]
    if names:
        return ", ".join(names)
    if entity_id == config.heater_entity and config.auxiliary_room:
        room = config.zones[config.auxiliary_room]
        return f"{room.name or config.auxiliary_room} auxiliary heater"
    return entity_id


def capability_message(detail):
    mode = detail.get("required_mode") or "Requested mode"
    modes = ", ".join(detail.get("available_modes") or []) or "not reported"
    return f"{mode} unavailable (HA modes: {modes})"


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [ControllerStatus(entry), Overview(entry), RecoveryStatus(entry),
                CurrentIssues(entry), LastFault(entry)]
    for key in entry.runtime_data.config.zones:
        entities.extend((RoomTemperature(entry, key), RoomPlan(entry, key)))
    for fan in entry.runtime_data.config.fan_controls.values():
        state = hass.states.get(fan.entity_id)
        label = state.name if state else fan.entity_id.split(".", 1)[1].replace("_", " ").title()
        entities.append(FanPlan(entry, fan, label))
    if entry.runtime_data.config.heater_entity:
        entities.append(AuxiliaryPlan(entry))

    # Preserve existing temperature IDs/history and remove obsolete plan entities
    # when rooms, fans, or the auxiliary heater are removed in Configure.
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefixes = tuple(f"{entry.entry_id}_{kind}" for kind in
                     ("room_", "plan_room_", "plan_fan_", "plan_auxiliary"))
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "sensor" and entity.platform == DOMAIN
                and entity.unique_id.startswith(prefixes) and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class ControllerEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key):
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
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


class ControllerStatus(ControllerEntity):
    """Keep the existing status entity compatible with automations."""
    _attr_name = "Controller status"
    _attr_icon = "mdi:home-thermometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry, "status")

    @property
    def native_value(self):
        return self.runtime.status["event"]

    @property
    def extra_state_attributes(self):
        # Exclude changing timestamps and raw probes to limit recorder churn.
        status = self.runtime.status
        return {key: status[key] for key in
                ("mode", "phase", "reasons", "warnings", "head_targets_f",
                 "heater_on", "auxiliary_fan_only", "fans", "room_targets", "error",
                 "last_error", "last_fault_at", "fault_count",
                 "control_enabled", "control_block_reason", "pending_fans",
                 "device_issues", "waiting_devices", "recovery_details",
                 "settle_remaining_seconds", "integration_recovery") if key in status}


class Overview(ControllerEntity):
    _attr_name = "Plan summary"
    _attr_icon = "mdi:clipboard-text-outline"

    def __init__(self, entry):
        super().__init__(entry, "overview")

    @property
    def native_value(self):
        status = self.runtime.status
        if not has_plan(status):
            return short(inactive_message(status))
        preview = status["event"] == "shadow"
        prefix = "Preview only · " if preview else "Live plan · "
        if preview and status.get("control_block_reason"):
            prefix += status["control_block_reason"] + " · "
        if not preview and (wait := phase_message(status)):
            prefix += wait + " · "
        warnings = plan_warnings(status)
        if warnings:
            prefix += f'{len(warnings)} warning(s) · '
        active = [room.name or key for key, room in self.runtime.config.zones.items()
                  if room.head in status["head_targets_f"]]
        if active:
            action = {"heat": "heat", "cool": "cool"}[status["mode"]]
            text = ("Would " if preview else "") + action + " " + ", ".join(active)
        else:
            text = "Heat pumps would stay off" if preview else "Heat pumps off requested"
        if status.get("heater_on"):
            text += "; auxiliary heat " + ("would be on" if preview else "requested")
        elif status.get("auxiliary_fan_only"):
            text += "; auxiliary fan-only " + ("would be on" if preview else "requested")
        return short(prefix + text)


class RecoveryStatus(ControllerEntity):
    """Put recovery progress on the device page without opening attributes."""
    _attr_name = "Recovery status"
    _attr_icon = "mdi:progress-clock"

    def __init__(self, entry):
        super().__init__(entry, "recovery")

    @property
    def native_value(self):
        status = self.runtime.status
        reload_status = status.get("integration_recovery", {}).get("reload_status")
        reload_message = reload_status if reload_status and reload_status != "Idle" else None
        if not has_plan(status):
            message = inactive_message(status)
            return short(message + (" · " + reload_message if reload_message else ""))
        if status["event"] == "shadow":
            reason = status.get("control_block_reason")
            messages = ["Preview only", reason, reload_message]
            return short(" · ".join(message for message in messages if message))
        details = status.get("recovery_details", {})
        issues = status.get("device_issues", {})
        waiting = dict.fromkeys([*details, *issues, *status.get("waiting_devices", [])])
        messages = [reload_message] if reload_message else []
        for entity_id in waiting:
            detail = details.get(entity_id, {})
            reason = (capability_message(detail) if detail.get("kind") == "capability"
                      else detail.get("reason") or issues.get(entity_id)
                      or "Waiting for off confirmation")
            messages.append(f"{climate_label(self.runtime.config, entity_id)}: {reason}")
        if wait := phase_message(status):
            messages.append(wait)
        return short("; ".join(messages) or "Ready")

    @property
    def extra_state_attributes(self):
        status = self.runtime.status
        return {key: status[key] for key in
                ("phase", "waiting_devices", "recovery_details", "settle_remaining_seconds",
                 "integration_recovery") if key in status}


class CurrentIssues(ControllerEntity):
    _attr_name = "Current issues"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, entry):
        super().__init__(entry, "issues")

    @property
    def native_value(self):
        status = self.runtime.status
        if not has_plan(status):
            return short(inactive_message(status))
        issues = plan_warnings(status)
        if status.get("control_block_reason"):
            issues.insert(0, status["control_block_reason"])
        return short("; ".join(issues) or "None")

    @property
    def extra_state_attributes(self):
        return {"warnings": plan_warnings(self.runtime.status)}


class LastFault(ControllerEntity):
    _attr_name = "Last controller fault"
    _attr_icon = "mdi:alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry, "last_fault")

    @property
    def native_value(self):
        return short(self.runtime.status.get("last_error") or "None since reload")

    @property
    def extra_state_attributes(self):
        status = self.runtime.status
        return {"occurred_at": status.get("last_fault_at"), "count": status.get("fault_count", 0)}


class RoomEntity(ControllerEntity):
    def __init__(self, entry, room_id, key):
        super().__init__(entry, key)
        self.room_id = room_id

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        area = self.runtime.config.zones[self.room_id].area_id
        if area:
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=area)


class RoomTemperature(RoomEntity):
    """Fused room temperature; retain pre-0.4.3 entity IDs."""
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_suggested_display_precision = 1

    def __init__(self, entry, room_id):
        super().__init__(entry, room_id, f"room_{room_id}")
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} temperature"

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


class RoomPlan(RoomEntity):
    _attr_icon = "mdi:thermostat"

    def __init__(self, entry, room_id):
        super().__init__(entry, room_id, f"plan_room_{room_id}")
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} plan"

    @property
    def available(self):
        return has_plan(self.runtime.status) and self.room_id in self.runtime.status.get("room_targets", {})

    def temperature(self, value):
        unit = self.hass.config.units.temperature_unit
        return f"{from_f(value, unit):.1f}".rstrip("0").rstrip(".") + f" {unit}"

    @property
    def native_value(self):
        if not self.available:
            return None
        status, config = self.runtime.status, self.runtime.config
        room = config.zones[self.room_id]
        target = status["head_targets_f"].get(room.head)
        shared = sum(zone.head == room.head for zone in config.zones.values()) > 1
        head = "shared head" if shared else "head"
        limits = status["room_targets"][self.room_id]
        waiting = (status.get("event") == "live"
                   and config.heads[room.head] in status.get("device_issues", {}))
        if waiting:
            detail = status.get("recovery_details", {}).get(config.heads[room.head], {})
            action = (f"keep {head} off; {capability_message(detail)}"
                      if detail.get("kind") == "capability"
                      else f"wait for {head}; off requested")
        elif target is not None:
            action = f'{status["mode"]} {head} to {self.temperature(target)}'
        else:
            action = f"keep {head} off"
            waiting = [mode for mode in ("heat", "cool") if limits[f"{mode}_requested"]]
            if waiting:
                action += " (" + "/".join(waiting) + " requested)"
        text = plan_prefix(status) + action
        if self.room_id not in status.get("rooms", {}):
            text = "No usable temperature · " + text
        band = "comfort" if limits["comfort_enabled"] else "limits only"
        text += f' · {band} {self.temperature(limits["low_f"])}–{self.temperature(limits["high_f"])}'
        return short(text)

    @property
    def extra_state_attributes(self):
        status, config = self.runtime.status, self.runtime.config
        head = config.zones[self.room_id].head
        return {"room_id": self.room_id, "head_entity_id": config.heads[head],
                "rooms_sharing_head": [room.name or key for key, room in config.zones.items()
                                      if room.head == head],
                "head_target_f": status.get("head_targets_f", {}).get(head),
                **status.get("room_targets", {}).get(self.room_id, {})}


class FanPlan(RoomEntity):
    _attr_icon = "mdi:fan"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(self, entry, fan, label):
        super().__init__(entry, fan.room, f"plan_fan_{fan.entity_id}")
        self.fan = fan
        self._attr_name = f"{label} planned speed"

    @property
    def available(self):
        return has_plan(self.runtime.status) and self.fan.entity_id in self.runtime.status.get("fans", {})

    @property
    def native_value(self):
        if not self.available:
            return None
        return self.runtime.status["fans"][self.fan.entity_id]

    @property
    def extra_state_attributes(self):
        return {"fan_entity_id": self.fan.entity_id, "room_id": self.room_id,
                "strategy": self.fan.strategy, "source_room": self.fan.source_room}


class AuxiliaryPlan(ControllerEntity):
    _attr_icon = "mdi:radiator"

    def __init__(self, entry):
        super().__init__(entry, "plan_auxiliary")
        room_id = self.runtime.config.auxiliary_room
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} auxiliary heater plan"

    @property
    def available(self):
        return has_plan(self.runtime.status) and "heater_on" in self.runtime.status

    @property
    def native_value(self):
        if not self.available:
            return None
        status = self.runtime.status
        if (status.get("event") == "live"
                and self.runtime.config.heater_entity in status.get("device_issues", {})):
            detail = status.get("recovery_details", {}).get(self.runtime.config.heater_entity, {})
            if detail.get("kind") == "capability":
                return short("Heater kept off · " + capability_message(detail))
            return "Waiting for heater · off requested"
        action = ("heat using room probes" if status["heater_on"] else
                  "circulate air without heat" if status.get("auxiliary_fan_only") else "stay off")
        return plan_prefix(status) + action
