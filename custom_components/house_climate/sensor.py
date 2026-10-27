"""Readable plans on the device page; planned actions never imply device feedback."""
from math import ceil, isclose, isfinite

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DEVICE_NAME, NAME, DOMAIN, SIGNAL_STATUS
from .controller.sensors import from_f, to_f


def short(text):
    """HA limits string states to 255 characters."""
    return text if len(text) <= 255 else text[:254] + "…"


def auxiliary_heartbeat_issue(status):
    """Keep the shorter auxiliary lease separate from the shared-head lease."""
    heartbeat = status.get("heartbeat") or {}
    if (status.get("event") == "live" and status.get("control_enabled") is not False
            and heartbeat.get("auxiliary_blocked")):
        return heartbeat.get("auxiliary_reason") or "Auxiliary heating held off until fresh Off confirmation and heartbeat"
    return None


def plan_warnings(status):
    warnings = [*status.get("warnings", []),
                *(f"Circulation fan {entity}: waiting for off; retrying independently"
                  for entity in status.get("pending_fans", []))]
    if (issue := auxiliary_heartbeat_issue(status)) and issue not in warnings:
        warnings.insert(0, issue)
    guard = status.get("power_guard") or {}
    if guard.get("blocked") and guard.get("reason"):
        warnings.insert(0, "Living-room heater power guard: " + guard["reason"])
    return warnings


def heartbeat_issue(status):
    """A delayed native pulse blocks new activations, not the whole controller."""
    heartbeat = status.get("heartbeat") or {}
    if (status.get("event") == "live" and status.get("control_enabled") is not False
            and heartbeat.get("blocked")
            and heartbeat.get("state") != "stopped"):
        return heartbeat.get("reason") or "Waiting for Home Assistant heartbeat confirmation; new activations are deferred"
    return None


def heartbeat_attributes(status):
    """Keep changing heartbeat ages out of recorder-facing entity attributes."""
    heartbeat = status.get("heartbeat")
    if heartbeat is None:
        return {}
    result = {key: heartbeat[key] for key in
              ("state", "blocked", "reason", "auxiliary_blocked", "auxiliary_reason")
              if key in heartbeat}
    if not heartbeat.get("blocked") and heartbeat.get("state") in ("pending", "confirmed"):
        # The HA-loop confirmation publishes both stages on every ordinary pulse.
        # Keep the exact delivery phase in runtime diagnostics, not recorder churn.
        result["state"] = "ok"
    return {"heartbeat": result}


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


def guard_temperature(value, display_unit, digits=1):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        converted = from_f(value, display_unit)
        return f"{converted:.{digits}f}".rstrip("0").rstrip(".") + display_unit
    return None


def guard_display_unit(entity):
    # Attributes can be inspected before an entity has been attached to HA.
    return (entity.hass.config.units.temperature_unit if entity.hass is not None
            else UnitOfTemperature.FAHRENHEIT)


def guard_room_label(label):
    return label if len(label) <= 48 else label[:47] + "…"


def current_head_constraints(status, head=None, config=None):
    """Ignore old or opposite-mode constraints on a head that is being served."""
    if not has_plan(status):
        return []
    result = []
    recovering = {*status.get("device_issues", {}), *status.get("recovery_details", {}),
                  *status.get("waiting_devices", [])}
    for key, directions in status.get("head_constraints", {}).items():
        if head is not None and key != head:
            continue
        if key in status.get("head_targets_f", {}):
            continue
        if config is not None and config.heads.get(key) in recovering:
            continue
        for mode in dict.fromkeys((status.get("mode"), "cool", "heat")):
            detail = directions.get(mode)
            if mode in ("heat", "cool") and isinstance(detail, dict):
                if config is not None and not any(
                        room.head == key and status.get("room_targets", {}).get(name, {}).get(f"{mode}_requested")
                        for name, room in config.zones.items()):
                    continue
                result.append((key, mode, detail))
    return result


