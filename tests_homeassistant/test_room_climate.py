"""Room cards use native HA conversion and never write to physical equipment."""
import math
from dataclasses import replace
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.services import _async_service_temperature_set
from homeassistant.components.number.services import _async_set_value
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.house_climate.climate import (
    PRESET_SCHEDULE, PRESET_TEMPORARY, RoomClimate,
    async_setup_entry as setup_climate,
)
from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.number import (
    RoomDesiredTemperature, async_setup_entry as setup_number,
)
from tests.test_controller import config


class RoomClimateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        self.cfg = Config.from_dict(config().to_dict())
        self.value = {"low_f": 70.0, "high_f": 72.0, "target_f": 71.0,
                      "comfort_enabled": True, "hold": False, "expires_at": None}
        self.comfort = SimpleNamespace(
            effective=Mock(side_effect=lambda room: dict(self.value)),
            bounds=Mock(side_effect=lambda room: (self.cfg.low_f,
                self.cfg.heater_cutoff_f if room == self.cfg.auxiliary_room else self.cfg.high_f)),
            async_set_range=AsyncMock(), async_set_target=AsyncMock(),
            async_resume=AsyncMock(), async_hold=AsyncMock(),
        )
        self.runtime = SimpleNamespace(
            config=self.cfg, comfort=self.comfort, live=True,
            stop_event=threading.Event(),
            status={"event": "shadow", "control_enabled": False,
                    "rooms": {"office": {"temperature": 73.25}}},
        )
        self.entry = SimpleNamespace(entry_id="room_cards", runtime_data=self.runtime)
        self.climate = RoomClimate(self.entry, "office")
        self.climate.hass = self.hass
        self.climate.entity_id = "climate.office_comfort"
        self.number = RoomDesiredTemperature(self.entry, "office")
        self.number.hass = self.hass
        self.number.entity_id = "number.office_desired_temperature"

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_standard_card_exposes_a_range_and_desired_number(self):
        self.assertTrue(self.climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)
        self.assertFalse(self.climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)
        self.assertEqual(self.climate.hvac_modes, [HVACMode.HEAT_COOL])
        self.assertEqual(self.climate.hvac_mode, HVACMode.HEAT_COOL)
        self.assertEqual(self.climate.current_temperature, 73.25)
        self.assertEqual(self.climate.target_temperature_low, 70)
        self.assertEqual(self.climate.target_temperature_high, 72)
        self.assertEqual(self.climate.temperature_unit, UnitOfTemperature.FAHRENHEIT)
        self.assertEqual(self.number.native_value, 71)
        self.assertEqual(self.number.native_unit_of_measurement, UnitOfTemperature.FAHRENHEIT)
        self.assertEqual(self.number.native_step, .5)
        self.assertEqual(self.climate.extra_state_attributes["desired_temperature_f"], 71)

    async def test_ha_range_service_converts_celsius_to_native_fahrenheit(self):
        self.hass.config.units = METRIC_SYSTEM
        await _async_service_temperature_set(self.climate, ServiceCall(
            self.hass, "climate", "set_temperature", {"target_temp_low": 20, "target_temp_high": 22}))
        args = self.comfort.async_set_range.await_args.args
        self.assertEqual(args[0], "office")
        self.assertAlmostEqual(args[1], 68)
        self.assertAlmostEqual(args[2], 71.6)
        self.assertEqual(self.climate.state_attributes["target_temp_low"], 21.1)
        self.assertEqual(self.climate.state_attributes["target_temp_high"], 22.2)
        self.assertEqual(self.climate.state_attributes["current_temperature"], 22.9)

    async def test_ha_range_service_passes_fahrenheit_unchanged(self):
        await _async_service_temperature_set(self.climate, ServiceCall(
            self.hass, "climate", "set_temperature", {"target_temp_low": 68, "target_temp_high": 70,
                                         "hvac_mode": HVACMode.HEAT_COOL}))
        self.comfort.async_set_range.assert_awaited_once_with("office", 68, 70)

    async def test_ha_desired_service_converts_celsius_to_native_fahrenheit(self):
        self.hass.config.units = METRIC_SYSTEM
        await _async_set_value(self.number, ServiceCall(self.hass, "number", "set_value", {"value": 22}))
        args = self.comfort.async_set_target.await_args.args
        self.assertEqual(args[0], "office")
        self.assertAlmostEqual(args[1], 71.6)
        self.assertEqual(self.number.unit_of_measurement, UnitOfTemperature.CELSIUS)
        self.assertAlmostEqual(self.number.value, 21.7)

    async def test_ha_desired_service_passes_fahrenheit_unchanged(self):
        await _async_set_value(self.number, ServiceCall(self.hass, "number", "set_value", {"value": 72}))
        self.comfort.async_set_target.assert_awaited_once_with("office", 72)

    async def test_schedule_preset_resumes_and_temporary_preserves_effective_profile(self):
        self.assertEqual(self.climate.preset_mode, PRESET_SCHEDULE)
        await self.climate.async_set_preset_mode(PRESET_TEMPORARY)
        self.comfort.async_hold.assert_awaited_once_with("office")
        self.value.update(hold=True, expires_at="2026-10-17T05:00:00+00:00")
        self.assertEqual(self.climate.preset_mode, PRESET_TEMPORARY)
        self.assertEqual(self.climate.extra_state_attributes["override_expires_at"],
                         "2026-10-17T05:00:00+00:00")
        await self.climate.async_set_preset_mode(PRESET_SCHEDULE)
        self.comfort.async_resume.assert_awaited_once_with("office")
        # Expiry/resume is read from the manager rather than latched by the entity.
        self.value.update(hold=False, expires_at=None)
        self.assertEqual(self.climate.preset_mode, PRESET_SCHEDULE)

    async def test_editing_is_available_in_shadow_disabled_and_missing_probe_states(self):
        self.runtime.status["rooms"] = {}
        self.assertTrue(self.climate.available)
        self.assertTrue(self.number.available)
        self.assertIsNone(self.climate.current_temperature)
        await self.climate.async_set_temperature(target_temp_low=68, target_temp_high=70)
        await self.number.async_set_native_value(69)
        self.comfort.async_set_range.assert_awaited_once_with("office", 68, 70)
        self.comfort.async_set_target.assert_awaited_once_with("office", 69)
        self.assertFalse(self.runtime.status["control_enabled"])
        self.assertEqual(self.runtime.status["event"], "shadow")
        self.runtime.stop_event.set()
        self.assertFalse(self.climate.available)
        self.assertFalse(self.number.available)

    async def test_validation_errors_are_readable_service_errors(self):
        self.comfort.async_set_range.side_effect = ValueError("Range must stay inside system limits")
        with self.assertRaisesRegex(ServiceValidationError, "system limits"):
            await self.climate.async_set_temperature(target_temp_low=65, target_temp_high=70)
        self.comfort.async_set_target.side_effect = ValueError("Desired temperature must be finite")
        with self.assertRaisesRegex(ServiceValidationError, "finite"):
            await self.number.async_set_native_value(math.nan)
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_temperature(temperature=72)
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_temperature(target_temp_low=68, target_temp_high=70,
                                                     hvac_mode=HVACMode.HEAT)
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_hvac_mode(HVACMode.OFF)
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_preset_mode("Away")

    async def test_single_bound_direct_update_preserves_the_other_bound(self):
        await self.climate.async_set_temperature(target_temp_low=69)
        self.comfort.async_set_range.assert_awaited_once_with("office", 69, 72)

    async def test_auxiliary_room_controls_respect_independent_heater_cutoff(self):
        climate = RoomClimate(self.entry, self.cfg.auxiliary_room)
        number = RoomDesiredTemperature(self.entry, self.cfg.auxiliary_room)
        self.assertEqual(climate.min_temp, self.cfg.low_f)
        self.assertEqual(climate.max_temp, self.cfg.heater_cutoff_f)
        self.assertEqual(number.native_max_value, self.cfg.heater_cutoff_f)
        self.assertEqual(self.number.native_max_value, self.cfg.high_f)

    async def test_hvac_action_requires_live_observed_device_feedback(self):
        head = self.cfg.heads[self.cfg.zones["office"].head]
        self.hass.states.async_set(head, "cool", {"hvac_action": "cooling"})
        self.assertEqual(self.climate.hvac_action, HVACAction.OFF)
        self.runtime.status.update(event="live", control_enabled=True)
        self.assertEqual(self.climate.hvac_action, HVACAction.COOLING)
        self.hass.states.async_set(head, "heat", {"hvac_action": "heating"})
        self.assertEqual(self.climate.hvac_action, HVACAction.HEATING)
        self.hass.states.async_set(head, "cool", {"hvac_action": "idle"})
        self.assertEqual(self.climate.hvac_action, HVACAction.IDLE)
        self.hass.states.async_set(head, "cool", {})
        self.assertIsNone(self.climate.hvac_action)
        self.hass.states.async_set(head, "unavailable")
        self.assertIsNone(self.climate.hvac_action)
        self.runtime.status.update(control_enabled=False)
        self.assertEqual(self.climate.hvac_action, HVACAction.OFF)

    async def test_auxiliary_feedback_can_report_observed_heat(self):
        entity = RoomClimate(self.entry, self.cfg.auxiliary_room)
        entity.hass = self.hass
        self.runtime.status.update(event="live", control_enabled=True)
        self.hass.states.async_set(self.cfg.heater_entity, "heat", {"hvac_action": "heating"})
        self.assertEqual(entity.hvac_action, HVACAction.HEATING)
        self.hass.states.async_set(self.cfg.heater_entity, "heat", {})
        self.assertIsNone(entity.hvac_action)

    async def test_room_identity_names_and_shared_head_metadata(self):
        room = self.cfg.zones["office"]
        self.assertEqual(self.climate.unique_id, "room_cards_climate_office")
        self.assertEqual(self.number.unique_id, "room_cards_desired_office")
        self.assertEqual(self.climate.name, room.name)
        self.assertEqual(self.number.name, f"{room.name} desired temperature")
        attrs = self.climate.extra_state_attributes
        self.assertEqual(attrs["head_entity_id"], self.cfg.heads[room.head])
        self.assertEqual(set(attrs["rooms_sharing_head"]),
                         {zone.name or key for key, zone in self.cfg.zones.items()
                          if zone.head == room.head})
        self.assertEqual(self.climate.device_info["identifiers"], {(DOMAIN, self.entry.entry_id)})

    async def test_setup_cleans_removed_owned_room_controls_only(self):
        climate_removed = SimpleNamespace(
            entity_id="climate.removed", domain="climate", platform=DOMAIN,
            unique_id="room_cards_climate_removed")
        number_removed = SimpleNamespace(
            entity_id="number.removed", domain="number", platform=DOMAIN,
            unique_id="room_cards_desired_removed")
        retained = SimpleNamespace(
            entity_id="climate.office", domain="climate", platform=DOMAIN,
            unique_id="room_cards_climate_office")
        external = SimpleNamespace(
            entity_id="climate.other", domain="climate", platform="other",
            unique_id="room_cards_climate_removed")
        registry = Mock()
        entries = [climate_removed, number_removed, retained, external]
        add_climate, add_number = Mock(), Mock()
        with patch("custom_components.house_climate.climate.er.async_get", return_value=registry), patch(
                "custom_components.house_climate.climate.er.async_entries_for_config_entry",
                return_value=entries):
            await setup_climate(self.hass, self.entry, add_climate)
            await setup_number(self.hass, self.entry, add_number)
        self.assertEqual([call.args[0] for call in registry.async_remove.call_args_list],
                         ["climate.removed", "number.removed"])
        self.assertEqual(len(add_climate.call_args.args[0]), len(self.cfg.zones))
        self.assertEqual(len(add_number.call_args.args[0]), len(self.cfg.zones))

    async def test_room_controls_assign_configured_room_area(self):
        room = replace(self.cfg.zones["office"], area_id="office_area")
        self.runtime.config = replace(self.cfg, zones={**self.cfg.zones, "office": room})
        registry = Mock()
        with patch("custom_components.house_climate.climate.er.async_get", return_value=registry):
            await self.climate.async_added_to_hass()
            await self.number.async_added_to_hass()
        self.assertEqual([call.kwargs for call in registry.async_update_entity.call_args_list],
                         [{"area_id": "office_area"}, {"area_id": "office_area"}])
        self.climate._call_on_remove_callbacks()
        self.number._call_on_remove_callbacks()
