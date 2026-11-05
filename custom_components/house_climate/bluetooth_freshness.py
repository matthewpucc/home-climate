"""H5100 receipt evidence from raw packets, never merged advertisement fields.

Receipt is neither physical acquisition time nor independent temperature progress.
Only the native bridge can attach this evidence; HA states are left untouched.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import re
import time
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .controller.sensors import to_f


def h5100_temperature(raw):
    """Decode only the known six-byte H5100 frame in this exact AD packet.

    govee-ble 1.4.0: company 0x0001, prefix 01 01, packed signed temperature
    and humidity, then battery/error. Reject ambiguous or truncated structures.
    Model identity is established separately from the HA registries.
    """
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("raw_unavailable")
    offset, payload = 0, None
    while offset < len(raw):
        size = raw[offset]
        if size == 0:
            if any(raw[offset:]):
                raise ValueError("malformed_packet")
            break
        end = offset + size + 1
        if size < 1 or end > len(raw):
            raise ValueError("malformed_packet")
        kind, data = raw[offset + 1], raw[offset + 2:end]
        if kind == 0xFF:
            if payload is not None or len(data) != 8 or data[:4] != b"\x01\x00\x01\x01":
                raise ValueError("unsupported_manufacturer_frame")
            payload = data[4:]
        offset = end
    if payload is None:
        raise ValueError("no_temperature_in_packet")
    packed, battery = int.from_bytes(payload[:3], "big"), payload[3]
    magnitude = packed & 0x7FFFFF
    temperature = (magnitude // 1000) / 10 * (-1 if packed & 0x800000 else 1)
    if battery & 0x80 or battery > 100 or not -40 <= temperature <= 60:
        raise ValueError("invalid_temperature_frame")
    return temperature


class BluetoothFreshness:
    def __init__(self, hass, config, *, clock=None):
        self.hass, self.config = hass, config
        self.clock = clock or time.monotonic
        self._custom_clock = clock is not None
        self.started = self.clock()
        self._snapshot = MappingProxyType({})
        self._bindings = {}
        self._unsub = []
        self._closed = False
        self.counts = Counter()

    def _identity(self, entity_id):
        entity = er.async_get(self.hass).async_get(entity_id)
        if entity is None or entity.platform != "govee_ble" or entity.disabled:
            return None
        entry = self.hass.config_entries.async_get_entry(entity.config_entry_id)
        device = dr.async_get(self.hass).async_get(entity.device_id) if entity.device_id else None
        if (entry is None or entry.domain != "govee_ble" or entry.state != ConfigEntryState.LOADED
                or device is None or device.model != "H5100"):
            return None
        address = (entry.unique_id or "").upper()
        if (not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address)
                or entity.unique_id.upper() != f"{address}-TEMPERATURE"
                or (dr.CONNECTION_BLUETOOTH, address) not in {
                    (kind, value.upper()) for kind, value in device.connections}):
            return None
        return (address, entity.config_entry_id, entity.device_id)

    @callback
    def async_start(self):
        if "govee_ble" not in self.hass.config.components:
            return
        entities = {p.entity_id for room in self.config.zones.values() for p in room.probes
                    if p.attribute is None}
        bindings = {entity: identity for entity in sorted(entities)
                    if (identity := self._identity(entity)) is not None}
        if not bindings:
            return
        # Optional imports: installations without Govee need no Bluetooth packages.
        from homeassistant.components import bluetooth
        if not self._custom_clock:
            from bluetooth_data_tools import monotonic_time_coarse
            self.clock = monotonic_time_coarse
            self.started = self.clock()
        register = getattr(bluetooth, "async_register_advertisement_callback", None)
        if register is None:
            self.counts["callback_unavailable"] += 1
            return
        for entity_id, identity in bindings.items():
            try:
                cancel = register(self.hass, lambda info, entity=entity_id: self._receive(entity, info), identity[0])
            except (KeyError, RuntimeError):
                self.counts["bluetooth_unavailable"] += 1
                continue
            self._bindings[entity_id] = identity
            self._unsub.append(cancel)

    @callback
    def _receive(self, entity_id, info):
        if self._closed:
            return
        try:
            identity = self._bindings.get(entity_id)
            if (identity is None or self._identity(entity_id) != identity
                    or info.address.upper() != identity[0]):
                raise ValueError("identity_mismatch")
            now, stamp = self.clock(), info.time
            previous = self._snapshot.get(entity_id)
            if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                    or not math.isfinite(stamp) or not 0 <= now - stamp <= 5
                    or stamp <= self.started or (previous and stamp <= previous["monotonic"])):
                raise ValueError("old_or_replayed_packet")
            temperature = h5100_temperature(getattr(info, "raw", None))
            received = datetime.now(timezone.utc).timestamp() - (now - stamp)
            receipt = {"monotonic": stamp, "received_at": received, "temperature_c": temperature}
            self._snapshot = MappingProxyType({**self._snapshot, entity_id: receipt})
            self.counts["accepted"] += 1
        except (ValueError, TypeError, AttributeError) as exc:
            self.counts[str(exc) if isinstance(exc, ValueError) else "invalid_callback"] += 1

    def evidence(self, entity_id, state):
        """Worker-side bounded snapshot; no HA state writes or timestamp renewal."""
        receipt = self._snapshot.get(entity_id)
        if (self._closed or receipt is None
                or self._identity(entity_id) != self._bindings.get(entity_id)):
            return None
        age = self.clock() - receipt["monotonic"]
        if not 0 <= age <= self.config.sensor_stale_seconds:
            return None
        attrs = state.attributes
        try:
            if (attrs.get("restored") or attrs.get("assumed_state")
                    or isinstance(state.state, bool)
                    or abs(to_f(float(state.state), attrs.get("unit_of_measurement"))
                           - to_f(receipt["temperature_c"], "°C")) > .11):
                return None
            if not math.isfinite(float(state.state)):
                return None
        except (ValueError, TypeError):
            return None
        return {"received_at": receipt["received_at"], "temperature_c": receipt["temperature_c"]}

    @callback
    def async_close(self):
        self._closed = True
        for cancel in self._unsub:
            cancel()
        self._unsub.clear()
        self._snapshot = MappingProxyType({})