def head_constraint_message(config, status, head, mode, detail, display_unit):
    """Put the actual room and limit before warnings can truncate the state."""
    room_id = detail.get("room")
    room = config.zones.get(room_id)
    label = guard_room_label((room.name or room_id) if room else detail.get("room_name") or "Attached room")
    temperature = guard_temperature(detail.get("temperature_f"), display_unit)
    boundary = guard_temperature(detail.get("boundary_f"), display_unit)
    required = guard_temperature(detail.get("required_f"), display_unit)
    # Small Fahrenheit headroom can round to the same tenth in Celsius. Keep
    # the comparison understandable instead of saying "21.9 needs 21.9".
    if temperature == boundary and detail.get("temperature_f") != detail.get("boundary_f"):
        temperature = guard_temperature(detail.get("temperature_f"), display_unit, 2)
        boundary = guard_temperature(detail.get("boundary_f"), display_unit, 2)
    if temperature == required and detail.get("temperature_f") != detail.get("required_f"):
        temperature = guard_temperature(detail.get("temperature_f"), display_unit, 2)
        required = guard_temperature(detail.get("required_f"), display_unit, 2)
    action = "cooling" if mode == "cool" else "heating"
    kind = detail.get("kind")
    if kind == "comfort_rearm" and temperature and required:
        text = f"{label} {temperature} needs {required} before {action} restarts"
    elif kind == "comfort_limit" and temperature and boundary:
        value, edge = detail["temperature_f"], detail["boundary_f"]
        relation = "below" if value < edge else "above" if value > edge else "at"
        limit = "floor" if mode == "cool" else "ceiling"
        text = f"{label} {temperature} is {relation} its {boundary} comfort {limit}"
    elif kind == "house_limit" and temperature and boundary:
        text = f"{label}: {action} blocked by {boundary} house guard (reading {temperature}"
        projected = guard_temperature(detail.get("projected_f"), display_unit)
        if projected and projected != temperature:
            text += f", forecast {projected}"
        text += ")"
    else:
        text = detail.get("reason") or f"{label}: {action} blocked"
    waiting = [guard_room_label(zone.name or key) for key, zone in config.zones.items()
               if zone.head == head and key != room_id
               and status.get("room_targets", {}).get(key, {}).get(f"{mode}_requested")]
    if waiting:
        # The explanation remains useful on both room cards sharing one head.
        text = text.removesuffix(f"; {action} waits")
        text += "; " + ", ".join(waiting) + f" {action} waits"
    elif kind == "comfort_limit":
        text += f"; {action} waits"
    return text


def head_constraint_messages(config, status, display_unit, head=None):
    return [head_constraint_message(config, status, key, mode, detail, display_unit)
            for key, mode, detail in current_head_constraints(status, head, config)]


def head_protection_messages(config, status, display_unit, head=None):
    """Protection is a planned exception to comfort, never actuator feedback."""
    if not has_plan(status):
        return []
    result = []
    recovering = {*status.get("device_issues", {}), *status.get("recovery_details", {}),
                  *status.get("waiting_devices", [])}
    for key, detail in status.get("head_protections", {}).items():
        mode = detail.get("mode")
        if ((head is not None and head != key) or key not in status.get("head_targets_f", {})
                or mode != status.get("mode") or mode not in ("heat", "cool")
                or config.heads.get(key) in recovering):
            continue
        origins = [guard_room_label(config.zones[name].name or name) for name in detail.get("rooms", [])
                   if name in config.zones]
        action = "Cooling" if mode == "cool" else "Heating"
        text = action + " for " + (", ".join(origins) + " house protection" if origins else "house protection")
        relaxed = []
        for name, room in config.zones.items():
            target = status.get("room_targets", {}).get(name, {})
            temperature = status.get("rooms", {}).get(name, {}).get("temperature")
            boundary = target.get("low_f" if mode == "cool" else "high_f")
            if (room.head != key or target.get("comfort_enabled") is not True
                    or not isinstance(temperature, (int, float)) or not isinstance(boundary, (int, float))):
                continue
            if (temperature <= boundary if mode == "cool" else temperature >= boundary):
                relaxed.append(guard_room_label(room.name or name))
        if relaxed:
            limit = "floor" if mode == "cool" else "ceiling"
            text += "; " + ", ".join(relaxed) + f" comfort {limit} relaxed"
            result.append(text)
    return result


def head_guard_messages(config, status, display_unit, head=None):
    return (head_protection_messages(config, status, display_unit, head)
            + head_constraint_messages(config, status, display_unit, head))


def current_issue_messages(config, status, display_unit):
    blockers = current_head_constraints(status, config=config)
    messages = [head_constraint_message(config, status, head, mode, detail, display_unit)
                for head, mode, detail in blockers]
    reasons = [detail.get("reason") for _, _, detail in blockers if detail.get("reason")]
    messages.extend(warning for warning in plan_warnings(status)
                    if not any(reason in warning for reason in reasons))
    return list(dict.fromkeys(messages))


def capability_message(detail):
    mode = detail.get("required_mode") or "Requested mode"
    modes = ", ".join(detail.get("available_modes") or []) or "not reported"
    return f"{mode} unavailable (HA modes: {modes})"


