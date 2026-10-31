"""Auxiliary startup against a temperature-only real HA climate provider.

The provider follows the observed public 0.38.0 contract: programming a target
also selects its heating parameter, but only explicit Heat powers the fan on.
No upstream code, device connection or private installation data is required.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import logging
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.components.climate.services import async_setup_services
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.executor import Executor
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.controller.policy import Plan
from custom_components.house_climate.runtime import NativeAPI
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.power_guard import HeaterPowerGuard
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class TemperatureOnlyClimate(ClimateEntity):
    """A provider for which set_temperature's hvac_mode kwarg is not power-On."""

    _attr_should_poll = False
    _attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE
                                | ClimateEntityFeature.TURN_ON
                                | ClimateEntityFeature.TURN_OFF)
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 1
    _attr_max_temp = 37
    _attr_target_temperature_step = 1
    _attr_current_temperature = 20
    def __init__(self, entity_id, *, head=False):
        self.entity_id = entity_id
        self._attr_name = "Synthetic target-only heater"
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]
        if head:
            self._attr_hvac_modes.append(HVACMode.COOL)
        self._attr_target_temperature = 22
        self.fan_power = False
        self.heating_parameter = False
        self.cooling_parameter = False
        self.commands = []
        self.reject_target = False
        self.reject_heat = False
        self.ignore_heat = False
        self.target_release = None
        self.heat_release = None
        self.target_entered = asyncio.Event()
        self.heat_entered = asyncio.Event()

    @property
    def hvac_mode(self):
        if not self.fan_power:
            return HVACMode.OFF
        if self.heating_parameter:
            return HVACMode.HEAT
        return HVACMode.COOL if self.cooling_parameter else HVACMode.FAN_ONLY

    @property
    def hvac_action(self):
        if not self.fan_power:
            return HVACAction.OFF
        if self.heating_parameter:
            return HVACAction.HEATING
        return HVACAction.COOLING if self.cooling_parameter else HVACAction.FAN

    async def async_set_temperature(self, **kwargs):
        # HA's real service handler converts the configured Fahrenheit value
        # to this provider's native Celsius before invoking this method.
        self.commands.append(("target", kwargs["temperature"], kwargs.get("hvac_mode")))
        self.target_entered.set()
        if self.target_release is not None:
            await self.target_release.wait()
        if self.reject_target:
            raise HomeAssistantError("Synthetic target rejection")
        self._attr_target_temperature = kwargs["temperature"]
        self.heating_parameter = True
        self.cooling_parameter = False
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        self.commands.append(("mode", hvac_mode, self.target_temperature))
        if hvac_mode == HVACMode.HEAT:
            self.heat_entered.set()
            if self.heat_release is not None:
                await self.heat_release.wait()
            if self.reject_heat:
                raise HomeAssistantError("Synthetic Heat rejection")
            if self.ignore_heat:
                return
        self.fan_power = hvac_mode != HVACMode.OFF
        self.heating_parameter = hvac_mode == HVACMode.HEAT
        self.cooling_parameter = hvac_mode == HVACMode.COOL
        self.async_write_ha_state()

    async def async_turn_on(self):
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self):
        await self.async_set_hvac_mode(HVACMode.OFF)


class AuxiliaryStartupNativeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        self.cfg = replace(self.cfg, command_timeout_seconds=1)
        # Replace the old always-apply-mode fixture with actual climate entity
        # services, including HA's conversion and feature validation path.
        for service in ("set_hvac_mode", "set_temperature"):
            self.hass.services.async_remove("climate", service)
        self.component = EntityComponent(logging.getLogger(__name__), "climate", self.hass,
                                         timedelta(seconds=30))
        self.hass.data[DATA_COMPONENT] = self.component
        async_setup_services(self.hass)
        self.entities = {}
        for entity in self.cfg.climate_entities:
            self.hass.states.async_remove(entity)
            self.entities[entity] = TemperatureOnlyClimate(entity, head=entity != self.cfg.heater_entity)
        await self.component.async_add_entities(list(self.entities.values()))
        await self.hass.async_block_till_done()
        self.heater = self.entities[self.cfg.heater_entity]
        self.api = NativeAPI(self.hass, True, threading.Event(), self.cfg)
        self.guard = SimpleNamespace(blocked=False, check_activation=Mock())
        self.api.power_guard = self.guard
        self.executor = Executor(self.cfg, self.api, "°F")
        self.plan = Plan(heater_on=True)
        self.started = time.monotonic()

    async def asyncTearDown(self):
        for entity in self.entities.values():
            for release in (entity.target_release, entity.heat_release):
                if release is not None:
                    release.set()
        for runtime in self.runtimes:
            await runtime.async_stop()
        self.runtimes.clear()
        await self.component._async_reset()
        await fixtures.NativeTests.asyncTearDown(self)

    async def snapshot(self):
        return await asyncio.to_thread(self.api.states)

    async def tick(self, seconds=0, *, plan=None):
        states = await self.snapshot()
        await asyncio.to_thread(self.executor.auxiliary, self.plan if plan is None else plan,
                                states, self.started + seconds)
        await self.hass.async_block_till_done()

    async def controlled(self):
        self.cfg = Config.from_dict(self.cfg.to_dict())
        self.controller, self.switch = await self.runtime(cfg=self.cfg, entry_id="auxiliary_startup")
        await self.claim(self.controller)
        await self.settle_watchdog(self.controller.controls)
        self.api = self.controller.api
        self.api.power_guard = self.guard
        self.executor = Executor(self.cfg, self.api, "°F")
        self.heater.commands.clear()
        self.started = time.monotonic()

    async def queued_heat(self, change, *, service="set_hvac_mode", seconds=.05):
        """Hold the HA job before actual dispatch, not inside the device call."""
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api._async_service_job

        async def held(*args, **kwargs):
            if args[2] == service and args[3].get("hvac_mode") == "heat":
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        task = None
        with patch.object(self.api, "_async_service_job", side_effect=held):
            try:
                task = asyncio.create_task(self.tick(seconds))
                await asyncio.wait_for(entered.wait(), 1)
                changed = change()
                if asyncio.iscoroutine(changed):
                    await changed
                release.set()
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_real_temperature_service_does_not_supply_explicit_power_on(self):
        await asyncio.to_thread(self.api.service, "climate", "set_temperature",
                                entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86)
        observed = self.hass.states.get(self.cfg.heater_entity)
        self.assertEqual(observed.state, "off")
        self.assertEqual(observed.attributes["temperature"], 86)
        self.assertEqual(self.heater.target_temperature, 30)
        self.assertFalse(self.heater.fan_power)
        self.assertEqual([command[0] for command in self.heater.commands], ["target"])

    async def test_initially_off_programs_target_then_explicit_heat_with_real_unit_conversion(self):
        for seconds in (0, .05, .1, .15):
            await self.tick(seconds)
        self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "heat")
        self.assertEqual(self.heater.target_temperature, 30)
        self.assertEqual(self.heater.commands[:2], [
            ("target", 30, "heat"), ("mode", HVACMode.HEAT, 30)])
        self.assertEqual(len(self.heater.commands), 2)
        self.assertNotIn(self.cfg.heater_entity, self.executor.pending)
        self.assertNotIn(self.cfg.heater_entity, self.executor.recoveries)

    async def test_cached_matching_target_while_off_is_reprogrammed_before_heat(self):
        self.heater._attr_target_temperature = 30
        self.heater.async_write_ha_state()
        await self.tick()
        self.assertEqual(self.heater.commands, [("target", 30, "heat")])
        self.assertFalse(self.heater.fan_power)
        for seconds in (.05, .1, .15):
            await self.tick(seconds)
        self.assertEqual(self.heater.commands, [
            ("target", 30, "heat"), ("mode", HVACMode.HEAT, 30)])

    async def test_already_heat_preserves_running_mode_without_power_cycle(self):
        self.heater.fan_power = self.heater.heating_parameter = True
        self.heater.async_write_ha_state()
        for seconds in (0, .05, .1):
            await self.tick(seconds)
        self.assertEqual(self.heater.commands, [("target", 30, "heat")])
        self.assertTrue(self.heater.fan_power)
        self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "heat")

    async def test_both_positive_stages_pass_the_fresh_dispatch_guard(self):
        for seconds in (0, .05, .1, .15):
            await self.tick(seconds)
        checked = [call.args[1] for call in self.guard.check_activation.call_args_list]
        self.assertEqual([body["hvac_mode"] for body in checked], ["heat", "heat"])
        self.assertIn("temperature", checked[0])
        self.assertNotIn("temperature", checked[1])

    async def test_stale_guard_before_target_prevents_parameter_side_effects(self):
        self.guard.check_activation.side_effect = HAActivationDeferred("Synthetic stale electrical reading")
        await self.tick()
        self.assertEqual(self.heater.commands, [])
        self.assertFalse(self.heater.fan_power)

    async def test_guard_revocation_at_queued_heat_cannot_power_on(self):
        await self.tick()
        self.assertEqual(self.heater.commands, [("target", 30, "heat")])
        await self.queued_heat(lambda: setattr(self.guard.check_activation, "side_effect",
                                              HAActivationDeferred("Synthetic stale electrical reading")))
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))
        self.assertFalse(self.api.service_jobs({self.cfg.heater_entity}))

    def use_real_output_policy(self):
        bindings = replace(example_bindings().power, heater=self.cfg.heater_entity)
        wall = [round(time.time(), 3)]
        for entity, value, unit in (
            (bindings.circuit_power, 140, "W"), (bindings.input_power, 130, "W"),
            (bindings.output_power, 2500, "W"), (bindings.battery, 100, "%"),
            (bindings.heater_current, .05, "A"),
            (bindings.wall_phase_a_voltage, 124, "V"),
            (bindings.wall_phase_b_voltage, 123, "V"),
        ):
            self.hass.states.async_set(entity, str(value), {"unit_of_measurement": unit},
                                       timestamp=wall[0])
        self.hass.states.async_set(bindings.heater_switch, "on", timestamp=wall[0])
        # Real policy at NativeAPI dispatch; no background guard actions needed.
        self.guard = HeaterPowerGuard(self.hass, bindings, live=False, entry_id="queued-output",
                                      notify=lambda: None, wall_clock=lambda: wall[0])
        self.api.power_guard = self.guard
        return bindings, wall

    async def test_qualifying_high_output_allows_both_real_startup_stages(self):
        self.use_real_output_policy()
        try:
            for seconds in (0, .05, .1, .15):
                await self.tick(seconds)
            self.assertTrue(self.heater.fan_power)
            self.assertTrue(self.guard.diagnostics["output_limits_bypassed"])
        finally:
            await self.guard.async_close()

    async def revoked_output_policy(self, case, *, service="set_hvac_mode"):
        bindings, wall = self.use_real_output_policy()
        def change():
            if case == "stale":
                wall[0] += 181
                for entity in self.guard.entity_ids - {bindings.circuit_power}:
                    state = self.hass.states.get(entity)
                    self.hass.states.async_set(entity, state.state, state.attributes,
                                               force_update=True, timestamp=wall[0])
            else:
                entity, value, unit = ((bindings.battery, 40, "%") if case == "reserve"
                                       else (bindings.circuit_power, 0, "W"))
                self.hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})
        try:
            if service == "set_hvac_mode":
                await self.tick()
                self.assertEqual(self.heater.commands, [("target", 30, "heat")])
            await self.queued_heat(change, service=service)
            self.assertFalse(self.heater.fan_power)
            self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                                 for command in self.heater.commands))
            if service == "set_temperature":
                self.assertEqual(self.heater.commands, [])
            self.assertTrue(self.guard.blocked)
        finally:
            await self.guard.async_close()

    async def test_circuit_expiry_at_queued_heat_blocks_actual_dispatch(self):
        await self.revoked_output_policy("stale")

    async def test_circuit_zero_at_queued_target_blocks_actual_dispatch(self):
        await self.revoked_output_policy("zero", service="set_temperature")

    async def test_battery_floor_at_queued_heat_blocks_actual_dispatch(self):
        await self.revoked_output_policy("reserve")

    async def test_off_after_deferred_heat_resets_latent_heating_parameter(self):
        await self.tick()
        await self.queued_heat(lambda: setattr(self.guard.check_activation, "side_effect",
                                              HAActivationDeferred("Synthetic stale electrical reading")))
        self.assertFalse(self.heater.fan_power)
        self.assertTrue(self.heater.heating_parameter)
        # The target command already selected the device's heating parameter.
        # A cached public Off state cannot replace an actual cleanup Off call.
        for seconds in (.1, .15):
            await self.tick(seconds, plan=Plan())
        self.assertIn(("mode", HVACMode.OFF, 30), self.heater.commands)
        self.assertFalse(self.heater.heating_parameter)
        self.assertFalse(self.heater.fan_power)

    async def test_controller_disable_at_queued_heat_revokes_old_ownership(self):
        await self.controlled()
        await self.tick()
        await self.queued_heat(lambda: self.controller.controls.async_set_enabled(False))
        self.assertFalse(self.controller.controls.enabled)
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))
        self.assertFalse(self.api.service_jobs({self.cfg.heater_entity}))

    async def test_runtime_stop_at_queued_heat_prevents_positive_dispatch(self):
        await self.controlled()
        await self.tick()
        await self.queued_heat(self.api.stop_event.set)
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))

    async def test_probe_revocation_at_queued_target_prevents_heat_parameter_side_effects(self):
        await self.controlled()
        def unavailable_probes():
            for probe in self.cfg.zones[self.cfg.auxiliary_room].probes:
                self.hass.states.async_set(probe.entity_id, "unavailable")
        await self.queued_heat(unavailable_probes, service="set_temperature", seconds=0)
        self.assertFalse(any(command[0] == "target" for command in self.heater.commands))
        self.assertFalse(self.heater.fan_power)

    async def test_probe_revocation_at_queued_heat_cannot_complete_staged_start(self):
        await self.controlled()
        await self.tick()
        def hot_probe():
            probe = self.cfg.zones[self.cfg.auxiliary_room].probes[0]
            self.hass.states.async_set(probe.entity_id, "75", {"unit_of_measurement": "°F"})
        await self.queued_heat(hot_probe)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))
        self.assertFalse(self.heater.fan_power)

    async def test_superseding_off_between_stages_never_sends_heat(self):
        await self.tick()
        for seconds in (.05, .1):
            await self.tick(seconds, plan=Plan())
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))
        self.assertIn(("mode", HVACMode.OFF, 30), self.heater.commands)

    async def test_rejected_target_is_not_followed_by_heat(self):
        self.heater.reject_target = True
        await self.tick()
        await self.tick(.05, plan=Plan())
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(any(command[:2] == ("mode", HVACMode.HEAT)
                             for command in self.heater.commands))
        self.assertTrue(any(command[:2] == ("mode", HVACMode.OFF)
                            for command in self.heater.commands))

    async def test_rejected_heat_runs_explicit_off_cleanup(self):
        await self.tick()
        self.heater.reject_heat = True
        await self.tick(.05)
        await self.tick(.1, plan=Plan())
        self.assertEqual([command[0] for command in self.heater.commands], ["target", "mode", "mode"])
        self.assertEqual(self.heater.commands[-1][1], HVACMode.OFF)
        self.assertFalse(self.heater.fan_power)

    async def test_explicit_native_deadline_rejects_queued_heat_before_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api._async_service_job
        clock = SimpleNamespace(monotonic=Mock(return_value=100.0))

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        request = None
        with (patch("custom_components.house_climate.runtime.time", clock),
              patch.object(self.api, "_async_service_job", side_effect=held)):
            try:
                request = asyncio.create_task(asyncio.to_thread(
                    self.api.service_before, "climate", "set_hvac_mode", deadline=100.5,
                    entity_id=self.cfg.heater_entity, hvac_mode="heat"))
                await asyncio.wait_for(entered.wait(), 1)
                clock.monotonic.return_value = 100.6
                release.set()
                with self.assertRaises(HAActivationDeferred):
                    await asyncio.wait_for(request, 2)
            finally:
                release.set()
                if request is not None and not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(self.heater.commands, [])
        self.assertFalse(self.heater.fan_power)
        self.assertFalse(self.api.service_jobs({self.cfg.heater_entity}))

    async def test_bounded_api_rejects_missing_or_invalid_positive_deadline(self):
        for deadline in (None, True, float("nan"), float("inf"), "soon", time.monotonic() - 1):
            with self.subTest(deadline=deadline), self.assertRaises(HAActivationDeferred):
                await asyncio.to_thread(self.api.service_before,
                    "climate", "set_hvac_mode", deadline=deadline,
                    entity_id=self.cfg.heater_entity, hvac_mode="heat")
        self.assertEqual(self.heater.commands, [])
        self.assertFalse(self.heater.fan_power)

    async def test_late_native_heat_after_off_is_compensated_by_another_off(self):
        await self.controlled()
        self.heater.heat_release = asyncio.Event()
        request = asyncio.create_task(asyncio.to_thread(
            self.api.service_before, "climate", "set_hvac_mode", deadline=time.monotonic() + .05,
            entity_id=self.cfg.heater_entity, hvac_mode="heat"))
        try:
            await asyncio.wait_for(self.heater.heat_entered.wait(), 1)
            with self.assertRaises(HAError):
                await asyncio.wait_for(request, 2)
            self.assertTrue(self.api.service_jobs({self.cfg.heater_entity}))
            await self.controller.controls.async_set_enabled(False)
            self.assertFalse(self.controller.controls.enabled)
            self.assertTrue(self.controller.controls.owned)
            before_late = len(self.heater.commands)
            self.heater.heat_release.set()
            await self.until(lambda: not self.api.service_jobs({self.cfg.heater_entity}))
            await self.settle_watchdog(self.controller.controls)
            await self.until(lambda: not self.heater.fan_power)
            self.assertTrue(any(command[:2] == ("mode", HVACMode.OFF)
                                for command in self.heater.commands[before_late:]))
        finally:
            self.heater.heat_release.set()
            if not request.done():
                await asyncio.gather(request, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
