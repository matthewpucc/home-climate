"""Raw receipt evidence never promotes merged/cached data into a fresh sample."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import threading
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate.bluetooth_freshness import BluetoothFreshness, h5100_temperature
from custom_components.house_climate.controller.config import Probe
from custom_components.house_climate.controller.sensors import Sensors, probe_evidence
from custom_components.house_climate.runtime import NativeAPI
from tests.test_source_evidence import configuration

ADDRESS = "AA:BB:CC:DD:EE:01"
ENTITY = "sensor.synthetic_h5100"
# Synthetic 21.4 C / 60% / 100% battery, matching the documented H5100 format.
PAYLOAD = b"\x01\x00\x01\x01" + (214600).to_bytes(3, "big") + b"\x64"
RAW = bytes((len(PAYLOAD) + 1, 0xff)) + PAYLOAD


class BluetoothFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.components.add("govee_ble")
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass, load_empty=True)
        self.owner = ConfigEntry(version=1, minor_version=1, domain="govee_ble", entry_id="synthetic_govee",
            title="Synthetic Govee", data={}, options={}, source="bluetooth", unique_id=ADDRESS, state=ConfigEntryState.LOADED,
            discovery_keys=MappingProxyType({}), subentries_data=None)
        self.hass.config_entries._entries[self.owner.entry_id] = self.owner
        self.device = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)}, model="H5100", manufacturer="Govee")
        self.entity = er.async_get(self.hass).async_get_or_create("sensor", "govee_ble", f"{ADDRESS}-temperature",
            suggested_object_id="synthetic_h5100", config_entry=self.owner, device_id=self.device.id)
        self.assertEqual(self.entity.entity_id, ENTITY)
        self.hass.states.async_set(ENTITY, "21.4", {"unit_of_measurement": "°C"})
        self.hass.states.get(ENTITY).last_reported = datetime.now(timezone.utc) - timedelta(hours=1)
        self.cfg = configuration([{"entity_id": ENTITY, "source": "desk"}])
        self.clock = Mock(return_value=100.)
        self.cancel = Mock()
        self.register = self.enterContext(patch(
            "homeassistant.components.bluetooth.async_register_advertisement_callback", return_value=self.cancel))
        self.monitor = BluetoothFreshness(self.hass, self.cfg, clock=self.clock)
        self.monitor.async_start()
        self.api = NativeAPI(self.hass, False, threading.Event(), self.cfg)
        self.api.bluetooth_freshness = self.monitor

    async def asyncTearDown(self):
        self.monitor.async_close()
        await self.hass.async_stop()
        self.directory.cleanup()

    def packet(self, stamp=101., raw=RAW, address=ADDRESS, **values):
        self.clock.return_value = stamp
        info = SimpleNamespace(address=address, raw=raw, time=stamp, source="synthetic_proxy",
                               manufacturer_data={1: PAYLOAD[2:]}, **values)
        self.register.call_args.args[1](info)
        return info

    def snapshot(self):
        return {s["entity_id"]: s for s in self.api._read_states_without_controls()}

    def evidence(self):
        return probe_evidence(self.cfg.zones["room"].probes[0], self.snapshot(),
                              datetime.now(timezone.utc), self.cfg.sensor_stale_seconds)

    async def test_stable_raw_packets_keep_value_fresh_without_measurement_or_progress(self):
        self.register.assert_called_once()
        self.assertEqual(self.register.call_args.args[2], ADDRESS)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.evidence()
        original = self.hass.states.get(ENTITY).last_reported
        self.packet()
        first = self.evidence()
        self.assertEqual(first["timestamp_kind"], "bluetooth_packet_receipt")
        self.assertEqual(first["freshness"], "qualified")
        self.assertIsNone(first["measurement_at"])
        self.assertIsNone(first["observed_at"])
        self.assertAlmostEqual(first["temperature_f"], 70.52)
        reader = Sensors(self.cfg)
        room = reader.read(self.snapshot(), datetime.now(timezone.utc))[0]["room"]
        self.packet(160.)
        repeat = reader.read(self.snapshot(), datetime.now(timezone.utc))[0]["room"]
        self.assertEqual(room.observation_id, repeat.observation_id)
        self.assertEqual(self.hass.states.get(ENTITY).last_reported, original)
        self.assertEqual(self.monitor.counts["accepted"], 2)

    async def test_stopped_packets_expire_using_monotonic_time(self):
        self.packet()
        self.clock.return_value = 101 + self.cfg.sensor_stale_seconds + .1
        with self.assertRaisesRegex(ValueError, "stale"):
            self.evidence()

    async def test_malformed_wrong_device_and_cached_fields_cannot_renew(self):
        for raw in (None, b"", b"\x02\x01\x06", RAW[:-1], RAW + RAW,
                    RAW[:-1] + b"\xe4", b"\x00\xff", bytes((9, 255)) + b"\x02" + PAYLOAD[1:]):
            with self.subTest(raw=raw):
                self.packet(raw=raw)
                with self.assertRaises(ValueError):
                    self.evidence()
        self.packet(address="AA:BB:CC:DD:EE:02")
        self.assertEqual(self.monitor.counts["accepted"], 0)
        self.packet()
        before = self.monitor._snapshot[ENTITY]
        self.packet(200., raw=b"\x02\x01\x06")  # Still carries old merged manufacturer_data.
        self.assertEqual(self.monitor._snapshot[ENTITY], before)

    async def test_cached_replay_restart_future_and_backwards_receipts_rejected(self):
        self.packet(99.)  # Before registration.
        self.assertEqual(self.monitor.counts["accepted"], 0)
        info = self.packet()
        first = self.monitor._snapshot[ENTITY]
        self.clock.return_value = 102.
        self.monitor._receive(ENTITY, info)
        self.monitor._receive(ENTITY, SimpleNamespace(**{**vars(info), "time": 103.}))
        self.assertEqual(self.monitor._snapshot[ENTITY], first)
        self.monitor.async_close()
        self.monitor = BluetoothFreshness(self.hass, self.cfg, clock=self.clock)
        self.monitor.async_start()
        self.api.bluetooth_freshness = self.monitor
        self.monitor._receive(ENTITY, info)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.evidence()
        self.packet(104.)
        self.assertEqual(self.evidence()["freshness"], "qualified")

    async def test_entity_value_unavailable_restored_mismatch_or_rebinding_cannot_use_receipt(self):
        self.packet()
        for value, attrs in (("unavailable", {}), ("99", {}), ("nan", {}),
                              ("21.4", {"restored": True}), ("21.4", {"assumed_state": True})):
            self.hass.states.async_set(ENTITY, value, {"unit_of_measurement": "°C", **attrs})
            self.assertNotIn("bluetooth_temperature_receipt", self.snapshot()[ENTITY])
        self.hass.states.async_set(ENTITY, "21.4", {"unit_of_measurement": "°C"})
        dr.async_get(self.hass).async_update_device(self.device.id, model="H5101")
        self.assertNotIn("bluetooth_temperature_receipt", self.snapshot()[ENTITY])

    async def test_explicit_physical_timestamp_and_active_fan_guards_still_apply(self):
        self.packet()
        data = self.snapshot()
        now = datetime.now(timezone.utc)
        probe = replace(self.cfg.zones["room"].probes[0], observation_time_entity="sensor.acquired")
        data["sensor.acquired"] = {"state": (now - timedelta(hours=1)).isoformat()}
        with self.assertRaisesRegex(ValueError, "physical observation"):
            probe_evidence(probe, data, now, 900)
        probe = Probe(ENTITY, "desk", require_active_entity="fan.purifier")
        with self.assertRaisesRegex(ValueError, "circulation"):
            probe_evidence(probe, data, now, 900)

    async def test_real_remote_scanner_keeps_merged_payload_but_raw_packet_does_not_renew(self):
        from habluetooth import BluetoothManager, BaseHaRemoteScanner, set_manager
        from habluetooth.central_manager import CentralBluetoothManager
        old = CentralBluetoothManager.manager
        self.addCleanup(set_manager, old)
        manager = BluetoothManager()
        set_manager(manager)
        scanner = BaseHaRemoteScanner("synthetic_proxy", "Synthetic proxy")
        self.monitor.async_close()
        self.register.side_effect = lambda hass, callback, address: manager.async_register_advertisement_callback(callback, address)
        self.monitor = BluetoothFreshness(self.hass, self.cfg, clock=self.clock)
        self.monitor.async_start()
        self.api.bluetooth_freshness = self.monitor
        self.clock.return_value = 101.
        scanner._async_on_raw_advertisement(ADDRESS, -50, RAW, {}, 101.)
        first = self.monitor._snapshot[ENTITY]
        self.clock.return_value = 102.
        scanner._async_on_raw_advertisement(ADDRESS, -50, b"\x02\x01\x06", {}, 102.)
        merged = scanner._previous_service_info[ADDRESS]
        self.assertEqual(merged.manufacturer_data, {1: PAYLOAD[2:]})
        self.assertEqual(merged.raw, b"\x02\x01\x06")
        self.assertEqual(self.monitor._snapshot[ENTITY], first)
        self.clock.return_value = 103.
        scanner._async_on_raw_advertisement(ADDRESS, -50, RAW, {}, 103.)
        self.clock.return_value = 104.
        scanner._async_on_raw_advertisement(ADDRESS, -50, RAW, {}, 104.)
        self.assertEqual(self.monitor.counts["accepted"], 3)
        self.assertEqual(self.monitor._snapshot[ENTITY]["monotonic"], 104.)

    async def test_unregister_clears_receipts_and_late_callbacks_are_ignored(self):
        self.packet()
        self.monitor.async_close()
        self.packet(102.)
        self.cancel.assert_called_once()
        self.assertEqual(dict(self.monitor._snapshot), {})


class H5100DecoderTests(unittest.TestCase):
    def test_positive_negative_and_range(self):
        self.assertAlmostEqual(h5100_temperature(RAW), 21.4)
        payload = PAYLOAD[:4] + (0x800000 | 100600).to_bytes(3, "big") + b"\x64"
        self.assertEqual(h5100_temperature(bytes((9, 255)) + payload), -10.)
        with self.assertRaises(ValueError):
            h5100_temperature(bytes((9, 255)) + PAYLOAD[:4] + (1000600).to_bytes(3, "big") + b"\x64")