def cycle_hold_message(hold, last_stop=None):
    """Describe confirmed timing separately from an off still awaiting a report."""
    remaining = hold.get("remaining_seconds")
    seconds = (max(0, ceil(remaining))
               if isinstance(remaining, (int, float)) and isfinite(remaining) else None)
    kind = hold.get("kind")
    stop_reason = (last_stop or {}).get("reason")
    stop_message = f"; last stop: {stop_reason}" if kind == "minimum_off" and stop_reason else ""
    if kind == "minimum_off":
        if "acknowledgement" in (hold.get("reason") or "").lower():
            following = f"minimum off time of {seconds}s follows" if seconds is not None else "minimum off time follows"
            return f"Waiting for off confirmation; {following}" + stop_message
        message = "Waiting for minimum off time"
    elif kind == "minimum_run":
        message = "Finishing minimum run"
    else:
        message = hold.get("reason") or "Waiting for head cycle timing"
    return message + (f": {seconds}s remaining" if seconds is not None else "") + stop_message


def cycle_hold_summary(status):
    """Keep the overview short; Recovery status lists rooms and countdowns."""
    recovering = {*status.get("device_issues", {}), *status.get("recovery_details", {}),
                  *status.get("waiting_devices", [])}
    holds = {entity: hold for entity, hold in status.get("cycle_holds", {}).items()
             if entity not in recovering}
    if not holds:
        return None
    kinds = {hold.get("kind") for hold in holds.values()}
    if kinds == {"minimum_run"}:
        return "Finishing minimum head run"
    return "Waiting for head cycle timing"


def visible_pending_heads(status):
    """Safety/recovery and cycle timing take precedence over ordinary feedback waits."""
    if (status.get("event") != "live" or status.get("mode") == "off"
            or phase_message(status)):
        return {}
    blocked = {*status.get("device_issues", {}), *status.get("recovery_details", {}),
               *status.get("waiting_devices", []), *status.get("cycle_holds", {})}
    return {entity: detail for entity, detail in status.get("pending_head_commands", {}).items()
            if entity not in blocked}


def pending_head_message(detail, display_unit):
    """Describe requested settings and HA feedback without implying acceptance."""
    remaining = detail.get("remaining_seconds")
    seconds = (max(0, ceil(remaining))
               if isinstance(remaining, (int, float)) and isfinite(remaining) else None)
    text = "Waiting for head confirmation"
    if seconds is not None:
        text += f": {seconds}s remaining"

    def setting(mode, value):
        result = str(mode or "unknown")
        if isinstance(value, (int, float)) and isfinite(value):
            native_unit = detail.get("temperature_unit") or display_unit
            try:
                converted = from_f(to_f(value, native_unit), display_unit)
            except ValueError:
                return result
            target = f"{converted:.1f}".rstrip("0").rstrip(".")
            result += f" {target} {display_unit}"
        return result

    requested = setting(detail.get("requested_mode"), detail.get("requested_target_temperature"))
    reported = setting(detail.get("reported_mode"), detail.get("reported_target_temperature"))
    return text + f"; requested {requested}, reported {reported}"


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [ControllerStatus(entry), Overview(entry), RecoveryStatus(entry),
                CurrentIssues(entry), LastFault(entry)]
    if getattr(entry.runtime_data, "power_guard", None) is not None:
        entities.append(HeaterPowerStatus(entry))
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
            "name": DEVICE_NAME,
            "manufacturer": NAME,
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
                ("mode", "phase", "reasons", "warnings", "head_targets_f", "head_call_targets_f",
                 "heater_on", "auxiliary_fan_only", "fans", "room_targets", "error",
                 "last_error", "last_fault_at", "fault_count",
                 "control_enabled", "control_block_reason", "pending_fans",
                 "configuration_reload_pending", "configuration_reload_reason",
                 "device_issues", "waiting_devices", "recovery_details",
                 "settle_remaining_seconds", "integration_recovery",
                 "decision_interval_seconds", "decision_remaining_seconds", "cycle_holds",
                 "head_stop_reasons", "pending_head_commands", "head_constraints",
                 "head_protections") if key in status} | heartbeat_attributes(status)


