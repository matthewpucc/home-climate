"""Virtual room fan requests for the standard HA and HomeKit fan controls."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from math import isfinite

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SERVICE, STATE_ON
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .climate import RoomControl
from .const import DOMAIN

AUTO = "auto"
# Emitted by HA's HomeKit Fan before calling the fan service. Avoid importing
# HomeKit (and its optional HAP dependencies) into the Home Climate integration.
HOMEKIT_CHANGE = "homekit_state_change"
CONTROL_NAMES = {"minimum": "purifier minimum", "speed": "purifier exact speed"}


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [
        RoomPurifierFan(entry, room, kind)
        for room in getattr(getattr(entry.runtime_data, "purifier_speeds", None), "rooms", ())
        for kind in CONTROL_NAMES
    ]
    registry = er.async_get(hass)
    current = {entity.unique_id for entity in entities}
    prefix = f"{entry.entry_id}_purifier_fan_"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == "fan" and entity.platform == DOMAIN
                and entity.unique_id.startswith(prefix) and entity.unique_id not in current):
            registry.async_remove(entity.entity_id)
    async_add_entities(entities)


class RoomPurifierFan(RoomControl, FanEntity):
    """An automatic or explicit room request, not a physical fan accessory."""

    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:air-purifier"
    _attr_preset_modes = [AUTO]
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )

    def __init__(self, entry, room_id, kind):
        super().__init__(entry, room_id, f"purifier_fan_{kind}_{room_id}")
        self.kind = kind
        room = self.runtime.config.zones[room_id]
        self._attr_name = f"{room.name or room_id} {CONTROL_NAMES[kind]}"
        self._command_lock = asyncio.Lock()
        self._homekit_batch = None
        self._homekit_contexts = OrderedDict()

    @property
    def suggested_object_id(self):
        return f"home_climate_purifier_{self.kind}_{self.room_id}"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(HOMEKIT_CHANGE, self._homekit_changed))

    @callback
    def _homekit_changed(self, event):
        if event.data.get(ATTR_ENTITY_ID) != self.entity_id:
            return
        service = event.data.get(ATTR_SERVICE)
        if service not in {"set_percentage", "set_preset_mode", "turn_on", "turn_off"}:
            return
        if self._homekit_batch is None:
            batch = self._homekit_batch = []

            @callback
            def close_batch():
                if self._homekit_batch is batch:
                    self._homekit_batch = None

            self.hass.loop.call_soon(close_batch)
        batch = self._homekit_batch
        self._homekit_contexts[event.context.id] = batch, len(batch)
        batch.append(service)
        # Bound the map even when events don't reach the service handler.
        while len(self._homekit_contexts) > 32:
            self._homekit_contexts.popitem(last=False)

    async def _homekit_neighbors(self):
        # HA sets an entity context before invoking the service. Capture it
        # before awaiting so ordinary HA writes are never treated as HAP writes.
        context = self._context
        found = self._homekit_contexts.pop(context.id, None) if context else None
        if found is None:
            return False, None, None
        batch, index = found
        # Collect the characteristics synchronously emitted by a HAP write.
        # Only handle adjacent slider + preset in upstream's known order;
        # independent On/Off and later sliders retain normal service semantics.
        await asyncio.sleep(0)
        return (True, batch[index - 1] if index else None,
                batch[index + 1] if index + 1 < len(batch) else None)

    @property
    def _speeds(self):
        return self.runtime.purifier_speeds

    @property
    def _manual(self):
        return self._speeds.manual_value(self.room_id, self.kind)

    @property
    def preset_mode(self):
        return AUTO if self._manual is None else None

    @property
    def percentage(self):
        manual = self._manual
        if manual is not None:
            return max(1, round(manual)) if manual > 0 else 0
        if self.kind == "minimum":
            minimum = self._speeds.minimum(self.room_id)
            return max(1, round(minimum)) if minimum > 0 else 0
        # Auto still delegates to the actual policy; this is only a display
        # hint for the room (maximum reported speed if several devices run).
        current = []
        for fan in self.runtime.config.fan_controls.values():
            if fan.room != self.room_id or self.hass is None:
                continue
            state = self.hass.states.get(fan.entity_id)
            if (state is None or state.state != STATE_ON or state.attributes.get("assumed_state")
                    or state.attributes.get("restored")):
                continue
            value = state.attributes.get("percentage")
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and isfinite(value) and 0 < value <= 100):
                current.append(max(1, round(value)))
        return max(current, default=0)

    @property
    def extra_state_attributes(self):
        return {"room_id": self.room_id, "purifier_control": self.kind,
                "speed_mode": "automatic" if self._manual is None else "override"}

    async def _set(self, value):
        if not self.available:
            raise ServiceValidationError("Purifier controls are stopping")
        try:
            await self._speeds.async_set_control(self.room_id, self.kind, value)
        except (ValueError, OSError) as exc:
            raise ServiceValidationError(str(exc)) from exc

    async def _resume_manual(self):
        if not self.available:
            raise ServiceValidationError("Purifier controls are stopping")
        try:
            await self._speeds.async_resume_control(self.room_id, self.kind)
        except (ValueError, OSError) as exc:
            raise ServiceValidationError(str(exc)) from exc

    async def async_set_percentage(self, percentage):
        _, _, following = await self._homekit_neighbors()
        if following == "set_preset_mode":
            return
        async with self._command_lock:
            await self._set(percentage)

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode != AUTO:
            raise ServiceValidationError("Only Auto is supported")
        await self._homekit_neighbors()
        async with self._command_lock:
            await self._set(None)

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        homekit, previous, _ = await self._homekit_neighbors()
        if homekit and percentage is not None and previous == "set_percentage":
            # This is the extra Manual write made after a paired HAP slider.
            return
        async with self._command_lock:
            if preset_mode is not None:
                if preset_mode != AUTO:
                    raise ServiceValidationError("Only Auto is supported")
                await self._set(None)
            elif percentage is not None and homekit:
                # HomeKit sends a synthetic percentage when selecting Manual.
                # Restore the deliberately chosen value, not its stale or 50% guess.
                manual = self._manual
                remembered = self._speeds.last_manual_value(self.room_id, self.kind)
                displayed = self.percentage
                if manual is None and remembered is None and displayed > 0:
                    await self._set(displayed)
                else:
                    await self._resume_manual()
            elif percentage is not None:
                await self._set(percentage)
            elif self._manual == 0:
                await self._resume_manual()
            # A bare On must not replace an existing positive or automatic request.

    async def async_turn_off(self, **kwargs):
        await self._homekit_neighbors()
        async with self._command_lock:
            await self._set(0)
