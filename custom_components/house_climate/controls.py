"""Native control interlocks and an HA-loop watchdog independent of the worker.

Only ownership is persisted. Enabling control is an explicit, per-load action.
The worker sees an internal adapter snapshot; no fake HA helper entities are made.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .controller.ha import HAError
from .controller.ha_package import watchdog_signature
from .controller.sensors import probe_value

_LOGGER = logging.getLogger(__name__)


class NativeControls:
    def __init__(self, hass, config, live, entry_id, notify):
        self.hass, self.config, self.live, self.notify = hass, config, live, notify
        self.store = Store(hass, 1, f"house_climate.{entry_id}.ownership")
        self.enabled = False
        self.active = False
        self.owned = ()
        self.recovering = False
        self.heartbeat = 0.0
        self.heartbeat_monotonic = 0.0
        self.inflight = 0
        self.problem = None
        self._lock = asyncio.Lock()
        self._unsubscribers = []
        self._task = None
        self._snapshot = {}
        self._publish()

    async def async_load(self):
        data = await self.store.async_load()
        if data:
            devices = data.get("devices", [])
            if not isinstance(devices, list) or any(
                not isinstance(entity, str) or not entity.startswith(("climate.", "fan."))
                for entity in devices
            ):
                raise HAError("Invalid saved controller ownership")
            self.owned = tuple(devices)
            self.recovering = bool(self.owned)
        self._publish()

    @callback
    def async_start(self):
        if self.active:
            return
        self.active = True
        entities = {*self.config.climate_entities, *self.config.fans.values(), *self.owned,
                    *(probe.entity_id for room in self.config.zones.values() for probe in room.probes)}
        self._unsubscribers = [
            async_track_time_interval(self.hass, self._schedule_check, timedelta(seconds=15)),
            async_track_state_change_event(self.hass, entities, self._schedule_check),
        ]
        self._publish()
        self._schedule_check()

    def _publish(self):
        # Assignment publishes a coherent snapshot for synchronous worker reads.
        self._snapshot = {
            "enabled": self.enabled, "owned": bool(self.owned), "active": self.active,
            "heartbeat": self.heartbeat,
        }
        self.notify()

    def overlay(self, states):
        snapshot = self._snapshot
        entity = self.config.entity
        result = dict(states)
        for key, state, attrs in (
            (entity("enabled"), "on" if snapshot["enabled"] else "off", {}),
            (entity("heater_owned"), "on" if snapshot["owned"] else "off", {}),
            (entity("heartbeat", "input_datetime"), str(snapshot["heartbeat"]),
             {"timestamp": snapshot["heartbeat"]}),
            (entity("status", "input_text"), "", {}),
            (entity("auxiliary_watchdog", "automation"), "on" if snapshot["active"] else "off", {}),
            (entity("auxiliary_probe_safe", "binary_sensor"), "on" if self.probes_safe(states) else "off",
             {"configuration_signature": watchdog_signature(self.config)}),
        ):
            result[key] = {"state": state, "attributes": attrs}
        return result

    def probes_safe(self, states):
        if not self.config.heater_entity:
            return True
        values = []
        now = datetime.now(timezone.utc)
        for probe in self.config.zones[self.config.auxiliary_room].probes:
            try:
                values.append(probe_value(probe, states, now, self.config.sensor_stale_seconds,
                                          self.hass.config.units.temperature_unit))
            except (ValueError, TypeError, OverflowError):
                continue
        return bool(values) and max(values) < self.config.heater_cutoff_f

    def _probe_states(self):
        if not self.config.heater_entity:
            return {}
        result = {}
        for probe in self.config.zones[self.config.auxiliary_room].probes:
            state = self.hass.states.get(probe.entity_id)
            if state:
                result[probe.entity_id] = {
                    "state": state.state, "attributes": dict(state.attributes),
                    "last_reported": state.last_reported.isoformat(),
                }
        return result

    @property
    def hint(self):
        if self.problem:
            return self.problem
        if self.recovering:
            return "Waiting for previously owned devices to stop"
        if not self.live:
            return "Enable live commands in Configure → System settings"
        if not self.enabled:
            return "Turn on Controller enabled on this device"
        return None

    @callback
    def pause(self, reason=None):
        self.enabled = False
        self.recovering = bool(self.owned)
        if reason:
            self.problem = reason
        self._publish()
        self._schedule_check()

    async def async_set_enabled(self, enabled):
        if enabled:
            if not self.active:
                raise HAError("Home Assistant is still starting")
            if not self.live:
                raise HAError("Enable live commands in Configure → System settings first")
            self.config.validate(live=True)
            if self.recovering or (self.owned and not self.enabled):
                raise HAError("Waiting for previously owned devices to acknowledge off")
            self.problem = None
        self.enabled = enabled
        if not enabled and self.owned:
            self.recovering = True
        self._publish()
        self._schedule_check()

    def accepts(self, domain, service, entity):
        return (domain, service, entity) in {
            ("input_boolean", "turn_on", self.config.entity("heater_owned")),
            ("input_boolean", "turn_off", self.config.entity("heater_owned")),
            ("input_datetime", "set_datetime", self.config.entity("heartbeat", "input_datetime")),
            ("input_text", "set_value", self.config.entity("status", "input_text")),
        }

    async def async_service(self, domain, service, data):
        if domain == "input_boolean":
            if service == "turn_on":
                async with self._lock:
                    if not self.active or not self.enabled or not self.live or self.recovering:
                        raise HAError("Control is disabled or still stopping")
                    if not self.owned:
                        # Persist the exact mapping before any actuator command.
                        devices = tuple([*self.config.climate_entities, *self.config.fans.values()])
                        try:
                            await self.store.async_save({"devices": list(devices)})
                        except BaseException:
                            self.enabled = False
                            self._publish()
                            raise
                        self.owned = devices
                        self.recovering = not self.enabled
                    self.heartbeat = datetime.now(timezone.utc).timestamp()
                    self.heartbeat_monotonic = time.monotonic()
                    self._publish()
            else:
                self.recovering = bool(self.owned)
                self._publish()
                await self._clear_if_off()
                self._schedule_check()
        elif domain == "input_datetime":
            if not self.enabled or self.recovering:
                raise HAError("Control is disabled or still stopping")
            self.heartbeat = datetime.now(timezone.utc).timestamp()
            self.heartbeat_monotonic = time.monotonic()
            self._publish()
        # The old input_text is only a CLI diagnostic sink; native sensors expose it.

    def check_activation(self, entity, data):
        if (not self.active or not self.live or not self.enabled or self.recovering
                or entity not in self.owned):
            raise HAError("Controller is disabled or stopping; activation blocked")
        if not 0 <= time.monotonic() - self.heartbeat_monotonic <= 120:
            raise HAError("Controller heartbeat expired; activation blocked")
        if (entity == self.config.heater_entity and data.get("hvac_mode") == "heat"
                and not self.probes_safe(self._probe_states())):
            raise HAError("External room probes block auxiliary heat")

    def _is_off(self, entity):
        state = self.hass.states.get(entity)
        return bool(state and state.state == "off"
                    and state.attributes.get("hvac_action") not in ("heating", "cooling"))

    async def _clear_if_off(self):
        async with self._lock:
            if self.owned and not self.inflight and all(self._is_off(entity) for entity in self.owned):
                await self.store.async_save({"devices": []})
                self.owned = ()
                self.recovering = False
                self._publish()

    @callback
    def _schedule_check(self, _event=None):
        if not self.active or (self._task and not self._task.done()):
            return
        self._task = self.hass.async_create_task(self.async_check(), "House Climate watchdog")

    async def _off(self, entity):
        try:
            async with asyncio.timeout(5):
                await self.hass.services.async_call(
                    entity.split(".", 1)[0],
                    "set_hvac_mode" if entity.startswith("climate.") else "turn_off",
                    {"entity_id": entity, **({"hvac_mode": "off"} if entity.startswith("climate.") else {})},
                    blocking=True,
                )
        except Exception as exc:
            self.problem = f"Could not stop {entity}: {exc}"
            _LOGGER.error("House Climate watchdog: %s", self.problem)
            self._publish()

    async def async_check(self):
        """Run on HA's loop even when the control worker stalls or exits."""
        if not self.owned:
            return
        expired = not 0 <= time.monotonic() - self.heartbeat_monotonic <= 120
        if self.recovering or not self.enabled or expired:
            if self.enabled and expired:
                self.enabled = False
                self.problem = "Control paused: controller heartbeat expired"
            self.recovering = True
            self._publish()
            # Off operations are independent and bounded, including unavailable devices.
            await asyncio.gather(*(self._off(entity) for entity in self.owned
                                   if self.inflight or not self._is_off(entity)))
            try:
                await self._clear_if_off()
            except Exception as exc:
                self.problem = f"Could not save controller shutdown: {exc}"
                _LOGGER.error("House Climate watchdog: %s", self.problem)
                self._publish()
        elif self.config.heater_entity in self.owned:
            heater = self.hass.states.get(self.config.heater_entity)
            could_heat = (not heater or heater.state not in ("off", "fan_only")
                          or heater.attributes.get("hvac_action") == "heating")
            if could_heat and not self.probes_safe(self._probe_states()):
                await self._off(self.config.heater_entity)

    async def async_close(self):
        await self.async_set_enabled(False)
        if self._task:
            await self._task
        await self.async_check()
        if self.owned or self.inflight:
            # Keep watchdog subscriptions alive; unload must not abandon ownership.
            return False
        self.active = False
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._publish()
        return True