class HeaterPowerStatus(ControllerEntity):
    """Explain heater shedding separately from shared heat-pump recovery."""
    _attr_name = "Heater power guard"
    _attr_icon = "mdi:flash-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry, "heater_power_guard")

    @property
    def native_value(self):
        guard = self.runtime.power_guard.diagnostics
        if guard.get("power_restore_inflight") or guard.get("power_restore_cleanup_pending"):
            return "Waiting for heater outlet restoration to stop safely"
        if not guard.get("live"):
            return "Preview only; live commands are not permitted"
        if guard.get("restoring"):
            return "Restoring heater power; confirming thermostat Off"
        return short(guard.get("reason") or ("Blocked" if guard.get("blocked") else "Ready"))

    @property
    def extra_state_attributes(self):
        # Original circuit/battery sensors already retain measured values. Keep
        # this status focused on the interlock and its ownership/recovery state.
        guard = self.runtime.power_guard.diagnostics
        return {key: guard[key] for key in
                ("blocked", "reason", "latched", "switch_cut_owned", "power_cut_confirmed",
                 "restoring", "restore_attempts", "power_restore_inflight",
                 "power_restore_cleanup_pending", "persistence_error", "live") if key in guard}


class Overview(ControllerEntity):
    _attr_name = "Plan summary"
    _attr_icon = "mdi:clipboard-text-outline"

    def __init__(self, entry):
        super().__init__(entry, "overview")

    @property
    def native_value(self):
        status = self.runtime.status
        if status.get("configuration_reload_pending"):
            return short(status["configuration_reload_reason"])
        if not has_plan(status):
            return short(inactive_message(status))
        if issue := heartbeat_issue(status):
            return short("Live plan · " + issue)
        preview = status["event"] == "shadow"
        prefix = "Preview only · " if preview else "Live plan · "
        guards = head_guard_messages(self.runtime.config, status, guard_display_unit(self))
        if preview and status.get("control_block_reason"):
            prefix += status["control_block_reason"] + " · "
        if (not preview and (wait := phase_message(status))
                and (wait != "Ready; no heat-pump demand" or not guards)):
            prefix += wait + " · "
        if not preview and (hold := cycle_hold_summary(status)):
            prefix += hold + " · "
        if not preview and visible_pending_heads(status):
            prefix += "Waiting for head confirmation · "
        if guards:
            prefix += guards[0] + " · "
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
        if auxiliary_heartbeat_issue(status):
            text += "; auxiliary heat blocked"
        elif status.get("heater_on"):
            text += "; auxiliary heat " + ("would be on" if preview else "requested")
        elif status.get("auxiliary_fan_only"):
            text += "; auxiliary fan-only " + ("would be on" if preview else "requested")
        return short(prefix + text)

    @property
    def extra_state_attributes(self):
        status = self.runtime.status
        return {key: status[key] for key in ("head_constraints", "head_protections") if key in status}


