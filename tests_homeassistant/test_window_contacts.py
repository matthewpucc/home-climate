"""Window evidence uses actual HA cached states/events without actuator writes."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import unittest

from custom_components.house_climate.controller.config import Settings
from custom_components.house_climate.controller.cadence import DecisionCadence
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.comfort import RoomComfort
from custom_components.house_climate.runtime import ControllerRuntime
from custom_components.house_climate.sensor import ControllerStatus, RoomPlan
from tests_homeassistant import test_native as fixtures
from tests.test_rooms import configuration


CONTACT = "binary_sensor.fixture_opening"


class WindowContactNativeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    def configured(self):
        room = next(iter(self.cfg.zones))
        cfg = replace(self.cfg, zones={**self.cfg.zones,
                                      room: replace(self.cfg.zones[room], windows=(CONTACT,))})
        return room, cfg

    async def test_native_cache_includes_explicit_door_class_contact_only(self):
        _room, cfg = self.configured()
        self.hass.states.async_set(CONTACT, "on", {"device_class": "door"})
        self.hass.states.async_set("binary_sensor.fixture_unmapped", "on")
        runtime = ControllerRuntime(self.hass, cfg, False)
        cached = {row["entity_id"]: row for row in runtime.api._read_states()}
        self.assertIn(CONTACT, runtime.api.entity_ids)
        self.assertEqual(cached[CONTACT]["state"], "on")
        self.assertEqual(cached[CONTACT]["attributes"]["device_class"], "door")
        self.assertNotIn("binary_sensor.fixture_unmapped", cached)
        self.assertEqual(self.calls, [])

    async def test_actual_subscription_wakes_for_contact_changes_but_not_report_noise(self):
        _room, cfg = self.configured()
        self.hass.states.async_set(CONTACT, "off")
        runtime = ControllerRuntime(self.hass, cfg, False)
        # Exercise async_start's real subscription without starting its worker.
        # No controller ownership, timers, or device services are created.
        with patch("custom_components.house_climate.runtime.threading.Thread.start"):
            runtime.async_start()
        try:
            runtime.wake_event.clear()
            self.hass.states.async_set(CONTACT, "on")
            await self.hass.async_block_till_done()
            self.assertTrue(runtime.wake_event.is_set())
            runtime.wake_event.clear()
            self.hass.states.async_set(CONTACT, "on", force_update=True)
            await self.hass.async_block_till_done()
            self.assertFalse(runtime.wake_event.is_set())
            self.hass.states.async_set(CONTACT, "on", {"restored": False})
            await self.hass.async_block_till_done()
            self.assertFalse(runtime.wake_event.is_set())
            self.hass.states.async_set(CONTACT, "on", {"restored": True})
            await self.hass.async_block_till_done()
            self.assertTrue(runtime.wake_event.is_set())
            runtime.wake_event.clear()
            self.hass.states.async_set(CONTACT, "unavailable")
            await self.hass.async_block_till_done()
            self.assertTrue(runtime.wake_event.is_set())
            runtime.stop_event.set()
            runtime.wake_event.clear()
            self.hass.states.async_set(CONTACT, "off")
            await self.hass.async_block_till_done()
            self.assertFalse(runtime.wake_event.is_set())
            self.assertEqual(self.calls, [])
        finally:
            runtime.stop_event.set()
            runtime._remove_climate_listener()
            runtime._remove_climate_listener = None
            runtime.finished.cancel()

    async def test_room_plan_and_controller_expose_confirmed_and_unknown_evidence(self):
        room, cfg = self.configured()
        self.hass.states.async_set(CONTACT, "on", {"device_class": "door"})
        runtime = ControllerRuntime(self.hass, cfg, False)
        states = {row["entity_id"]: row for row in runtime.api._read_states()}
        now = datetime.now(timezone.utc)
        cadence = DecisionCadence(cfg)
        settings = Settings.read(states, cfg)
        cadence.decide(states, now, settings, monotonic_now=0)
        plan = cadence.decide(states, now + timedelta(seconds=120), settings, monotonic_now=120)
        runtime.status = {"event": "shadow", **plan.as_dict()}
        entry = SimpleNamespace(entry_id="fixture_window_entry", runtime_data=runtime)
        self.assertEqual(ControllerStatus(entry).extra_state_attributes["window_states"][room]["state"], "open")
        self.assertEqual(RoomPlan(entry, room).extra_state_attributes["window"]["state"], "open")
        states[CONTACT]["state"] = "unavailable"
        runtime.status = {"event": "shadow", **cadence.decide(states, now + timedelta(seconds=121), settings,
                                                               monotonic_now=121).as_dict()}
        window = RoomPlan(entry, room).extra_state_attributes["window"]
        self.assertEqual(window["state"], "unknown")
        self.assertEqual(window["last_confirmed_state"], "open")
        self.assertEqual(self.calls, [])

    async def test_pre_window_stored_hold_survives_upgrade_and_new_contact_wiring(self):
        original = Config.from_dict(configuration())
        room_data = asdict(original.zones["alpha"])
        room_data.pop("windows")  # The exact room payload stored by 0.7.3.
        for probe in room_data["probes"]:
            probe.pop("report_on_change", None)
        old_data = {"room": room_data, "head": original.heads["shared"],
                    "timezone": self.hass.config.time_zone,
                    "limits": [original.low_f, original.high_f], "auxiliary_cutoff": None}
        old_fingerprint = hashlib.sha256(json.dumps(old_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for with_contact in (False, True):
            with self.subTest(with_contact=with_contact):
                cfg = (replace(original, zones={**original.zones,
                                               "alpha": replace(original.zones["alpha"], windows=(CONTACT,))})
                       if with_contact else original)
                comfort = RoomComfort(self.hass, cfg, f"fixture_window_hold_{with_contact}", lambda: None)
                stored = {"rooms": {"alpha": {"low_f": 70, "high_f": 74, "target_f": 72,
                                               "expires_at": None, "fingerprint": old_fingerprint}}}
                try:
                    await comfort.store.async_save(stored)
                    await comfort.async_load()
                    effective = comfort.effective("alpha")
                    self.assertTrue(effective["hold"])
                    self.assertEqual((effective["low_f"], effective["high_f"], effective["target_f"]),
                                     (70, 74, 72))
                    self.assertEqual(comfort._fingerprint("alpha"), old_fingerprint)
                    self.assertEqual(await comfort.store.async_load(), stored)
                finally:
                    await comfort.async_close()


if __name__ == "__main__":
    unittest.main()
