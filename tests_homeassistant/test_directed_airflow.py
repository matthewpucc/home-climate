"""Real HA registry/services, synthetic devices; never contact physical equipment."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.fan import FanEntityFeature
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate.controller.config import Config, Settings
from custom_components.house_climate.controller.executor import Executor
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.controller.policy import Policy
from custom_components.house_climate.runtime import NativeActivationDispatch
from custom_components.house_climate.runtime import NativeAPI
from custom_components.house_climate.airflow_calibration import async_load_calibrations
from custom_components.house_climate.sensor import ControllerStatus
from tests.test_directed_airflow import directed_config, fixed_config, FAN
from tests.test_rooms import states
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native
from tests_homeassistant import test_purifier_alias_guard as aliases


class NativeAirflowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog
    register = aliases.PurifierAliasGuardTests.register

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.owner = ConfigEntry(version=1, minor_version=1, domain="hass_dyson", entry_id="synthetic_airflow_owner",
            title="Synthetic fan", data={}, options={}, source="user", unique_id=None,
            discovery_keys=MappingProxyType({}), state=ConfigEntryState.LOADED, subentries_data=None)
        self.hass.config_entries._entries[self.owner.entry_id] = self.owner
        raw = directed_config()
        raw.update(timezone="America/Los_Angeles", equipment_timing_confirmed=True,
                   heater_entity="climate.test_heater", auxiliary_room="gamma", heater_setpoint_confirmed=True)
        self.cfg = Config.from_dict(raw)
        self.fan_device = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            identifiers={("hass_dyson", "synthetic_purifier")})
        self.heater_device = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            identifiers={("hass_dyson", "synthetic_heater")})
        self.register(FAN, self.fan_device.id)
        self.register(self.cfg.heater_entity, self.heater_device.id)
        self.data = states(self.cfg, {"alpha": 73, "beta": 72, "gamma": 68}, datetime.now(timezone.utc))
        for entity, state in self.data.items():
            self.hass.states.async_set(entity, state["state"], state.get("attributes", {}))
        self.set_fan(state="on", percentage=20, supported_features=FanEntityFeature(63), percentage_step=10,
                     angle_low=43, angle_high=75, direction="forward", oscillating=True, oscillation_enabled=True)
        self.hass.services.async_register("fan", "set_direction", self.profile_handler)
        self.hass.services.async_register("hass_dyson", "set_oscillation_angles", self.profile_handler)
        self.runtime_obj, _ = await self.runtime(cfg=self.cfg)
        await self.claim(self.runtime_obj)
        await self.settle_watchdog(self.runtime_obj.controls)
        self.executor = Executor(self.cfg, self.runtime_obj.api, "°F")
        self.policy = Policy(self.cfg)
        self.calls.clear()

    def set_fan(self, *, state=None, **attributes):
        old = self.hass.states.get(FAN)
        self.hass.states.async_set(FAN, state or old.state, {**old.attributes, **attributes}, force_update=True)

    def temperature(self, room, value):
        for probe in self.cfg.zones[room].probes:
            self.hass.states.async_set(probe.entity_id, value, {"unit_of_measurement": "°F"}, force_update=True)

    async def profile_handler(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.service == "set_direction":
            self.set_fan(direction=call.data["direction"])
        else:
            self.assertEqual(set(call.data), {"device_id", "lower_angle", "upper_angle"})
            self.assertEqual(call.data["device_id"], self.fan_device.id)
            self.set_fan(angle_low=call.data["lower_angle"], angle_high=call.data["upper_angle"],
                         oscillating=True, oscillation_enabled=True)

    async def plan(self):
        data = await asyncio.to_thread(self.runtime_obj.api.states)
        now = datetime.now(timezone.utc)
        plan = self.policy.decide(data, now, Settings.read(data, self.cfg))
        return plan, data

    async def tick(self):
        plan, data = await self.plan()
        await asyncio.to_thread(self.executor.step, plan, data, time.monotonic())
        return plan

    async def ticket(self):
        plan, data = await self.plan()
        ticket = self.runtime_obj.api.airflow_authorization(FAN, plan, data, time.monotonic())
        return ticket

    async def configure_fixed(self, *, commission=True, demand=74.5):
        control = Config.from_dict(fixed_config()).fan_controls[FAN]
        self.cfg = replace(self.cfg, fan_controls={**self.cfg.fan_controls, FAN: control})
        self.runtime_obj.api.config = self.cfg
        self.runtime_obj.config = self.cfg
        self.policy = Policy(self.cfg)
        self.executor = Executor(self.cfg, self.runtime_obj.api, "°F")
        self.set_fan(supported_features=FanEntityFeature(59), angle_low=100, angle_high=190,
                     oscillating=False, oscillation_enabled=False)
        self.temperature("alpha", demand)
        if commission:
            await async_load_calibrations(self.runtime_obj.api, "native")

    async def send(self, ticket, service="set_airflow_profile", **data):
        if not data:
            data = {"lower_angle": 100, "upper_angle": 190}
        return await asyncio.to_thread(self.runtime_obj.api.airflow_service, ticket, service, **data)

    def boosts(self):
        return [call for call in self.calls if call[0] == "fan" and call[1] in ("turn_on", "set_percentage")
                and call[2].get("percentage", 0) > 20]

    def configure_override(self, percentage):
        control = replace(self.cfg.fan_controls[FAN], speed_override=percentage)
        self.cfg = replace(self.cfg, fan_controls={**self.cfg.fan_controls, FAN: control})
        self.runtime_obj.config = self.runtime_obj.api.config = self.runtime_obj.controls.config = self.cfg
        self.policy = Policy(self.cfg)
        self.executor = Executor(self.cfg, self.runtime_obj.api, "°F")

    async def test_speed_override_preserves_native_aim_and_stops_with_controller(self):
        self.configure_override(70)
        for _ in range(8):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 70)
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "verified")
        self.assertEqual(plan.fan_airflow[FAN]["speed_override"], 70)
        self.assertEqual(plan.fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertTrue(any(call[1] == "set_oscillation_angles" for call in self.calls))
        self.temperature("alpha", 72)
        for _ in range(3):
            plan = await self.tick()
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 70)
        await self.runtime_obj.controls.async_set_enabled(False)
        await self.settle_watchdog(self.runtime_obj.controls)
        self.assertEqual(self.hass.states.get(FAN).state, "off")

    async def test_zero_override_stops_without_new_direction_and_clear_restores_automatic(self):
        self.configure_override(0)
        for _ in range(3):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).state, "off")
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertFalse(any(call[0] == "hass_dyson" or call[1] == "set_direction" for call in self.calls))
        self.configure_override(None)
        for _ in range(8):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "verified")

    async def test_profile_before_boost_atomic_pair_and_explanatory_diagnostics(self):
        _, data = await self.plan()
        self.assertIsInstance(data[FAN]["attributes"]["supported_features"], FanEntityFeature)
        for _ in range(6):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)
        services = [call[1] for call in self.calls if call[0] == "hass_dyson" or call[2].get("entity_id") == FAN]
        self.assertLess(services.index("set_direction"), services.index("set_oscillation_angles"))
        self.assertLess(services.index("set_oscillation_angles"), services.index("turn_on"))
        status = plan.fan_airflow[FAN]
        self.assertTrue(status["provisional"])
        self.assertEqual(status["execution"]["phase"], "verified")
        self.assertFalse(status["execution"]["physical_bearing_measured"])
        self.assertIsNotNone(status["execution"]["last_verified_command"])

    async def test_plain_integer_feature_masks_still_authorize_and_dispatch(self):
        for fixed in (False, True):
            if fixed:
                await self.configure_fixed()
            with self.subTest(fixed=fixed):
                self.set_fan(supported_features=None)
                self.set_fan(supported_features=59 if fixed else 63)
                self.assertIs(type(self.hass.states.get(FAN).attributes["supported_features"]), int)
                self.calls.clear()
                await self.send(await self.ticket())
                self.assertEqual([call[1] for call in self.calls], ["set_oscillation_angles"])

    async def test_malformed_feature_masks_rejected_at_authorization_and_dispatch(self):
        for fixed in (False, True):
            if fixed:
                await self.configure_fixed()
            for features in (None, True, False, "63", 63.0, [], {}):
                with self.subTest(fixed=fixed, features=features):
                    self.set_fan(supported_features=None)
                    self.set_fan(supported_features=FanEntityFeature(59 if fixed else 63))
                    ticket = await self.ticket()
                    # HA reuses equal attribute dictionaries: 63.0 equals the valid flag.
                    self.set_fan(supported_features=None)
                    self.set_fan(supported_features=features)
                    self.assertIs(type(self.hass.states.get(FAN).attributes["supported_features"]), type(features))
                    with self.assertRaisesRegex(HAError, "speed, oscillation"):
                        await self.ticket()
                    with self.assertRaisesRegex(HAError, "speed, oscillation"):
                        await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_feature_flags_still_require_speed_and_oscillation(self):
        for missing in (FanEntityFeature.SET_SPEED, FanEntityFeature.OSCILLATE):
            with self.subTest(missing=missing):
                self.set_fan(supported_features=FanEntityFeature(63))
                ticket = await self.ticket()
                self.set_fan(supported_features=FanEntityFeature(63) & ~missing)
                with self.assertRaisesRegex(HAError, "speed, oscillation"):
                    await self.ticket()
                with self.assertRaisesRegex(HAError, "speed, oscillation"):
                    await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_switchable_feature_flags_still_require_direction_service(self):
        ticket = await self.ticket()
        self.hass.services.async_remove("fan", "set_direction")
        with self.assertRaisesRegex(HAError, "Switchable outlet requires"):
            await self.ticket()
        with self.assertRaisesRegex(HAError, "Switchable outlet requires"):
            await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_stored_angles_and_optimistic_oscillation_do_not_acknowledge(self):
        async def accepted(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            self.set_fan(angle_low=100, angle_high=190, oscillating=True, oscillation_enabled=False)
        self.hass.services.async_register("hass_dyson", "set_oscillation_angles", accepted)
        for _ in range(6):
            plan = await self.tick()
        self.assertEqual(self.boosts(), [])
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "angles")
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)

    async def test_already_matching_cached_settings_still_require_fresh_ack(self):
        self.set_fan(angle_low=100, angle_high=190)
        async def accept_only(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
        self.hass.services.async_register("hass_dyson", "set_oscillation_angles", accept_only)
        for _ in range(6):
            await self.tick()
        self.assertEqual(self.boosts(), [])

    async def test_demand_clear_after_angles_never_completes_old_boost(self):
        for _ in range(3):
            await self.tick()
        self.temperature("alpha", 72)
        for _ in range(3):
            plan = await self.tick()
        self.assertEqual(self.boosts(), [])
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)

    async def test_dispatch_rechecks_loss_of_boost_basis_with_same_comfort_profile(self):
        self.set_fan(angle_low=100, angle_high=190)
        ticket = await self.ticket()
        self.temperature("beta", 73.2)
        with self.assertRaisesRegex(HAActivationDeferred, "no longer permits"):
            await self.send(ticket, "turn_on", percentage=60)
        self.assertEqual(self.boosts(), [])

    async def test_identity_alias_change_at_actual_dispatch_blocks_every_phase(self):
        for service, data in (("set_direction", {"direction": "forward"}),
                              ("set_airflow_profile", {"lower_angle": 100, "upper_angle": 190}),
                              ("turn_on", {"percentage": 60})):
            self.set_fan(angle_low=100, angle_high=190)
            ticket = await self.ticket()
            original = self.hass.async_create_background_task
            def create(coroutine, name, **kwargs):
                if name == "Home Climate device service":
                    er.async_get(self.hass).async_update_entity(FAN, device_id=self.heater_device.id)
                return original(coroutine, name, **kwargs)
            try:
                with patch.object(self.hass, "async_create_background_task", side_effect=create):
                    with self.assertRaises(HAError):
                        await self.send(ticket, service, **data)
            finally:
                er.async_get(self.hass).async_update_entity(FAN, device_id=self.fan_device.id)
        self.assertEqual(self.calls, [])

    async def test_stop_shadow_expired_and_superseded_intents_never_dispatch(self):
        api = self.runtime_obj.api
        ticket = await self.ticket()
        api.live = False
        with self.assertRaisesRegex(HAError, "Shadow"):
            await self.send(ticket)
        api.live = True
        api.stop_event.set()
        with self.assertRaises(HAError):
            await self.send(ticket)
        api.stop_event.clear()
        api.reset_airflow_authorizations()
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)
        expired = replace(await self.ticket(), deadline=time.monotonic() - 1)
        with self.assertRaises(HAActivationDeferred):
            await self.send(expired)
        self.assertEqual(self.calls, [])

    async def test_repair_epoch_at_dispatch_and_integration_identity_changes(self):
        api = self.runtime_obj.api
        ticket = await self.ticket()
        original = self.hass.async_create_background_task
        def create(coroutine, name, **kwargs):
            if name == "Home Climate device service":
                lease = api.repair_gate.begin("synthetic_repair", (FAN,))
                api.repair_gate.release(lease)
            return original(coroutine, name, **kwargs)
        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            with self.assertRaises(HAActivationDeferred):
                await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_missing_angle_service_preserves_minimum_and_independent_retry(self):
        self.hass.services.async_remove("hass_dyson", "set_oscillation_angles")
        for _ in range(4):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertEqual(self.boosts(), [])
        self.assertIn("angle services", plan.fan_airflow[FAN]["execution"]["error"])
        self.assertGreater(plan.fan_airflow[FAN]["execution"]["retry_in_seconds"], 0)

    async def test_verified_boost_is_reduced_on_capability_loss_without_new_boost_command(self):
        for _ in range(6):
            await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)
        self.hass.services.async_remove("hass_dyson", "set_oscillation_angles")
        await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)

    async def test_same_aim_losing_transfer_basis_reduces_to_comfort_floor(self):
        for _ in range(6):
            await self.tick()
        self.temperature("beta", 73.2)
        plan = await self.tick()
        self.assertEqual(plan.fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "verified")
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)

    async def test_speed_ack_failure_does_not_overwrite_or_resend_verified_profile(self):
        for _ in range(3):
            await self.tick()
        async def accepted(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
        self.hass.services.async_register("fan", "turn_on", accepted)
        await self.tick()
        self.assertIn(FAN, self.executor.pending)
        self.assertEqual(self.executor.airflow.pending[FAN].phase, "verified")
        self.executor.pending[FAN].since -= 100
        await self.tick()
        self.assertIn(FAN, self.executor.fan_failures)
        self.assertEqual(self.executor.airflow.pending[FAN].phase, "verified")
        self.assertEqual(sum(call[1] == "set_oscillation_angles" for call in self.calls), 1)

    async def test_verified_profile_rejects_restored_and_stale_feedback(self):
        for _ in range(6):
            await self.tick()
        plan, data = await self.plan()
        data[FAN]["last_reported"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await asyncio.to_thread(self.executor.step, plan, data, time.monotonic())
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertIn("stale", plan.fan_airflow[FAN]["execution"]["error"])
        self.set_fan(assumed_state=True)
        with self.assertRaisesRegex(HAError, "restored/assumed"):
            await self.ticket()

    async def test_older_matching_report_cannot_acknowledge_angles(self):
        for _ in range(2):
            await self.tick()
        pending = self.executor.airflow.pending[FAN]
        self.assertEqual(pending.phase, "angles")
        plan, data = await self.plan()
        data[FAN]["last_reported"] = (pending.baseline - timedelta(seconds=1)).isoformat()
        await asyncio.to_thread(self.executor.step, plan, data, time.monotonic())
        self.assertEqual(pending.phase, "angles")
        self.assertEqual(self.boosts(), [])

    async def test_zero_floor_profile_acknowledges_while_off_then_powers_on_explicitly(self):
        fan = self.cfg.fan_controls[FAN]
        controls = {**self.cfg.fan_controls, FAN: replace(fan, minimum_percentage=0)}
        self.cfg = replace(self.cfg, fan_controls=controls)
        self.runtime_obj.api.config = self.cfg
        self.policy = Policy(self.cfg)
        self.executor = Executor(self.cfg, self.runtime_obj.api, "°F")
        self.set_fan(state="off", percentage=0)
        for _ in range(6):
            await self.tick()
        self.assertEqual(self.hass.states.get(FAN).state, "on")
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)

    async def test_retained_route_and_gradient_hysteresis_survive_dispatch_revalidation(self):
        self.set_fan(angle_low=100, angle_high=190)
        await self.plan()  # Establish the route before a smaller useful gradient.
        self.temperature("beta", 72.6)
        ticket = await self.ticket()
        await self.send(ticket, "turn_on", percentage=60)
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)

    async def test_directional_boost_preserves_existing_speed_quantization(self):
        self.set_fan(angle_low=100, angle_high=190, percentage_step=100 / 3, percentage=33)
        ticket = await self.ticket()
        await self.send(ticket, "turn_on", percentage=66)
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 66)

    async def test_actual_angle_job_outlives_caller_timeout_and_late_completion_cannot_boost(self):
        api = self.runtime_obj.api
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(call):
            entered.set()
            await release.wait()
            await self.profile_handler(call)
        self.hass.services.async_register("hass_dyson", "set_oscillation_angles", delayed)
        ticket = await self.ticket()
        deadline = time.monotonic() + .03
        dispatch = NativeActivationDispatch(deadline, airflow_authorization=ticket)
        request = asyncio.create_task(api._async_request("services/fan/set_airflow_profile",
            {"entity_id": FAN, "lower_angle": 100, "upper_angle": 190}, deadline,
            generation=api.controls.command_generation(FAN), dispatch=dispatch,
            repair_epoch=api.repair_gate.capture(FAN)))
        try:
            await entered.wait()
            with self.assertRaises(TimeoutError):
                await request
            self.assertTrue(api.service_jobs((FAN,)))
            self.assertFalse(api.airflow_jobs_clear(FAN))
            self.assertGreater(api.controls.service_inflight, 0)
            self.temperature("alpha", 72)
            await self.tick()
        finally:
            release.set()
            await self.hass.async_block_till_done(wait_background_tasks=True)
        for _ in range(2):
            await self.tick()
        self.assertEqual(self.boosts(), [])
        self.assertTrue(api.airflow_jobs_clear(FAN))
        self.assertEqual(api.controls.service_inflight, 0)

    async def test_restart_executor_requires_new_profile_ack_and_diagnostics_are_exposed(self):
        for _ in range(6):
            plan = await self.tick()
        self.executor = Executor(self.cfg, self.runtime_obj.api, "°F")
        self.calls.clear()
        plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertEqual(self.boosts(), [])
        self.runtime_obj.status = plan.as_dict()
        sensor = ControllerStatus(SimpleNamespace(entry_id="synthetic", runtime_data=self.runtime_obj))
        self.assertEqual(sensor.extra_state_attributes["fan_airflow"], plan.fan_airflow)

    async def test_shutdown_revokes_tickets_before_first_device_stop(self):
        api = self.runtime_obj.api
        ticket = await self.ticket()
        original = api.service
        def service(*args, **kwargs):
            self.assertNotIn(FAN, api._airflow_authorizations)
            return original(*args, **kwargs)
        with patch.object(api, "service", side_effect=service):
            await asyncio.to_thread(self.executor.shutdown)
        with self.assertRaises(HAActivationDeferred):
            await self.send(ticket)

    async def test_abandoned_queued_profile_cannot_dispatch_when_loop_resumes(self):
        queued = []
        future = Mock()
        future.result.side_effect = FutureTimeout()
        future.cancel.return_value = False
        def hold(coroutine, loop):
            queued.append(coroutine)
            return future
        ticket = await self.ticket()
        try:
            with patch("custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe", side_effect=hold):
                with self.assertRaises(HAActivationDeferred):
                    await self.send(ticket)
            with self.assertRaises(HAActivationDeferred):
                await queued.pop()
        finally:
            for coroutine in queued:
                coroutine.close()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime_obj.controls.service_inflight, 0)

    async def test_ownership_generation_change_at_actual_dispatch_cancels_profile(self):
        api = self.runtime_obj.api
        ticket = await self.ticket()
        generation = api.controls.command_generation(FAN)
        original = self.hass.async_create_background_task
        def create(coroutine, name, **kwargs):
            if name == "Home Climate device service":
                api.controls.command_generation = lambda entity: generation + 1
            return original(coroutine, name, **kwargs)
        old = api.controls.command_generation
        try:
            with patch.object(self.hass, "async_create_background_task", side_effect=create):
                with self.assertRaises(HAActivationDeferred):
                    await self.send(ticket)
        finally:
            api.controls.command_generation = old
        self.assertEqual(self.calls, [])

    async def test_manual_heater_alias_is_never_aimed_or_stopped_as_a_purifier(self):
        controls = self.runtime_obj.controls
        await controls.async_set_auxiliary_manual(True)
        await self.settle_watchdog(controls)
        self.hass.states.async_set(self.cfg.heater_entity, "heat", {
            "hvac_modes": ["off", "heat"], "hvac_action": "heating"})
        self.calls.clear()
        ticket = await self.ticket()
        er.async_get(self.hass).async_update_entity(FAN, device_id=self.heater_device.id)
        try:
            with self.assertRaises(HAError):
                await self.send(ticket)
            with self.assertRaises(HAError):
                await asyncio.to_thread(self.runtime_obj.api.service, "fan", "turn_off", entity_id=FAN)
            self.assertEqual(self.calls, [])
            self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "heat")
        finally:
            er.async_get(self.hass).async_update_entity(FAN, device_id=self.fan_device.id)

    async def test_duplicate_physical_purifier_aliases_do_not_get_independent_direction_queues(self):
        other = next(entity for entity in self.cfg.fans.values() if entity != FAN)
        self.register(other, self.fan_device.id)
        ticket = await self.ticket()
        with self.assertRaisesRegex(HAError, "same device"):
            await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_59_fixed_outlet_rotates_and_boosts_without_direction_service(self):
        await self.configure_fixed()
        _, data = await self.plan()
        self.assertIsInstance(data[FAN]["attributes"]["supported_features"], FanEntityFeature)
        self.hass.services.async_remove("fan", "set_direction")
        for _ in range(5):
            plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 60)
        self.assertTrue(any(call[1] == "set_oscillation_angles" for call in self.calls))
        self.assertFalse(any(call[1] == "set_direction" for call in self.calls))
        execution = plan.fan_airflow[FAN]["execution"]
        self.assertEqual(execution["outlet_control"], "calibrated_fixed")
        self.assertFalse(execution["direction_telemetry_applicable"])
        self.assertIsNone(execution["last_verified_command"]["direction"])
        self.assertIn("direction telemetry not applicable", execution["last_verified_command"]["evidence"])

    async def test_fixed_comfort_aim_needs_no_active_head_and_ignores_synthetic_direction(self):
        await self.configure_fixed(demand=73)
        self.set_fan(direction="synthetic_default_not_evidence")
        for _ in range(5):
            plan = await self.tick()
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_targets_f, {})
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "verified")
        self.assertEqual(self.boosts(), [])
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertFalse(any(call[1] == "set_direction" for call in self.calls))

    async def test_missing_direction_never_auto_converts_unproven_switchable_profile(self):
        self.set_fan(supported_features=FanEntityFeature(59), direction="forward")
        for _ in range(3):
            plan = await self.tick()
        self.assertEqual(self.boosts(), [])
        self.assertFalse(any(call[1] in ("set_direction", "set_oscillation_angles") for call in self.calls))
        self.assertIn("default forward string is not proof", plan.fan_airflow[FAN]["execution"]["error"])

    async def test_fixed_stored_angles_and_optimistic_oscillation_never_complete_ack(self):
        await self.configure_fixed()
        async def accepted(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            self.set_fan(oscillating=True, oscillation_enabled=False)
        self.hass.services.async_register("hass_dyson", "set_oscillation_angles", accepted)
        for _ in range(5):
            plan = await self.tick()
        self.assertEqual(self.boosts(), [])
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "angles")
        self.assertFalse(any(call[1] == "set_direction" for call in self.calls))

    async def test_fixed_outlet_rejects_direction_calls_and_changed_capability_at_dispatch(self):
        await self.configure_fixed()
        ticket = await self.ticket()
        with self.assertRaisesRegex(HAError, "no direction command"):
            await self.send(ticket, "set_direction", direction="forward")
        for features in (63, 57, 51):
            self.set_fan(supported_features=FanEntityFeature(59))
            ticket = await self.ticket()
            self.set_fan(supported_features=FanEntityFeature(features))
            with self.subTest(features=features), self.assertRaises(HAError):
                await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_outlet_calibration_contract_change_invalidates_queued_authorization(self):
        await self.configure_fixed()
        ticket = await self.ticket()
        fan = self.cfg.fan_controls[FAN]
        changed = replace(fan, airflow=replace(fan.airflow, calibration_key="synthetic_new_calibration"))
        self.runtime_obj.api.config = replace(self.cfg, fan_controls={**self.cfg.fan_controls, FAN: changed})
        with self.assertRaisesRegex(HAActivationDeferred, "calibration/outlet contract changed"):
            await self.send(ticket)
        self.assertEqual(self.calls, [])

    async def test_fixed_identity_pin_survives_api_restart_and_rejects_replacement(self):
        await self.configure_fixed()
        original = dict(self.runtime_obj.api._fixed_airflow_bindings)
        replacement = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            identifiers={("hass_dyson", "synthetic_replacement_purifier")})
        er.async_get(self.hass).async_update_entity(FAN, device_id=replacement.id)
        with self.assertRaisesRegex(HAError, "physical identity"):
            await self.ticket()
        api = NativeAPI(self.hass, True, self.runtime_obj.stop_event, self.cfg)
        api.controls = self.runtime_obj.controls
        await async_load_calibrations(api, "native")
        self.assertEqual(api._fixed_airflow_bindings, original)
        plan, _ = await self.plan()
        data = await asyncio.to_thread(api.states)
        with self.assertRaisesRegex(HAError, "physical identity"):
            api.airflow_authorization(FAN, plan, data, time.monotonic())
        self.assertEqual(self.calls, [])

    async def test_fixed_lifecycle_reload_rejects_old_ticket_and_requires_new_ack(self):
        await self.configure_fixed()
        for _ in range(5):
            await self.tick()
        ticket = await self.ticket()
        self.hass.data["fan"] = SimpleNamespace(get_entity=lambda entity: native_object)
        native_object = object()
        with self.assertRaisesRegex(HAActivationDeferred, "lifecycle changed"):
            await self.send(ticket)
        plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "reduce")

    async def test_explicit_new_calibration_key_can_commission_replacement(self):
        await self.configure_fixed()
        original = dict(self.runtime_obj.api._fixed_airflow_bindings)
        replacement = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            identifiers={("hass_dyson", "synthetic_recalibrated_purifier")})
        er.async_get(self.hass).async_update_entity(FAN, device_id=replacement.id)
        fan = self.cfg.fan_controls[FAN]
        changed = replace(fan, airflow=replace(fan.airflow, calibration_key="synthetic_new_calibration"))
        self.cfg = replace(self.cfg, fan_controls={**self.cfg.fan_controls, FAN: changed})
        self.runtime_obj.api.config = self.cfg
        self.policy = Policy(self.cfg)
        await async_load_calibrations(self.runtime_obj.api, "native")
        bindings = self.runtime_obj.api._fixed_airflow_bindings
        self.assertEqual(bindings["synthetic_local_purifier"], original["synthetic_local_purifier"])
        self.assertEqual(bindings["synthetic_new_calibration"][0], replacement.id)
        self.assertEqual((await self.ticket()).identity[0], replacement.id)
        self.assertEqual(self.calls, [])

    async def test_fixed_without_durable_commissioning_or_with_failed_storage_keeps_minimum(self):
        await self.configure_fixed(commission=False)
        plan = await self.tick()
        self.assertIn("uncommissioned", plan.fan_airflow[FAN]["execution"]["error"])
        with patch("custom_components.house_climate.airflow_calibration.Store.async_save",
                   new=AsyncMock(side_effect=OSError("synthetic storage failure"))):
            await async_load_calibrations(self.runtime_obj.api, "native")
        plan = await self.tick()
        self.assertIn("storage unavailable", plan.fan_airflow[FAN]["execution"]["error"])
        self.assertEqual(self.boosts(), [])
        self.assertFalse(any(call[1] in ("set_direction", "set_oscillation_angles") for call in self.calls))

    async def test_corrupt_fixed_calibration_storage_never_recommissions(self):
        await self.configure_fixed(commission=False)
        with patch("custom_components.house_climate.airflow_calibration.Store.async_load",
                   new=AsyncMock(return_value={"bindings": {"synthetic_local_purifier": ["incomplete"]}})), \
             patch("custom_components.house_climate.airflow_calibration.Store.async_save", new=AsyncMock()) as save:
            await async_load_calibrations(self.runtime_obj.api, "native")
        save.assert_not_awaited()
        self.assertEqual(self.runtime_obj.api._fixed_airflow_bindings, {})
        with self.assertRaisesRegex(HAError, "storage unavailable"):
            await self.ticket()
        self.assertEqual(self.calls, [])

    async def test_lost_fixed_calibration_record_never_rebinds_same_key(self):
        from homeassistant.helpers.storage import Store
        await self.configure_fixed()
        replacement = dr.async_get(self.hass).async_get_or_create(config_entry_id=self.owner.entry_id,
            identifiers={("hass_dyson", "synthetic_replacement_purifier")})
        er.async_get(self.hass).async_update_entity(FAN, device_id=replacement.id)
        original_load = Store.async_load
        for missing in ("airflow_calibration", "airflow_commissioned"):
            async def load(store):
                if f".{missing}." in store.key:
                    return None
                return await original_load(store)
            with self.subTest(missing=missing), \
                 patch.object(Store, "async_load", new=load), \
                 patch.object(Store, "async_save", new=AsyncMock()) as save:
                await async_load_calibrations(self.runtime_obj.api, "native")
            save.assert_not_awaited()
            self.assertEqual(self.runtime_obj.api._fixed_airflow_bindings, {})
            with self.assertRaisesRegex(HAError, "storage unavailable"):
                await self.ticket()
        self.assertEqual(self.calls, [])

    async def test_fixed_calibration_key_change_discards_verified_settings(self):
        await self.configure_fixed()
        for _ in range(5):
            await self.tick()
        self.assertIn(FAN, self.executor.airflow.verified)
        fan = self.cfg.fan_controls[FAN]
        changed = replace(fan, airflow=replace(fan.airflow, calibration_key="synthetic_new_calibration"))
        self.cfg = replace(self.cfg, fan_controls={**self.cfg.fan_controls, FAN: changed})
        self.runtime_obj.api.config = self.cfg
        self.executor.config = self.cfg
        self.policy = Policy(self.cfg)
        plan = await self.tick()
        self.assertNotIn(FAN, self.executor.airflow.verified)
        self.assertIsNone(plan.fan_airflow[FAN]["execution"]["last_verified_command"])
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)

    async def test_fixed_lost_oscillation_feedback_removes_boost(self):
        await self.configure_fixed()
        for _ in range(5):
            await self.tick()
        self.set_fan(oscillating=True, oscillation_enabled=False)
        plan = await self.tick()
        self.assertEqual(self.hass.states.get(FAN).attributes["percentage"], 20)
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "reduce")

    async def test_fixed_stop_at_actual_dispatch_prevents_angle_command(self):
        await self.configure_fixed()
        ticket = await self.ticket()
        original = self.hass.async_create_background_task
        def create(coroutine, name, **kwargs):
            if name == "Home Climate device service":
                self.runtime_obj.stop_event.set()
            return original(coroutine, name, **kwargs)
        try:
            with patch.object(self.hass, "async_create_background_task", side_effect=create):
                with self.assertRaises(HAError):
                    await self.send(ticket)
        finally:
            self.runtime_obj.stop_event.clear()
        self.assertEqual(self.calls, [])

    async def test_raw_device_and_untyped_virtual_calls_are_rejected(self):
        api = self.runtime_obj.api
        for domain, service, data in (
                ("hass_dyson", "set_oscillation_angles", {"device_id": self.fan_device.id, "lower_angle": 100, "upper_angle": 190}),
                ("fan", "set_airflow_profile", {"entity_id": FAN, "lower_angle": 100, "upper_angle": 190})):
            with self.assertRaises(HAError):
                await asyncio.to_thread(api.service, domain, service, **data)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
