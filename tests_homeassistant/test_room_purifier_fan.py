"""Native virtual fan semantics and pinned HomeKit FanV2 contract."""
import asyncio
import logging
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.components.fan import DATA_COMPONENT
from homeassistant.components.fan.services import async_setup_services
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.house_climate.const import SIGNAL_STATUS
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.fan import AUTO, RoomPurifierFan, async_setup_entry
from custom_components.house_climate.purifier import RoomPurifierSpeeds
from tests.test_directed_airflow import directed_config, FAN


class RoomPurifierFanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.config = Config.from_dict(directed_config())
        self.manager = RoomPurifierSpeeds(
            self.hass, self.config, "fan_fixture",
            lambda: async_dispatcher_send(self.hass, SIGNAL_STATUS), threading.Event())
        await self.manager.async_load()
        self.runtime = SimpleNamespace(config=self.config, purifier_speeds=self.manager,
                                       stop_event=threading.Event())
        self.entry = SimpleNamespace(entry_id="fan_fixture", runtime_data=self.runtime)

    async def asyncTearDown(self):
        await self.manager.async_close()
        if not self.hass.is_stopping:
            await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def add_fans(self):
        component = self.hass.data[DATA_COMPONENT] = EntityComponent(
            logging.getLogger(__name__), "fan", self.hass)
        async_setup_services(self.hass)
        floor = RoomPurifierFan(self.entry, "alpha", "minimum")
        exact = RoomPurifierFan(self.entry, "alpha", "speed")
        # An explicitly enabled one-room pilot in this synthetic HA instance.
        for entity in (floor, exact):
            entity._attr_entity_registry_enabled_default = True
            entity._attr_device_info = None  # Synthetic component has no config entry.
        await component.async_add_entities([floor, exact])
        return floor, exact

    async def service(self, entity, service, **data):
        await self.hass.services.async_call("fan", service,
            {"entity_id": entity.entity_id, **data}, blocking=True)
        await self.hass.async_block_till_done()

    async def test_two_opt_in_entities_per_configured_room_and_registry_isolated(self):
        add = Mock()
        old = SimpleNamespace(domain="fan", platform="house_climate", entity_id="fan.old",
                              unique_id=f"{self.entry.entry_id}_purifier_fan_speed_removed")
        other = SimpleNamespace(domain="number", platform="house_climate", entity_id="number.old",
                                unique_id=old.unique_id)
        registry = Mock()
        with (patch.object(er, "async_get", return_value=registry),
              patch.object(er, "async_entries_for_config_entry", return_value=[old, other])):
            await async_setup_entry(self.hass, self.entry, add)
        (entities,) = add.call_args.args
        self.assertEqual(len(entities), 2 * len(self.manager.rooms))
        self.assertEqual(len({entity.unique_id for entity in entities}), len(entities))
        self.assertTrue(all(not entity.entity_registry_enabled_default for entity in entities))
        self.assertEqual({entity.suggested_object_id for entity in entities},
                         {f"home_climate_purifier_{kind}_{room}" for room in self.manager.rooms
                          for kind in ("minimum", "speed")})
        registry.async_remove.assert_called_once_with("fan.old")

    async def test_native_fan_services_are_virtual_and_controls_independent(self):
        floor, exact = await self.add_fans()
        self.hass.states.async_set(FAN, "on", {"percentage": 64})
        self.assertEqual(exact.percentage, 64)
        for entity in (floor, exact):
            self.assertEqual(entity.preset_modes, [AUTO])
            self.assertEqual(entity.preset_mode, AUTO)
            self.assertTrue(entity.is_on)

        await self.service(floor, "set_percentage", percentage=32)
        await self.service(exact, "turn_on", percentage=67)
        await self.service(floor, "turn_off")
        self.assertEqual((floor.percentage, floor.preset_mode, floor.is_on), (0, None, False))
        self.assertEqual(exact.percentage, 67)
        await self.service(floor, "turn_on")
        self.assertEqual(floor.percentage, 32)
        await self.service(exact, "set_percentage", percentage=0)
        self.assertFalse(exact.is_on)
        await self.service(exact, "turn_on")
        self.assertEqual(exact.percentage, 67)
        await self.service(exact, "set_preset_mode", preset_mode=AUTO)
        self.assertEqual((exact.percentage, exact.preset_mode), (64, AUTO))
        await self.service(floor, "set_preset_mode", preset_mode=AUTO)
        self.assertEqual((floor.percentage, floor.preset_mode), (20, AUTO))
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 64)
        self.assertTrue(self.manager.wake_event.is_set())

    async def test_pinned_homekit_fanv2_chars_and_grouped_write_precedence(self):
        try:
            from pyhap.loader import Loader
            from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage
            from homeassistant.components.homekit.type_fans import Fan
            from homeassistant.components.homekit.const import (
                CHAR_ACTIVE, CHAR_ROTATION_SPEED, CHAR_TARGET_FAN_STATE, SERV_FANV2,
            )
        except ModuleNotFoundError as exc:
            self.skipTest(f"Install pinned HA HomeKit optional requirements: {exc}")
        floor, exact = await self.add_fans()
        storage = AccessoryIIDStorage(self.hass, "synthetic_homekit_fans")
        await storage.async_initialize()
        driver = Mock(loader=Loader(), iid_storage=storage)
        for i, (entity, initial_percentage) in enumerate(((floor, 20), (exact, 0)), 2):
            with self.subTest(kind=entity.kind):
                bridge = Fan(self.hass, driver, entity.name, entity.entity_id, i, {})
                service = bridge.get_service(SERV_FANV2)
                self.assertIsNotNone(service)
                self.assertEqual(bridge.char_target_fan_state.value, 1)
                self.assertEqual(bridge.char_active.value, 1)
                self.assertEqual(bridge.char_speed.value, initial_percentage or 1)

                bridge.set_chars({CHAR_ACTIVE: 1, CHAR_ROTATION_SPEED: 37,
                                  CHAR_TARGET_FAN_STATE: 0})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode), (37, None))

                bridge.set_chars({CHAR_ACTIVE: 0, CHAR_ROTATION_SPEED: 99,
                                  CHAR_TARGET_FAN_STATE: 1})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode, entity.is_on),
                                 (0, None, False))
                bridge.set_chars({CHAR_ACTIVE: 1})
                await self.hass.async_block_till_done()
                self.assertEqual(entity.percentage, 37)
                bridge.set_chars({CHAR_ROTATION_SPEED: 92, CHAR_TARGET_FAN_STATE: 1})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode),
                                 (initial_percentage, AUTO))
                self.assertEqual(self.manager.last_manual_value("alpha", entity.kind), 37)

                bridge.set_chars({CHAR_TARGET_FAN_STATE: 0})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode),
                                 (37, None))


                # Independent writes that happen before the event loop yields
                # are still ordered, rather than dropped as a combined write.
                bridge.set_chars({CHAR_ACTIVE: 0})
                bridge.set_chars({CHAR_ACTIVE: 1})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode), (37, None))
                bridge.set_chars({CHAR_TARGET_FAN_STATE: 1})
                bridge.set_chars({CHAR_ROTATION_SPEED: 48})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode), (48, None))
                bridge.set_chars({CHAR_ACTIVE: 1, CHAR_ROTATION_SPEED: 0,
                                  CHAR_TARGET_FAN_STATE: 0})
                await self.hass.async_block_till_done()
                self.assertEqual((entity.percentage, entity.preset_mode, entity.is_on),
                                 (0, None, False))
