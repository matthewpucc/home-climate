"""Saved circulation fan cleanup must not hold the critical climate lease."""
import asyncio
import unittest
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as controls_fixtures


class FanRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = controls_fixtures.NativeControlTests.runtime
    claim = controls_fixtures.NativeControlTests.claim
    settle_watchdog = controls_fixtures.NativeControlTests.settle_watchdog

    async def saved(self, entry_id, devices=(), pending=()):
        store = Store(self.hass, 1, f"house_climate.{entry_id}.ownership")
        data = {"devices": list(devices)}
        if pending:
            data["pending_fans"] = list(pending)
        await store.async_save(data)
        return store

    async def test_old_mapping_offline_fan_does_not_block_new_climate_control(self):
        old_head, old_fan = "climate.previous_head", "fan.previous_purifier"
        self.hass.states.async_set(old_head, "heat", {})
        self.hass.states.async_set(old_fan, "unavailable", {})
        store = await self.saved("old_mapping", [old_head, old_fan])

        async def offline(call):
            if call.data["entity_id"] == old_fan:
                self.calls.append((call.domain, call.service, dict(call.data)))
                raise HomeAssistantError("old fan offline")
            await self.handle(call)

        self.hass.services.async_register("fan", "turn_off", offline)
        runtime, switch = await self.runtime(entry_id="old_mapping")
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(self.hass.states.get(old_head).state, "off")
        self.assertFalse(runtime.controls.owned)
        self.assertFalse(runtime.controls.recovering)
        self.assertEqual(runtime.controls.pending_fans, (old_fan,))
        self.assertEqual(await store.async_load(),
                         {"devices": [], "pending_fans": [old_fan]})
        self.assertIn("Turn on Controller enabled", runtime.controls.hint)
        runtime.async_start()
        await switch.async_turn_on()
        await self.until(lambda: self.hass.states.get(self.cfg.heads["guest"]).state == "cool")
        self.assertTrue(switch.is_on)
        self.assertEqual(runtime.fault_count, 0)
        self.assertIn(old_fan, runtime.controls.pending_fans)
        saved = await store.async_load()
        self.assertEqual(set(saved["devices"]),
                         set(runtime.config.climate_entities) | set(runtime.config.fans.values()))
        self.assertEqual(saved["pending_fans"], [old_fan])
        self.assertFalse(any(s.entity_id.startswith(("input_", "automation.", "binary_sensor."))
                             for s in self.hass.states.async_all()))
        self.hass.services.async_register("fan", "turn_off", self.handle)

    async def test_pending_fans_remain_durable_until_off_is_observed(self):
        old_fan = "fan.previous_purifier"
        self.hass.states.async_set(old_fan, "unavailable", {})
        store = await self.saved("pending_ack", pending=[old_fan])

        async def lost_ack(call):
            if call.data["entity_id"] == old_fan:
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("fan", "turn_off", lost_ack)
        runtime, _ = await self.runtime(entry_id="pending_ack")
        await self.settle_watchdog(runtime.controls)
        before = len([c for c in self.calls if c[2]["entity_id"] == old_fan])
        await runtime.controls.async_check()
        self.assertGreater(len([c for c in self.calls if c[2]["entity_id"] == old_fan]), before)
        self.assertEqual(runtime.controls.pending_fans, (old_fan,))
        self.assertEqual(await store.async_load(),
                         {"devices": [], "pending_fans": [old_fan]})
        self.hass.states.async_set(old_fan, "off", {})
        await self.settle_watchdog(runtime.controls)
        self.assertFalse(runtime.controls.pending_fans)
        self.assertEqual(await store.async_load(), {"devices": []})

    async def test_critical_off_failure_still_blocks_enable_and_unload(self):
        old_head, old_fan = "climate.previous_head", "fan.previous_purifier"
        self.hass.states.async_set(old_head, "heat", {})
        self.hass.states.async_set(old_fan, "unavailable", {})
        await self.saved("critical_off", [old_head, old_fan])

        async def lost_ack(call):
            if call.data["entity_id"] in (old_head, old_fan):
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", lost_ack)
        self.hass.services.async_register("fan", "turn_off", lost_ack)
        runtime, switch = await self.runtime(entry_id="critical_off")
        await self.settle_watchdog(runtime.controls)
        self.assertTrue(runtime.controls.recovering)
        self.assertIn(old_head, runtime.controls.owned)
        self.assertFalse(runtime.controls.pending_fans)
        with self.assertRaisesRegex(HomeAssistantError, "Waiting for previously owned"):
            await switch.async_turn_on()
        self.assertFalse(await runtime.async_stop())
        self.assertTrue(runtime.controls.active)
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        await runtime.controls.async_check()
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(runtime.controls.pending_fans, (old_fan,))
        self.assertTrue(await runtime.async_stop())
        self.assertFalse(runtime.controls.active)

    async def test_claim_waits_for_cleanup_and_atomically_reclaims_current_fan(self):
        fan = self.cfg.fans["office"]
        old = self.hass.states.get(fan)
        self.hass.states.async_set(fan, "on", {**old.attributes, "percentage": 60})
        store = await self.saved("reclaim", pending=[fan])
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed(call):
            if call.data["entity_id"] == fan:
                self.calls.append((call.domain, call.service, dict(call.data)))
                started.set()
                await finish.wait()
                raise HomeAssistantError("cleanup fan offline")
            await self.handle(call)

        self.hass.services.async_register("fan", "turn_off", delayed)
        runtime, _ = await self.runtime(entry_id="reclaim")
        await asyncio.wait_for(started.wait(), 2)
        await runtime.controls.async_set_enabled(True)
        claim = asyncio.create_task(asyncio.to_thread(
            runtime.api.service, "input_boolean", "turn_on",
            entity_id=runtime.config.entity("heater_owned")))
        await asyncio.sleep(.02)
        self.assertFalse(claim.done())
        self.assertFalse(runtime.controls.owned)
        finish.set()
        await asyncio.wait_for(claim, 2)
        self.assertFalse(runtime.controls.pending_fans)
        saved = await store.async_load()
        self.assertIn(fan, saved["devices"])
        self.assertNotIn(fan, saved.get("pending_fans", []))
        before = len([c for c in self.calls if c[1] == "turn_off" and c[2]["entity_id"] == fan])
        await asyncio.to_thread(runtime.api.service, "fan", "turn_on",
                                entity_id=fan, percentage=60)
        await runtime.controls.async_check()
        self.assertEqual(len([c for c in self.calls if c[1] == "turn_off"
                              and c[2]["entity_id"] == fan]), before)
        self.assertEqual(self.hass.states.get(fan).state, "on")
        self.hass.services.async_register("fan", "turn_off", self.handle)

    async def test_structural_fault_recovery_hides_virtual_lease_and_prevents_claim_cascade(self):
        runtime, switch = await self.runtime(entry_id="fault_cascade")
        runtime.async_start()
        await switch.async_turn_on()
        guest = self.cfg.heads["guest"]
        await self.until(lambda: self.hass.states.get(guest).state == "cool")
        attrs = dict(self.hass.states.get(guest).attributes)
        heater = self.cfg.heater_entity
        heater_state = self.hass.states.get(heater)
        heater_attrs = dict(heater_state.attributes)

        async def lost_off(call):
            if call.data["entity_id"] == guest:
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", lost_off)
        with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
            # A verified high thermostat target outside the device range is
            # structural; a cooling-only head is a valid local capability.
            self.hass.states.async_set(heater, heater_state.state,
                                       {**heater_attrs, "max_temp": 80})
            await self.until(lambda: runtime.controls.recovering and runtime.fault_count > 0)
        faults = runtime.fault_count
        observed = runtime.controls.overlay({})
        self.assertEqual(observed[runtime.config.entity("enabled")]["state"], "off")
        self.assertEqual(observed[runtime.config.entity("heater_owned")]["state"], "off")
        await asyncio.sleep(.15)
        self.assertEqual(runtime.fault_count, faults)
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.hass.states.async_set(heater, "off", heater_attrs)
        self.hass.states.async_set(guest, "off", attrs)
        await runtime.controls.async_check()
        await self.until(lambda: self.hass.states.get(guest).state == "cool")
        self.assertEqual(runtime.fault_count, faults)

    async def test_pending_only_unload_detaches_callbacks_and_resumes_next_load(self):
        fan = "fan.previous_purifier"
        self.hass.states.async_set(fan, "unavailable", {})
        store = await self.saved("pending_unload", pending=[fan])

        async def offline(call):
            if call.data["entity_id"] == fan:
                self.calls.append((call.domain, call.service, dict(call.data)))
                raise HomeAssistantError("old fan offline")
            await self.handle(call)

        self.hass.services.async_register("fan", "turn_off", offline)
        runtime, _ = await self.runtime(entry_id="pending_unload")
        await self.settle_watchdog(runtime.controls)
        self.assertTrue(await runtime.async_stop())
        self.assertFalse(runtime.controls.active)
        self.assertFalse(runtime.controls._unsubscribers)
        self.assertEqual(await store.async_load(),
                         {"devices": [], "pending_fans": [fan]})
        before = len(self.calls)
        self.hass.states.async_set(fan, "on", {})
        await asyncio.sleep(.02)
        self.assertEqual(len(self.calls), before)
        # Repeated lifecycle cleanup cannot revive a detached watchdog.
        self.assertTrue(await runtime.async_stop())
        self.assertEqual(len(self.calls), before)
        reloaded, _ = await self.runtime(entry_id="pending_unload")
        await self.settle_watchdog(reloaded.controls)
        self.assertGreater(len(self.calls), before)
        self.assertIn(fan, reloaded.controls.pending_fans)
        self.hass.services.async_register("fan", "turn_off", self.handle)
        await reloaded.controls.async_check()
        self.assertFalse(reloaded.controls.pending_fans)

    async def test_failed_recovery_save_preserves_critical_lease(self):
        head, fan = "climate.previous_head", "fan.previous_purifier"
        self.hass.states.async_set(head, "off", {})
        self.hass.states.async_set(fan, "unavailable", {})
        await self.saved("recovery_save", [head, fan])

        async def lost_ack(call):
            if call.data["entity_id"] != fan:
                await self.handle(call)

        self.hass.services.async_register("fan", "turn_off", lost_ack)
        runtime, switch = await self.runtime(entry_id="recovery_save")
        with patch.object(runtime.controls.store, "async_save", side_effect=OSError("disk full")):
            with self.assertLogs("custom_components.house_climate.controls", level="ERROR"):
                await self.settle_watchdog(runtime.controls)
            self.assertIn(head, runtime.controls.owned)
            self.assertTrue(runtime.controls.recovering)
            with self.assertRaises(HomeAssistantError):
                await switch.async_turn_on()
        await runtime.controls.async_check()
        self.assertFalse(runtime.controls.owned)
        self.assertEqual(runtime.controls.pending_fans, (fan,))
        self.hass.services.async_register("fan", "turn_off", self.handle)
