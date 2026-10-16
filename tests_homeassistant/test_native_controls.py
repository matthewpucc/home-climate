"""Native control requires no helper package and keeps shutdown ownership durable."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.runtime import ControllerRuntime
from custom_components.house_climate.sensor import Overview
from custom_components.house_climate.switch import ControllerEnabled, async_setup_entry
from tests_homeassistant import test_native as fixtures


class NativeControlTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until

    async def runtime(self, live=True, cfg=None, entry_id="native"):
        cfg = cfg or Config.from_dict(self.cfg.to_dict())
        # No legacy YAML helpers or watchdog entities exist in this installation.
        for state in list(self.hass.states.async_all()):
            if state.entity_id.startswith(("input_", "automation.", "binary_sensor.")):
                self.hass.states.async_remove(state.entity_id)
        runtime = ControllerRuntime(self.hass, cfg, live)
        self.runtimes.append(runtime)
        await runtime.async_setup_controls(entry_id)
        runtime.controls.async_start()
        entry = SimpleNamespace(entry_id=entry_id, runtime_data=runtime)
        switch = ControllerEnabled(entry)
        switch.hass = self.hass
        return runtime, switch

    async def claim(self, runtime):
        await runtime.controls.async_set_enabled(True)
        await asyncio.to_thread(runtime.api.service, "input_boolean", "turn_on",
                                entity_id=runtime.config.entity("heater_owned"))

    async def settle_watchdog(self, controls):
        await self.hass.async_block_till_done()
        if controls._task:
            await controls._task

    async def test_device_switch_enables_live_without_helper_entities(self):
        runtime, switch = await self.runtime()
        added = []
        await async_setup_entry(self.hass, SimpleNamespace(entry_id="native", runtime_data=runtime), added.extend)
        self.assertEqual(added[0].name, "Controller enabled")
        self.assertTrue(added[0].entity_registry_enabled_default)
        self.assertIsNone(added[0].entity_category)
        runtime.async_start()
        await self.until(lambda: runtime.status["event"] == "shadow")
        self.assertFalse(switch.is_on)
        self.assertIn("Turn on Controller enabled", Overview(SimpleNamespace(
            entry_id="native", runtime_data=runtime)).native_value)
        self.assertEqual(self.calls, [])
        await switch.async_turn_on()
        await self.until(lambda: self.hass.states.get(self.cfg.heads["guest"]).state == "cool")
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.status["event"], "live")
        self.assertTrue(all(domain in ("climate", "fan") for domain, _, _ in self.calls))
        self.assertFalse(any(s.entity_id.startswith(("input_", "automation.", "binary_sensor."))
                             for s in self.hass.states.async_all()))
        activation = next(i for i, call in enumerate(self.calls) if call[1] == "set_temperature")
        off_before = {call[2]["entity_id"] for call in self.calls[:activation]
                      if call[1] == "set_hvac_mode" and call[2]["hvac_mode"] == "off"}
        self.assertTrue(set(self.cfg.heads.values()) <= off_before)
        await switch.async_turn_off()
        await self.until(lambda: runtime.status["event"] == "shadow" and not runtime.controls.owned)
        self.assertTrue(all(self.hass.states.get(e).state == "off"
                            for e in runtime.config.climate_entities + list(runtime.config.fans.values())))

    async def test_shadow_and_commissioning_gates_are_not_bypassed_by_switch(self):
        runtime, switch = await self.runtime(live=False)
        with self.assertRaisesRegex(HomeAssistantError, "Enable live commands"):
            await switch.async_turn_on()
        self.assertFalse(switch.is_on)
        with self.assertRaisesRegex(HAError, "Shadow mode"):
            await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                    entity_id=self.cfg.heater_entity, hvac_mode="heat")
        self.assertEqual(self.calls, [])

    async def test_claim_is_saved_before_first_actuator_command(self):
        runtime, _ = await self.runtime()
        original = runtime.controls.store.async_save
        saved = []
        async def save(data):
            self.assertEqual(self.calls, [])
            saved.append(data)
            await original(data)
        with patch.object(runtime.controls.store, "async_save", side_effect=save):
            await self.claim(runtime)
        self.assertEqual(set(saved[0]["devices"]),
                         set(runtime.config.climate_entities) | set(runtime.config.fans.values()))
        await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86)
        self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "heat")

    async def test_failed_ownership_save_cannot_activate_equipment(self):
        runtime, _ = await self.runtime()
        with patch.object(runtime.controls.store, "async_save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await self.claim(runtime)
        with self.assertRaises(HAError):
            await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                    entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86)
        self.assertFalse(runtime.controls.enabled)
        self.assertEqual(self.calls, [])

    async def test_disabling_rejects_command_queued_from_an_old_plan(self):
        runtime, switch = await self.runtime()
        await self.claim(runtime)
        await switch.async_turn_off()
        with self.assertRaisesRegex(HAError, "activation blocked"):
            await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                    entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86)
        self.assertFalse(any(call[1] == "set_temperature" for call in self.calls))
        await self.settle_watchdog(runtime.controls)

    async def test_watchdog_stops_owned_devices_without_a_running_worker(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        heater = self.hass.states.get(self.cfg.heater_entity)
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        head = self.hass.states.get(self.cfg.heads["guest"])
        self.hass.states.async_set(head.entity_id, "cool", dict(head.attributes))
        runtime.controls.heartbeat_monotonic -= 121
        await self.settle_watchdog(runtime.controls)
        await runtime.controls.async_check()
        self.assertIsNone(runtime.thread)
        self.assertFalse(runtime.controls.enabled)
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "off")
        self.assertEqual(self.hass.states.get(head.entity_id).state, "off")
        self.assertIn("heartbeat expired", runtime.controls.hint)

    async def test_probe_cutoff_and_missing_probes_block_heat_but_allow_confirmed_fan_only(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        heater = self.hass.states.get(self.cfg.heater_entity)
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        probes = runtime.config.zones[runtime.config.auxiliary_room].probes
        # A single hot external probe must win over the other, cooler probe.
        probe = probes[0]
        self.hass.states.async_set(probe.entity_id, "71", {"unit_of_measurement": "°F"})
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "off")
        with self.assertRaisesRegex(HAError, "External room probes"):
            await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                    entity_id=heater.entity_id, hvac_mode="heat", temperature=86)
        before = len(self.calls)
        self.hass.states.async_set(heater.entity_id, "fan_only", {**heater.attributes, "hvac_action": "fan"})
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "fan_only")
        self.assertEqual(len(self.calls), before)
        for probe in probes:
            self.hass.states.async_set(probe.entity_id, "unavailable")
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "off")

    async def test_watchdog_probe_freshness_and_celsius_are_independent_of_worker(self):
        runtime, _ = await self.runtime()
        now = datetime.now(timezone.utc)
        probes = runtime.config.zones[runtime.config.auxiliary_room].probes
        def readings(value, when):
            return {p.entity_id: {"state": str(value), "last_reported": when.isoformat(),
                    "attributes": {"unit_of_measurement": "°C"}} for p in probes}
        self.assertTrue(runtime.controls.probes_safe(readings(19, now)))
        self.assertFalse(runtime.controls.probes_safe(readings(23, now)))
        self.assertFalse(runtime.controls.probes_safe(readings(
            19, now - timedelta(seconds=runtime.config.sensor_stale_seconds + 1))))

    async def test_restart_recovers_saved_old_mapping_even_in_shadow(self):
        old_head = "climate.previous_head"
        self.hass.states.async_set(old_head, "heat", {})
        store = Store(self.hass, 1, "house_climate.recovery.ownership")
        await store.async_save({"devices": [old_head]})
        runtime, switch = await self.runtime(live=False, entry_id="recovery")
        self.assertFalse(switch.is_on)
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(old_head).state, "off")
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(await store.async_load(), {"devices": []})
        self.assertFalse(runtime.controls.enabled)

    async def test_failed_shutdown_retains_watchdog_and_blocks_unload_until_acknowledged(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        heater = self.hass.states.get(self.cfg.heater_entity)
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        async def offline(_call):
            raise RuntimeError("offline")
        self.hass.services.async_register("climate", "set_hvac_mode", offline)
        with self.assertLogs("custom_components.house_climate.controls", level="ERROR"):
            self.assertFalse(await runtime.async_stop())
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.assertTrue(runtime.controls.active)
        self.assertTrue(runtime.controls.owned)
        with self.assertRaises(HAError):
            await runtime.controls.async_set_enabled(True)
        # On a subsequent unload attempt the device acknowledges off.
        self.assertTrue(await runtime.async_stop())
        self.assertFalse(runtime.controls.active)
        self.assertFalse(runtime.controls.owned)

    async def test_manual_devices_are_not_touched_without_ownership(self):
        runtime, _ = await self.runtime(live=False)
        heater = self.hass.states.get(self.cfg.heater_entity)
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        await self.settle_watchdog(runtime.controls)
        await runtime.controls.async_check()
        await runtime.async_stop()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "heat")

    async def test_inflight_activation_keeps_ownership_until_late_heat_is_stopped(self):
        runtime, switch = await self.runtime()
        await self.claim(runtime)
        started, finish = asyncio.Event(), asyncio.Event()
        async def delayed(call):
            started.set()
            await finish.wait()
            await self.handle(call)
        self.hass.services.async_register("climate", "set_temperature", delayed)
        command = asyncio.create_task(asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86))
        await asyncio.wait_for(started.wait(), 2)
        await switch.async_turn_off()
        if runtime.controls._task:
            await runtime.controls._task
        self.assertTrue(runtime.controls.owned)
        finish.set()
        await command
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "off")
        self.assertFalse(runtime.controls.owned)

    async def test_switch_still_requires_verified_timing_and_heater_setpoint(self):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), equipment_timing_confirmed=False)
        runtime, switch = await self.runtime(cfg=cfg)
        with self.assertRaisesRegex(HomeAssistantError, "Confirm equipment timing"):
            await switch.async_turn_on()
        self.assertFalse(runtime.controls.enabled)
        self.assertEqual(self.calls, [])

    async def test_worker_exit_pauses_control_and_triggers_watchdog_immediately(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        heater = self.hass.states.get(self.cfg.heater_entity)
        self.hass.states.async_set(heater.entity_id, "heat", dict(heater.attributes))
        with patch.object(runtime.api, "request", side_effect=HAError("test worker failure")):
            with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
                runtime.async_start()
                await self.until(lambda: runtime.status["event"] == "worker_failed")
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.enabled)
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(self.hass.states.get(heater.entity_id).state, "off")
