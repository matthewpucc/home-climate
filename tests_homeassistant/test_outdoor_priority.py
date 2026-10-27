"""Native outdoor evidence uses the actual HA registry and event subscription."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch

from custom_components.house_climate.controller.config import Config, OutdoorProbe
from custom_components.house_climate.controller.outdoor import read_outdoor, WEATHER_MAX_AGE_SECONDS
from custom_components.house_climate.controller.protection import ProtectionTemperatures
from custom_components.house_climate.runtime import ControllerRuntime
from tests_homeassistant import test_native as fixtures


WEATHER = "weather.fixture_station"
OBSERVATION = "sensor.fixture_station_observation_time"


class NativeOutdoorPriorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle

    def runtime(self, observation_age=60):
        now = datetime.now(timezone.utc)
        self.hass.states.async_set(WEATHER, "sunny", {"temperature": 50, "temperature_unit": "°F"})
        self.hass.states.async_set(OBSERVATION, (now - timedelta(seconds=observation_age)).isoformat())
        base = Config.from_dict(self.cfg.to_dict())
        cfg = replace(base, protection=ProtectionTemperatures(), outdoor_probes=(
            OutdoorProbe(WEATHER, "station", observation_time_entity=OBSERVATION),),
            zones={name: replace(room, schedules=()) for name, room in base.zones.items()})
        return ControllerRuntime(self.hass, cfg, False)

    async def test_companion_timestamp_is_in_private_native_snapshot_and_fences_old_data(self):
        runtime = self.runtime()
        self.hass.states.async_set("sensor.fixture_unrelated_timestamp", datetime.now(timezone.utc).isoformat())
        cached = {row["entity_id"]: row for row in runtime.api._read_states()}
        self.assertIn(OBSERVATION, runtime.api.entity_ids)
        self.assertIn(OBSERVATION, runtime._outdoor_entities)
        self.assertNotIn("sensor.fixture_unrelated_timestamp", cached)
        self.assertEqual(read_outdoor(runtime.config, cached, datetime.now(timezone.utc)).status, "known")
        capability = runtime.recovery.capabilities
        capability.status = {"recovery_inputs": capability.input_signature(),
                             "decision_valid_until_monotonic": None}
        self.assertTrue(capability.fresh_inputs())
        old = datetime.now(timezone.utc) - timedelta(seconds=WEATHER_MAX_AGE_SECONDS + 1)
        self.hass.states.async_set(OBSERVATION, old.isoformat())
        self.hass.states.async_set(WEATHER, "sunny", {"temperature": 50, "temperature_unit": "°F"}, force_update=True)
        self.assertFalse(capability.fresh_inputs(), "Fresh HA publication hid an old physical observation")
        self.assertEqual(self.calls, [])

    async def test_actual_timestamp_subscription_wakes_but_report_and_forecast_noise_do_not(self):
        runtime = self.runtime()
        with patch("custom_components.house_climate.runtime.threading.Thread.start"):
            runtime.async_start()
        try:
            runtime.wake_event.clear()
            current = self.hass.states.get(OBSERVATION).state
            self.hass.states.async_set(OBSERVATION, current, force_update=True)
            await self.hass.async_block_till_done()
            self.assertFalse(runtime.wake_event.is_set())
            self.hass.states.async_set(WEATHER, "sunny", {
                "temperature": 50, "temperature_unit": "°F", "forecast": [{"temperature": -40}]})
            await self.hass.async_block_till_done()
            self.assertFalse(runtime.wake_event.is_set())
            self.hass.states.async_set(OBSERVATION, "unavailable")
            await self.hass.async_block_till_done()
            self.assertTrue(runtime.wake_event.is_set())
            runtime.wake_event.clear()
            self.hass.states.async_set(OBSERVATION, datetime.now(timezone.utc).isoformat())
            await self.hass.async_block_till_done()
            self.assertTrue(runtime.wake_event.is_set())
            self.assertEqual(self.calls, [])
        finally:
            runtime.stop_event.set()
            runtime._remove_climate_listener()
            runtime._remove_climate_listener = None
            runtime.finished.cancel()

    async def test_actual_observation_expiry_invalidates_native_plan_without_entity_change(self):
        runtime = self.runtime(WEATHER_MAX_AGE_SECONDS - 10)
        cached = {row["entity_id"]: row for row in runtime.api._read_states()}
        reading = read_outdoor(runtime.config, cached, datetime.now(timezone.utc))
        self.assertEqual(reading.status, "known")
        capability = runtime.recovery.capabilities
        marker = capability.input_signature()
        capability.status = {"recovery_inputs": marker, "decision_valid_until_monotonic": None}
        expiry = datetime.fromtimestamp(reading.next_expiry, timezone.utc)
        with patch("custom_components.house_climate.capability_recovery.datetime") as clock:
            clock.now.return_value = expiry - timedelta(microseconds=1)
            self.assertTrue(capability.fresh_inputs())
            clock.now.return_value = expiry
            self.assertFalse(capability.fresh_inputs())
        self.assertEqual(cached, {row["entity_id"]: row for row in runtime.api._read_states()})
        # The actual reader horizon also gates queued repair admission exactly.
        capability.status["decision_valid_until_monotonic"] = time.monotonic()
        self.assertFalse(capability.fresh_inputs())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