class RecoveryStatus(ControllerEntity):
    """Put recovery progress on the device page without opening attributes."""
    _attr_name = "Recovery status"
    _attr_icon = "mdi:progress-clock"

    def __init__(self, entry):
        super().__init__(entry, "recovery")

    @property
    def native_value(self):
        status = self.runtime.status
        if status.get("configuration_reload_pending"):
            return short(status["configuration_reload_reason"])
        reload_status = status.get("integration_recovery", {}).get("reload_status")
        reload_message = reload_status if reload_status and reload_status not in ("Idle", "Disabled", "Shadow mode", "Stopped") else None
        if not has_plan(status):
            message = inactive_message(status)
            return short(message + (" · " + reload_message if reload_message else ""))
        if status["event"] == "shadow":
            reason = status.get("control_block_reason")
            guards = head_guard_messages(self.runtime.config, status, guard_display_unit(self))
            messages = ["Preview only", reason, reload_message, *guards]
            return short(" · ".join(message for message in messages if message))
        if issue := heartbeat_issue(status):
            return short(issue)
        details = status.get("recovery_details", {})
        issues = status.get("device_issues", {})
        waiting = dict.fromkeys([*details, *issues, *status.get("waiting_devices", [])])
        messages = [reload_message] if reload_message else []
        if issue := auxiliary_heartbeat_issue(status):
            messages.append(issue)
        for entity_id in waiting:
            detail = details.get(entity_id, {})
            reason = (capability_message(detail) if detail.get("kind") == "capability"
                      else detail.get("reason") or issues.get(entity_id)
                      or "Waiting for off confirmation")
            messages.append(f"{climate_label(self.runtime.config, entity_id)}: {reason}")
        for entity_id, hold in status.get("cycle_holds", {}).items():
            if entity_id not in waiting:
                last_stop = status.get("head_stop_reasons", {}).get(entity_id)
                messages.append(f"{climate_label(self.runtime.config, entity_id)}: {cycle_hold_message(hold, last_stop)}")
        for entity_id, detail in visible_pending_heads(status).items():
            message = pending_head_message(detail, self.hass.config.units.temperature_unit)
            messages.append(f"{climate_label(self.runtime.config, entity_id)}: {message}")
        guards = head_guard_messages(self.runtime.config, status, guard_display_unit(self))
        if (wait := phase_message(status)) and (wait != "Ready; no heat-pump demand" or not guards):
            messages.append(wait)
        messages.extend(guards)
        return short("; ".join(messages) or "Ready")

    @property
    def extra_state_attributes(self):
        status = self.runtime.status
        return {key: status[key] for key in
                ("phase", "waiting_devices", "recovery_details", "settle_remaining_seconds",
                 "integration_recovery", "cycle_holds", "head_stop_reasons", "pending_head_commands",
                 "head_constraints", "head_protections") if key in status} | heartbeat_attributes(status)


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
        issues = current_issue_messages(self.runtime.config, status, guard_display_unit(self))
        if status.get("control_block_reason"):
            issues.insert(0, status["control_block_reason"])
        if issue := heartbeat_issue(status):
            issues.insert(0, issue)
        return short("; ".join(issues) or "None")

    @property
    def extra_state_attributes(self):
        status = self.runtime.status
        return {"warnings": current_issue_messages(self.runtime.config, status, guard_display_unit(self)),
                **{key: status[key] for key in ("head_constraints", "head_protections") if key in status}
                } | heartbeat_attributes(status)


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
        cycle_hold = (status.get("cycle_holds", {}).get(config.heads[room.head])
                      if status.get("event") == "live" else None)
        pending_head = visible_pending_heads(status).get(config.heads[room.head])
        waiting = (status.get("event") == "live"
                   and config.heads[room.head] in status.get("device_issues", {}))
        if waiting:
            detail = status.get("recovery_details", {}).get(config.heads[room.head], {})
            action = (f"keep {head} off; {capability_message(detail)}"
                      if detail.get("kind") == "capability"
                      else f"wait for {head}; off requested")
        elif cycle_hold and target is not None:
            last_stop = status.get("head_stop_reasons", {}).get(config.heads[room.head])
            timing = cycle_hold_message(cycle_hold, last_stop).lower()
            if cycle_hold.get("kind") == "minimum_run":
                action = f'{head}: {timing}; {status["mode"]} toward {self.temperature(target)}'
            else:
                action = f'{head}: {timing}; then request {status["mode"]} toward {self.temperature(target)}'
        elif pending_head:
            message = pending_head_message(pending_head, self.hass.config.units.temperature_unit)
            action = f"{head}: {message[0].lower()}{message[1:]}"
            if target is not None:
                action += f"; room target {self.temperature(target)}"
        elif target is not None:
            call_target = status.get("head_call_targets_f", {}).get(room.head)
            if call_target is not None and not isclose(call_target, target, rel_tol=0, abs_tol=.05):
                action = (f'{status["mode"]} {head} toward {self.temperature(target)} '
                          f'(head setpoint {self.temperature(call_target)})')
            else:
                action = f'{status["mode"]} {head} to {self.temperature(target)}'
        else:
            action = f"keep {head} off"
            blockers = head_constraint_messages(config, status, self.hass.config.units.temperature_unit, room.head)
            if blockers:
                action += "; " + blockers[0]
            else:
                waiting = [mode for mode in ("heat", "cool") if limits[f"{mode}_requested"]]
                if waiting:
                    action += " (" + "/".join(waiting) + " requested)"
        if target is not None:
            protection = head_protection_messages(config, status, self.hass.config.units.temperature_unit, room.head)
            if protection:
                action += "; " + protection[0]
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
                "head_call_target_f": status.get("head_call_targets_f", {}).get(head),
                **({"head_constraints": status["head_constraints"][head]}
                   if head in status.get("head_constraints", {}) else {}),
                **({"head_protection": status["head_protections"][head]}
                   if head in status.get("head_protections", {}) else {}),
                **({"last_stop": status["head_stop_reasons"][config.heads[head]]}
                   if config.heads[head] in status.get("head_stop_reasons", {}) else {}),
                **({"pending_head_command": status["pending_head_commands"][config.heads[head]]}
                   if config.heads[head] in status.get("pending_head_commands", {}) else {}),
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
        if issue := auxiliary_heartbeat_issue(status):
            return short("Auxiliary heat blocked · " + issue)
        if (status.get("event") == "live"
                and self.runtime.config.heater_entity in status.get("device_issues", {})):
            detail = status.get("recovery_details", {}).get(self.runtime.config.heater_entity, {})
            if detail.get("kind") == "capability":
                return short("Heater kept off · " + capability_message(detail))
            return "Waiting for heater · off requested"
        action = ("heat using room probes" if status["heater_on"] else
                  "circulate air without heat" if status.get("auxiliary_fan_only") else "stay off")
        return plan_prefix(status) + action
