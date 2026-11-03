"""Integration boundaries between purifier floors and bounded fallback retries."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from custom_components.house_climate import handoff
from tests_homeassistant import test_auxiliary_delayed_off as delayed
from tests_homeassistant import test_handoff_retries as retries
from tests_homeassistant import test_purifier_alias_guard as aliases


class PurifierRetryInteractionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retries.HandoffRetryTests.asyncSetUp
    asyncTearDown = retries.HandoffRetryTests.asyncTearDown
    reset = retries.HandoffRetryTests.reset
    report = retries.HandoffRetryTests.report
    handle = retries.HandoffRetryTests.handle
    step = retries.HandoffRetryTests.step
    request = retries.HandoffRetryTests.request
    count = retries.HandoffRetryTests.count
    first = retries.HandoffRetryTests.first

    def floor(self, minimum, fallback=40):
        self.controls.config = replace(self.cfg,
            fan_controls={key: replace(control, minimum_percentage=minimum)
                          for key, control in self.cfg.fan_controls.items()},
            fallback=replace(self.cfg.fallback, fan_percentage=fallback))
        self.default_response = "no_report"
        self.reset((self.fan,))

    async def test_below_floor_or_fallback_report_does_not_consume_retry_without_dispatch(self):
        for minimum, target in ((100, 100), (20, 40)):
            with self.subTest(minimum=minimum):
                self.wall += 200
                self.floor(minimum)
                initial = await self.first(self.fan)
                self.wall = initial["finished_at"] + 15
                self.report(self.fan, percentage=target - 1)
                await self.step()
                self.assertEqual(self.count(self.fan), 2)
                second = self.request(self.fan)
                self.assertTrue(second["dispatched"])
                self.assertEqual(second["first_at"], initial["first_at"])
                self.assertEqual(self.calls[-1][2]["percentage"], target)
                self.wall = second["finished_at"] + 15
                self.report(self.fan, percentage=target - 1)
                await self.step()
                self.assertEqual(self.count(self.fan), 3)
                self.wall += 20
                self.report(self.fan, percentage=target - 1)
                await self.step()
                self.assertEqual(self.count(self.fan), 3)
                self.assertIn(self.fan, self.controls.owned)
                self.wall += 1
                self.report(self.fan, percentage=target)
                await self.step()
                self.assertIsNone(self.controls.handoff)

    async def late_report(self, percentage):
        self.floor(100)
        initial = await self.first(self.fan)
        self.wall = initial["finished_at"] + 15
        self.report(self.fan, percentage=50)
        original = self.controls.store.async_save
        changed = False

        async def save(data):
            nonlocal changed
            await original(data)
            if self.request(self.fan)["attempts"] == 2 and not changed:
                changed = True
                self.wall += 1
                self.report(self.fan, percentage=percentage)

        with patch.object(self.controls.store, "async_save", side_effect=save):
            await self.step()
        self.assertTrue(changed)

    async def test_late_below_floor_report_during_retry_save_still_dispatches(self):
        await self.late_report(99)
        self.assertEqual(self.count(self.fan), 2)
        self.assertTrue(self.request(self.fan)["dispatched"])
        self.assertIn(self.fan, self.controls.owned)

    async def test_late_valid_floor_report_avoids_redundant_retry_and_completes(self):
        await self.late_report(100)
        self.assertEqual(self.count(self.fan), 1)
        await self.step()
        self.assertIsNone(self.controls.handoff)
        self.assertEqual(self.controls.owned, ())

    async def test_coarse_speed_rung_is_preserved_on_each_bounded_retry(self):
        self.floor(20)
        self.report(self.fan, "off", percentage=0, attrs={"percentage_step": 100 / 3})
        await self.step()
        first_at = self.request(self.fan)["first_at"]
        for _ in range(2):
            self.wall = self.request(self.fan)["finished_at"] + 15
            self.report(self.fan, percentage=33, attrs={"percentage_step": 100 / 3})
            await self.step()
        self.assertEqual(self.count(self.fan), 3)
        self.assertEqual([call[2]["percentage"] for call in self.calls], [66, 66, 66])
        self.assertEqual(self.request(self.fan)["first_at"], first_at)


class PurifierRetryIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = aliases.PurifierAliasGuardTests.asyncSetUpBase
    asyncTearDownBase = aliases.PurifierAliasGuardTests.asyncTearDownBase
    asyncSetUp = aliases.PurifierAliasGuardTests.asyncSetUp
    asyncTearDown = aliases.PurifierAliasGuardTests.asyncTearDown
    handle = aliases.PurifierAliasGuardTests.handle
    register = aliases.PurifierAliasGuardTests.register
    config = aliases.PurifierAliasGuardTests.config
    restored_fan_handoff = aliases.PurifierAliasGuardTests.restored_fan_handoff
    fan_calls = aliases.PurifierAliasGuardTests.fan_calls

    async def test_retry_rechecks_saved_floor_identity_after_queueing(self):
        for device_id in (self.heater_device.id, None):
            with self.subTest(device_id=device_id):
                er.async_get(self.hass).async_update_entity(self.fan, device_id=self.purifier_device.id)
                controls, _ = await self.restored_fan_handoff({self.fan: 100}, remove_current_floors=True)
                controls.config = replace(controls.config, head_activation_timeout_seconds=120)
                await handoff.step(controls)
                request = controls.handoff["requests"][self.fan]
                state = self.hass.states.get(self.fan)
                wall = request["finished_at"] + 15
                self.hass.states.async_set(self.fan, "on", {**state.attributes, "percentage": 50},
                                           force_update=True, timestamp=wall)
                before = len(self.fan_calls())
                original = self.hass.async_create_background_task

                def create(coroutine, name, **kwargs):
                    if name == "Home Climate recovery setting":
                        er.async_get(self.hass).async_update_entity(self.fan, device_id=device_id)
                    return original(coroutine, name, **kwargs)

                with patch.object(handoff, "now", return_value=wall), patch.object(
                        self.hass, "async_create_background_task", side_effect=create):
                    await handoff.step(controls)
                self.assertEqual(len(self.fan_calls()), before)
                self.assertEqual(controls.handoff["requests"][self.fan]["attempts"], 2)
                self.assertFalse(controls.handoff["requests"][self.fan].get("dispatched"))
                self.assertIn(self.fan, controls.owned)
                self.assertEqual(controls.service_inflight, 0)
                controls.handoff = None
                controls.owned = ()


class PurifierDelayedOffInteractionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = aliases.PurifierAliasGuardTests.asyncSetUpBase
    asyncTearDownBase = aliases.PurifierAliasGuardTests.asyncTearDownBase
    asyncSetUp = aliases.PurifierAliasGuardTests.asyncSetUp
    asyncTearDown = aliases.PurifierAliasGuardTests.asyncTearDown
    handle = aliases.PurifierAliasGuardTests.handle
    register = aliases.PurifierAliasGuardTests.register
    config = aliases.PurifierAliasGuardTests.config
    runtime = aliases.PurifierAliasGuardTests.runtime
    claim = aliases.PurifierAliasGuardTests.claim
    settle_watchdog = aliases.PurifierAliasGuardTests.settle_watchdog
    until = delayed.AuxiliaryDelayedOffTests.until
    delayed_off = delayed.AuxiliaryDelayedOffTests.delayed_off
    controlled_clock = delayed.AuxiliaryDelayedOffTests.controlled_clock
    set_device_mode = delayed.AuxiliaryDelayedOffTests.set_device_mode

    async def timed_runtime(self):
        return await self.runtime(cfg=replace(self.config(),
            heartbeat_timeout_seconds=300, auxiliary_heartbeat_timeout_seconds=120))

    async def test_purifier_activation_reconciles_delayed_off_without_renewing_auxiliary(self):
        runtime, clock = await self.delayed_off()
        controls = runtime.controls
        authorized = (controls.auxiliary_authorized_at, controls.auxiliary_authorized_wall)
        epoch = controls._auxiliary_stop_epoch
        clock.value = 151
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 221
        before = len(self.calls)
        await asyncio.to_thread(runtime.api.service, "fan", "turn_on",
                                entity_id=self.fan, percentage=20)
        self.assertFalse(controls.auxiliary_heartbeat_blocked)
        self.assertFalse(controls._auxiliary_heat_possible)
        self.assertTrue(controls._auxiliary_heartbeat_off_ack)
        self.assertEqual(controls._auxiliary_stop_epoch, epoch)
        self.assertEqual((controls.auxiliary_authorized_at, controls.auxiliary_authorized_wall), authorized)
        self.assertEqual(self.calls[before:], [("fan", "turn_on", {"entity_id": self.fan, "percentage": 20})])
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")
        self.assertTrue(controls.enabled)
