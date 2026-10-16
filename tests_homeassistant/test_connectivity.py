"""Temporary climate connectivity loss must not cycle healthy devices."""
import asyncio
from dataclasses import replace
import unittest
from types import SimpleNamespace

from homeassistant.exceptions import HomeAssistantError

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.sensor import CurrentIssues
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as control_fixtures


class ConnectivityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = control_fixtures.NativeControlTests.runtime

    def temperatures(self, room, value):
        for probe in self.cfg.zones[room].probes:
            old = self.hass.states.get(probe.entity_id)
            self.hass.states.async_set(probe.entity_id, str(value), dict(old.attributes))

    def activations(self, entity):
        return [call for call in self.calls
                if call[1] == "set_temperature" and call[2]["entity_id"] == entity]

    async def disconnect(self, entity, *, raise_error=False):
        old = self.hass.states.get(entity)
        attrs = dict(old.attributes)
        self.hass.states.async_set(entity, "unavailable", attrs)

        async def unavailable(call):
            if call.data["entity_id"] == entity:
                self.calls.append((call.domain, call.service, dict(call.data)))
                if raise_error:
                    raise HomeAssistantError("temporary Wi-Fi disconnect")
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", unavailable)
        return attrs

    async def test_intermittent_head_does_not_cycle_healthy_running_head(self):
        runtime, switch = await self.runtime()
        runtime.async_start()
        await switch.async_turn_on()
        guest, living = self.cfg.heads["guest"], self.cfg.heads["living"]
        await self.until(lambda: self.hass.states.get(guest).state == "cool")
        count = len(self.activations(guest))
        attrs = await self.disconnect(living)
        await self.until(lambda: living in runtime.status.get("device_issues", {}))
        await asyncio.sleep(.12)
        self.assertEqual(runtime.status["event"], "live")
        self.assertEqual(self.hass.states.get(guest).state, "cool")
        self.assertEqual(len(self.activations(guest)), count)
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)
        self.assertIn(living, runtime.controls.owned)
        self.assertIn(living, CurrentIssues(SimpleNamespace(
            entry_id="native", runtime_data=runtime)).native_value)
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.hass.states.async_set(living, "off", attrs)
        await self.until(lambda: living not in runtime.status.get("device_issues", {}))
        self.assertEqual(len(self.activations(guest)), count)
        self.assertEqual(runtime.fault_count, 0)

    async def test_offline_head_at_startup_waits_without_fault_until_reconnected(self):
        living = self.cfg.heads["living"]
        attrs = await self.disconnect(living)
        runtime, switch = await self.runtime()
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: living in runtime.status.get("device_issues", {}))
        await asyncio.sleep(.12)
        self.assertFalse(any(self.activations(entity) for entity in self.cfg.heads.values()))
        self.assertEqual(runtime.fault_count, 0)
        self.assertTrue(switch.is_on)
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.hass.states.async_set(living, "off", attrs)
        await self.until(lambda: self.hass.states.get(self.cfg.heads["guest"]).state == "cool")
        self.assertEqual(runtime.fault_count, 0)

    async def test_mode_change_waits_for_offline_head_off_before_heating(self):
        runtime, switch = await self.runtime()
        runtime.async_start()
        await switch.async_turn_on()
        guest, living = self.cfg.heads["guest"], self.cfg.heads["living"]
        await self.until(lambda: self.hass.states.get(guest).state == "cool")
        attrs = await self.disconnect(living)
        await self.until(lambda: living in runtime.status.get("device_issues", {}))
        self.temperatures("primary", 64)
        await self.until(lambda: runtime.status.get("mode") == "heat"
                         and self.hass.states.get(guest).state == "off")
        await asyncio.sleep(.12)
        head_calls = [call for call in self.calls if call[1] == "set_temperature"
                      and call[2]["entity_id"] in self.cfg.heads.values()]
        self.assertFalse(any(call[2]["hvac_mode"] == "heat" for call in head_calls))
        self.assertEqual(runtime.fault_count, 0)
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.hass.states.async_set(living, "off", attrs)
        await self.until(lambda: self.hass.states.get(self.cfg.heads["primary"]).state == "heat")
        self.assertEqual(runtime.fault_count, 0)

    async def test_auxiliary_disconnect_retries_off_without_stopping_shared_cooling(self):
        runtime, switch = await self.runtime()
        runtime.async_start()
        await switch.async_turn_on()
        guest, heater = self.cfg.heads["guest"], self.cfg.heater_entity
        await self.until(lambda: self.hass.states.get(guest).state == "cool"
                         and self.hass.states.get(heater).state == "heat")
        count = len(self.activations(guest))
        heat_count = len(self.activations(heater))
        attrs = await self.disconnect(heater, raise_error=True)
        self.temperatures("living", 71)
        await self.until(lambda: heater in runtime.status.get("device_issues", {}))
        await asyncio.sleep(.12)
        self.assertEqual(self.hass.states.get(guest).state, "cool")
        self.assertEqual(len(self.activations(guest)), count)
        self.assertEqual(len(self.activations(heater)), heat_count)
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)
        self.assertIn(heater, runtime.controls.owned)
        self.assertTrue(any(call[1] == "set_hvac_mode" and call[2]["entity_id"] == heater
                            and call[2]["hvac_mode"] == "off" for call in self.calls))
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.hass.states.async_set(heater, "off", attrs)
        await self.until(lambda: heater not in runtime.status.get("device_issues", {}))
        await runtime.controls.async_check()
        self.assertIsNone(runtime.controls.problem)
        self.assertEqual(len(self.activations(heater)), heat_count)
        self.assertEqual(len(self.activations(guest)), count)

    async def test_activation_ack_timeout_recovers_one_head_without_cycling_another(self):
        self.temperatures("office", 74)
        cfg = replace(Config.from_dict(self.cfg.to_dict()), command_timeout_seconds=.12)
        guest, bonus = cfg.heads["guest"], cfg.heads["bonus"]
        acknowledge = False

        async def lost_ack(call):
            if call.data["entity_id"] == guest and not acknowledge:
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_temperature", lost_ack)
        runtime, switch = await self.runtime(cfg=cfg)
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: guest in runtime.status.get("device_issues", {}))
        self.assertEqual(self.hass.states.get(bonus).state, "cool")
        count = len(self.activations(bonus))
        acknowledge = True
        await self.until(lambda: self.hass.states.get(guest).state == "cool"
                         and guest not in runtime.status.get("device_issues", {}))
        self.assertEqual(len(self.activations(bonus)), count)
        self.assertEqual(runtime.fault_count, 0)
        first = next(i for i, call in enumerate(self.calls)
                     if call[1] == "set_temperature" and call[2]["entity_id"] == guest)
        later = self.calls[first + 1:]
        off_index = next(i for i, call in enumerate(later)
                         if call[1] == "set_hvac_mode" and call[2]["entity_id"] == guest
                         and call[2]["hvac_mode"] == "off")
        activation_index = next(i for i, call in enumerate(later)
                                if call[1] == "set_temperature" and call[2]["entity_id"] == guest)
        self.assertLess(off_index, activation_index)
