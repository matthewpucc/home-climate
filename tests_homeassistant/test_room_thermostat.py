"""Opt-in HomeKit scalar facade against real HA and durable room intent."""
import asyncio
import math
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.services import _async_service_temperature_set
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.climate import (
    RoomClimate, RoomThermostat, PRESET_PROTECTION_ONLY, async_setup_entry,
)
from custom_components.house_climate.sensor import RoomComfortMode
from tests_homeassistant import test_comfort_controls as fixtures


class RoomThermostatTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.RoomComfortTests.handle
    asyncSetUp = fixtures.RoomComfortTests.asyncSetUp
    asyncTearDown = fixtures.RoomComfortTests.asyncTearDown
    manager = fixtures.RoomComfortTests.manager

    async def thermostat(self):
        comfort = await self.manager()
        runtime = SimpleNamespace(config=self.cfg, comfort=comfort, live=True,
                                  stop_event=threading.Event(),
                                  status={"event": "live", "control_enabled": True,
                                          "rooms": {"guest": {"temperature": 68.25}}})
        entry = SimpleNamespace(entry_id="scalar", runtime_data=runtime)
        entity = RoomThermostat(entry, "guest")
        entity.hass, entity.entity_id = self.hass, "climate.guest_thermostat"
        return entity, comfort, entry

    async def test_scalar_opt_in_identity_and_capabilities(self):
        entity, _, entry = await self.thermostat()
        self.assertFalse(entity.entity_registry_enabled_default)
        self.assertEqual(entity.unique_id, "scalar_thermostat_guest")
        self.assertEqual(RoomClimate(entry, "guest").unique_id, "scalar_climate_guest")
        self.assertEqual(entity.supported_features, ClimateEntityFeature.TARGET_TEMPERATURE)
        self.assertEqual(entity.hvac_modes, [HVACMode.OFF, HVACMode.AUTO])
        self.assertEqual(entity.hvac_mode, HVACMode.AUTO)
        self.assertEqual(entity.current_temperature, 68.25)
        attrs = entity.state_attributes
        self.assertIn("temperature", attrs)
        self.assertNotIn("target_temp_low", attrs)
        self.assertNotIn("target_temp_high", attrs)
        self.assertNotIn("preset_modes", entity.capability_attributes)
        self.assertIsNone(entity.hvac_action)

    async def test_temperature_translates_asymmetric_window_and_preserves_hold(self):
        entity, comfort, _ = await self.thermostat()
        await comfort.async_set_target("guest", 68)
        await comfort.async_set_range("guest", 67, 71)
        before = comfort.effective("guest")
        await entity.async_set_temperature(temperature=70)
        after = comfort.effective("guest")
        self.assertEqual(after["low_f"], before["low_f"] + 2)
        self.assertEqual(after["high_f"], before["high_f"] + 2)
        self.assertEqual((after["low_f"], after["high_f"]), (69, 73))
        self.assertEqual(after["mode"], "hold")
        await comfort.async_hold("guest", persistent=True)
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        await entity.async_set_temperature(temperature=71, hvac_mode=HVACMode.AUTO)
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        self.assertEqual(self.calls, [])

    async def test_off_is_persistent_room_scoped_and_auto_resumes_schedule(self):
        entity, comfort, entry = await self.thermostat()
        other = comfort.effective("living")
        await entity.async_set_hvac_mode(HVACMode.OFF)
        self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        self.assertEqual(comfort.effective("guest")["mode"], "protection_only")
        self.assertEqual(comfort.effective("living"), other)
        restored = await self.manager(entry_id="comfort")
        self.assertEqual(restored.effective("guest")["mode"], "protection_only")
        self.assertEqual(RoomClimate(entry, "guest").preset_mode, PRESET_PROTECTION_ONLY)
        mode = RoomComfortMode(entry, "guest")
        self.assertEqual(mode.native_value, PRESET_PROTECTION_ONLY)
        self.assertIn(mode.native_value, mode.options)
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        self.assertEqual(comfort.effective("guest")["mode"], "schedule")
        self.assertEqual(self.calls, [])

    async def test_temperature_while_off_rejected_without_reactivation(self):
        entity, comfort, _ = await self.thermostat()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        before = comfort.effective("guest")
        with self.assertRaises(ServiceValidationError):
            await entity.async_set_temperature(temperature=70)
        self.assertEqual(comfort.effective("guest"), before)

    async def test_combined_siri_auto_and_target_and_off_target(self):
        entity, comfort, _ = await self.thermostat()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        await entity.async_set_temperature(hvac_mode=HVACMode.AUTO, temperature=70)
        self.assertEqual(entity.hvac_mode, HVACMode.AUTO)
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        self.assertEqual(entity.target_temperature, 70)
        await entity.async_set_temperature(hvac_mode=HVACMode.OFF, temperature=71)
        self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        self.assertEqual(entity.target_temperature, 70)

    async def test_concurrent_bridge_mode_then_temperature_preserves_new_intent(self):
        entity, comfort, _ = await self.thermostat()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        await asyncio.gather(entity.async_set_hvac_mode(HVACMode.AUTO),
                             entity.async_set_temperature(hvac_mode=HVACMode.AUTO, temperature=70))
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        self.assertEqual(entity.target_temperature, 70)

    async def test_invalid_combined_target_does_not_resume_off(self):
        entity, _, _ = await self.thermostat()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        for value in (True, "70", math.nan, math.inf, 100):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await entity.async_set_temperature(hvac_mode=HVACMode.AUTO, temperature=value)
            self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        for kwargs in ({"target_temp_low": 68}, {"temperature": 70, "hvac_mode": HVACMode.HEAT}):
            with self.assertRaises(ServiceValidationError):
                await entity.async_set_temperature(**kwargs)

    async def test_native_service_converts_metric_and_normalizes_bounds(self):
        entity, _, _ = await self.thermostat()
        self.hass.config.units = METRIC_SYSTEM
        for value, expected in ((20, 68), (23.9, 75), (18.9, 66)):
            await _async_service_temperature_set(entity, ServiceCall(
                self.hass, "climate", "set_temperature", {"temperature": value}))
            self.assertEqual(entity.target_temperature, expected)
        self.assertEqual(self.calls, [])

    async def test_missing_temperature_and_shared_equipment_do_not_invent_activity(self):
        entity, _, _ = await self.thermostat()
        room = self.cfg.zones["guest"]
        self.hass.states.async_set(self.cfg.heads[room.head], "heat", {"hvac_action": "heating"})
        for value in (None, math.nan, math.inf, "unknown", True):
            entity.runtime.status["rooms"]["guest"]["temperature"] = value
            self.assertIsNone(entity.current_temperature)
            self.assertIsNone(entity.hvac_action)
        await entity.async_set_hvac_mode(HVACMode.OFF)
        self.assertIsNone(entity.hvac_action)

    async def test_registry_cleanup_handles_both_facades_without_touching_external(self):
        _, _, entry = await self.thermostat()
        registry, add = Mock(), Mock()
        stale = SimpleNamespace(entity_id="climate.old", domain="climate", platform="house_climate",
                                unique_id="scalar_thermostat_deleted")
        with patch("custom_components.house_climate.climate.er.async_get", return_value=registry), patch(
                "custom_components.house_climate.climate.er.async_entries_for_config_entry", return_value=[stale]):
            await async_setup_entry(self.hass, entry, add)
        registry.async_remove.assert_called_once_with("climate.old")
        entities = add.call_args.args[0]
        self.assertEqual(sum(isinstance(e, RoomThermostat) for e in entities), len(self.cfg.zones))

    async def test_pinned_homekit_bridge_scalar_characteristics_and_combined_calls(self):
        # Optional HomeKit requirements are not part of the base HA wheel.
        try:
            from pyhap.loader import Loader
            from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage
            from homeassistant.components.homekit.type_thermostats import Thermostat
            from homeassistant.components.homekit.const import (
                CHAR_TARGET_HEATING_COOLING, CHAR_TARGET_TEMPERATURE,
                CHAR_HEATING_THRESHOLD_TEMPERATURE, CHAR_COOLING_THRESHOLD_TEMPERATURE,
            )
        except ModuleNotFoundError as exc:
            self.skipTest(f"Install pinned HA HomeKit optional requirements: {exc}")
        entity, comfort, _ = await self.thermostat()
        def publish():
            self.hass.states.async_set(entity.entity_id, entity.hvac_mode, {
                **entity.capability_attributes, **entity.state_attributes,
                "supported_features": int(entity.supported_features)})
        publish()
        storage = AccessoryIIDStorage(self.hass, "synthetic_homekit")
        await storage.async_initialize()
        driver = Mock(loader=Loader(), iid_storage=storage)
        accessory = Thermostat(self.hass, driver, "Guest thermostat", entity.entity_id, 2, {})
        self.assertEqual(accessory.hc_homekit_to_hass, {0: HVACMode.OFF, 3: HVACMode.AUTO})
        self.assertNotIn(CHAR_HEATING_THRESHOLD_TEMPERATURE, accessory.chars)
        self.assertNotIn(CHAR_COOLING_THRESHOLD_TEMPERATURE, accessory.chars)
        self.assertIsNone(accessory.char_heating_thresh_temp)
        self.assertIsNone(accessory.char_cooling_thresh_temp)
        self.assertEqual(accessory.fan_chars, [])
        self.assertAlmostEqual(accessory.char_target_temp.value,
                               (entity.target_temperature - 32) * 5 / 9, places=1)
        accessory.async_call_service = Mock()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        publish()
        accessory._set_chars({CHAR_TARGET_HEATING_COOLING: 3, CHAR_TARGET_TEMPERATURE: 20})
        calls = accessory.async_call_service.call_args_list
        self.assertEqual([call.args[1] for call in calls], ["set_hvac_mode", "set_temperature"])
        for call in calls:
            args = {key: value for key, value in call.args[2].items() if key != "entity_id"}
            if call.args[1] == "set_hvac_mode":
                await entity.async_set_hvac_mode(**args)
            else:
                await entity.async_set_temperature(**args)
        self.assertEqual(entity.target_temperature, 68)
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        await comfort.async_hold("guest", persistent=True)
        publish()
        accessory.async_call_service.reset_mock()
        accessory._set_chars({CHAR_TARGET_HEATING_COOLING: 3, CHAR_TARGET_TEMPERATURE: 21})
        calls = accessory.async_call_service.call_args_list
        self.assertEqual([call.args[1] for call in calls], ["set_temperature"])
        await entity.async_set_temperature(**{key: value for key, value in calls[0].args[2].items()
                                             if key != "entity_id"})
        self.assertEqual(comfort.effective("guest")["mode"], "hold")
        self.assertAlmostEqual(entity.target_temperature, 69.8)
