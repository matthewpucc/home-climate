"""Capability repair through real HA services, registries and native ownership.

All devices are synthetic.  The owning integration reload is the only mocked
boundary; actuator calls still pass through NativeAPI and HA's service registry.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries

from custom_components.house_climate.controller.config import Config, Profile
from custom_components.house_climate.controller.policy import Policy
from custom_components.house_climate.runtime import NativeActivationDispatch
from tests_homeassistant import test_integration_reload as registry_fixtures
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class CapabilityRepairFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    asyncTearDownBase = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog
    entry = registry_fixtures.IntegrationReloadTests.entry
    register = registry_fixtures.IntegrationReloadTests.register

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.owner = self.entry("test_heat_pump_bridge", "capability_owner")
        cfg = replace(Config.from_dict(self.cfg.to_dict()), heater_entity=None,
                      auxiliary_room=None, head_minimum_off_seconds=0,
                      off_settle_seconds=.001)
        self.head, self.sibling = cfg.heads["guest"], cfg.heads["primary"]
        self.scope = (self.head, self.sibling)
        for entity in self.scope:
            self.register(entity, self.owner)
        self.controller, _ = await self.runtime(cfg=cfg, entry_id="capability_flow")
        await self.claim(self.controller)
        await self.settle_watchdog(self.controller.controls)
        self.manager = self.controller.recovery
        self.clock = Mock(return_value=0)
        self.manager._clock = self.clock
        self.manager.capabilities.clock = self.clock
        self.manager.capabilities.evidence.clock = self.clock
        self.poll_patch = patch("custom_components.house_climate.capability_recovery.POLL_SECONDS", .001)
        self.poll_patch.start()
        self.reload = AsyncMock(return_value=True)
        self.reload_patch = patch.object(ConfigEntries, "async_reload", self.reload)
        self.reload_patch.start()
        self.refresh_calls = []
        self.refresh_modes = None
        self.hass.services.async_register("homeassistant", "update_entity", self.refresh)
        self.calls.clear()
        self.worker_report = None
        self.report_factory = None
        self.auto_reports = True
        self.worker = asyncio.create_task(self.worker_reports())

    async def asyncTearDown(self):
        try:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            await self.asyncTearDownBase()
        finally:
            self.reload_patch.stop()
            self.poll_patch.stop()

    def publish(self, entity, *, mode=None, modes=None, attrs=None):
        old = self.hass.states.get(entity)
        attributes = dict(old.attributes) if attrs is None else dict(attrs)
        if modes is not None:
            attributes["hvac_modes"] = list(modes)
        self.hass.states.async_set(entity, mode or old.state, attributes)

    async def refresh(self, call):
        entity = call.data["entity_id"]
        self.refresh_calls.append((self.clock(), entity))
        self.publish(entity, modes=self.refresh_modes)

    def status(self, *, selected="cool", requested="cool"):
        return {"event": "live", "phase": "running", "mode": selected,
                "recovery_details": {},
                "unavailable_mode_requests": {"guest": {requested: {"priority": 1}}},
                "recovery_inputs": self.manager.capabilities.input_signature()}

    async def worker_reports(self):
        """A synthetic worker emits a newer decision after the owner is paused."""
        while True:
            if (self.auto_reports and self.manager.diagnostics.get(
                    "capability_repair", {}).get("phase") == "decision"):
                report = (self.report_factory() if self.report_factory else
                          dict(self.worker_report or self.status()))
                report["recovery_inputs"] = self.manager.capabilities.input_signature()
                self.manager.observe(report)
            await asyncio.sleep(.001)

    async def observe(self, at, status=None):
        self.clock.return_value = at
        self.worker_report = status or self.status()
        self.manager.observe(self.worker_report)
        refreshes = tuple(self.manager.capabilities.refreshes.values())
        if refreshes:
            await asyncio.gather(*refreshes)
        await asyncio.sleep(0)

    async def learn(self, modes=("off", "heat", "cool")):
        for at in (0, 60, 120):
            for entity in self.scope:
                self.publish(entity, mode="off", modes=modes)
            await self.observe(at)
        self.assertEqual(len(self.manager.capabilities.evidence.baselines), 2)
        self.assertEqual(self.refresh_calls, [])

    async def fail_rechecks(self, *, modes=("off", "heat"), status=None):
        self.publish(self.head, modes=modes)
        for at in (180, 240, 300):
            await self.observe(at, status)

    async def start_repair(self, status=None):
        await self.observe(self.clock(), status)
        self.assertIsNotNone(self.manager._task)
        return self.manager._task

    async def finish_repair(self, repair):
        await self.until(lambda: repair.done() or self.manager.diagnostics.get(
            "capability_repair", {}).get("phase") == "minimum_off")
        self.clock.return_value += 1
        return await asyncio.wait_for(repair, 2)

    def actuator_calls(self):
        return [call for call in self.calls if call[0] in ("climate", "fan")]

    def room_temperature(self, room, temperature):
        for probe in self.controller.config.zones[room].probes:
            self.hass.states.async_set(probe.entity_id, str(temperature),
                                       {"unit_of_measurement": "°F"})

    def policy_report(self):
        cfg = replace(self.controller.config, timezone=self.hass.config.time_zone)
        data = {row["entity_id"]: row for row in self.controller.api._read_states()}
        now = datetime.now(timezone.utc)
        settings = self.controller.comfort.settings(data, cfg, now)
        plan = Policy(cfg).decide(data, now, settings, self.hass.config.units.temperature_unit)
        return {**plan.as_dict(), "event": "live", "phase": "running", "recovery_details": {}}

    async def test_cached_polls_do_not_replace_three_spaced_actual_rechecks(self):
        await self.learn()
        self.publish(self.head, modes=("off", "heat"))
        await self.observe(180)
        for at in (181, 195, 210, 239):
            await self.observe(at)
        await self.observe(240)
        for at in (241, 270, 299):
            await self.observe(at)
        self.assertEqual(self.refresh_calls, [(180, self.head), (240, self.head)])
        self.assertEqual(self.manager.diagnostics["capability_incidents"][self.head]["failed_rechecks"], 2)
        self.reload.assert_not_awaited()
        self.assertEqual(self.actuator_calls(), [])
        await self.observe(300)
        repair = await self.start_repair()
        await self.finish_repair(repair)
        self.assertEqual(self.refresh_calls, [(180, self.head), (240, self.head), (300, self.head)])
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def assert_explicit_attempts_without_new_report(self, *, timeout):
        await self.learn()
        self.publish(self.head, modes=("off", "heat"))
        marker = self.hass.states.get(self.head).last_reported
        attempted = []

        async def unresolved_refresh(call):
            attempted.append((self.clock(), call.data["entity_id"]))
            if timeout:
                raise TimeoutError("Synthetic bridge refresh timed out")

        self.hass.services.async_register("homeassistant", "update_entity", unresolved_refresh)
        for failures, at in enumerate((180, 240, 300), 1):
            await self.observe(at)
            incident = self.manager.diagnostics["capability_incidents"][self.head]
            self.assertEqual(incident["failed_rechecks"], failures)
            self.assertEqual(self.hass.states.get(self.head).last_reported, marker)
            self.assertFalse(incident["last_refresh"]["fresh_report"])
            self.assertTrue(incident["last_refresh"]["no_change"])
            self.assertEqual(incident["last_refresh"]["timeout"], timeout)
            self.assertEqual(incident["last_refresh"]["error_type"], "TimeoutError" if timeout else None)
            if failures < 3:
                for poll in (at + 15, at + 30, at + 59):
                    await self.observe(poll)
                    self.assertEqual(self.manager.diagnostics["capability_incidents"][self.head]["failed_rechecks"], failures)
                self.reload.assert_not_awaited()
                self.assertEqual(self.actuator_calls(), [])
        self.assertEqual(attempted, [(180, self.head), (240, self.head), (300, self.head)])
        await self.finish_repair(await self.start_repair())
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def test_no_report_refresh_attempts_count_but_cached_polls_do_not(self):
        await self.assert_explicit_attempts_without_new_report(timeout=False)

    async def test_timeout_refresh_attempts_count_but_cached_polls_do_not(self):
        await self.assert_explicit_attempts_without_new_report(timeout=True)

    async def test_one_good_refresh_resets_consecutive_failures(self):
        await self.learn()
        self.publish(self.head, modes=("off", "heat"))
        await self.observe(180)
        self.refresh_modes = ("off", "heat", "cool")
        await self.observe(240)
        self.assertEqual(self.manager.diagnostics["capability_incidents"][self.head]["failed_rechecks"], 0)
        self.refresh_modes = None
        self.publish(self.head, modes=("off", "heat"))
        for at in (300, 360):
            await self.observe(at)
        self.assertEqual(self.manager.diagnostics["capability_incidents"][self.head]["failed_rechecks"], 2)
        self.reload.assert_not_awaited()
        self.assertEqual(self.actuator_calls(), [])
        await self.observe(420)
        await self.finish_repair(await self.start_repair())
        self.reload.assert_awaited_once()

    async def test_real_cool_only_baseline_and_valid_off_do_not_invent_loss(self):
        await self.learn(("off", "cool"))
        for at in (180, 240, 300, 900):
            self.publish(self.head, mode="off", modes=("off", "cool"))
            await self.observe(at, self.status(selected=None, requested="heat"))
        self.assertEqual(self.manager.diagnostics["capability_incidents"], {})
        self.assertEqual(self.refresh_calls, [])
        self.assertEqual(self.actuator_calls(), [])
        self.reload.assert_not_awaited()

    async def test_opposite_mode_comfort_loss_does_not_stop_healthy_shared_cooling(self):
        await self.learn()
        for entity in self.scope:
            self.publish(entity, mode="cool")
        cfg = self.controller.config
        zones = {**cfg.zones,
                 "primary": replace(cfg.zones["primary"], schedules=(),
                                    profile=Profile(target_f=68, half_band_f=1, priority=10)),
                 "guest": replace(cfg.zones["guest"], schedules=(),
                                  profile=Profile(target_f=72, half_band_f=1.5, priority=1))}
        cfg = self.controller.config = replace(cfg, zones=zones)
        for room, temperature in {"primary": 71, "guest": 68, "office": 72,
                                  "entertainment": 71, "living": 68}.items():
            self.room_temperature(room, temperature)
        self.publish(self.head, modes=("off", "cool"))
        status = self.policy_report()
        self.assertEqual(status["mode"], "cool")
        self.assertIn("primary", status["head_targets_f"])
        self.assertIn("heat", status["unavailable_mode_requests"]["guest"])
        self.assertGreater(status["room_targets"]["primary"]["priority"],
                           status["room_targets"]["guest"]["priority"])
        self.assertEqual(status["recovery_inputs"], self.manager.capabilities.input_signature())
        await self.fail_rechecks(modes=("off", "cool"), status=status)
        await self.observe(301, status)
        incident = self.manager.diagnostics["capability_incidents"][self.head]
        self.assertFalse(incident["required"])
        self.assertIn("advisory", incident["reason"])
        self.assertEqual(self.actuator_calls(), [])
        self.reload.assert_not_awaited()
        self.assertTrue(all(self.hass.states.get(entity).state == "cool" for entity in self.scope))

    async def test_changed_preset_before_queued_repair_admission_cancels_obsolete_loss(self):
        await self.learn()
        await self.fail_rechecks()
        self.manager.observe(self.status())
        repair = self.manager._task
        self.assertIsNotNone(repair)
        old = self.hass.states.get(self.head)
        self.publish(self.head, attrs={**old.attributes, "preset_mode": "away"})
        self.assertFalse(await asyncio.wait_for(repair, 2))
        self.reload.assert_not_awaited()
        self.assertEqual(self.actuator_calls(), [])
        self.assertFalse(self.controller.api.repair_gate.held(self.head))

    async def test_changed_probe_before_queued_admission_invalidates_old_decision(self):
        await self.learn()
        await self.fail_rechecks()
        self.manager.observe(self.status())
        repair = self.manager._task
        self.assertIsNotNone(repair)
        probe = self.controller.config.zones["guest"].probes[0]
        self.hass.states.async_set(probe.entity_id, "69", {"unit_of_measurement": "°F"})
        self.assertFalse(await asyncio.wait_for(repair, 2))
        self.reload.assert_not_awaited()
        self.assertEqual(self.actuator_calls(), [])
        self.assertFalse(self.controller.api.repair_gate.held(self.head))

    async def test_probe_change_in_queued_reload_wrapper_aborts_without_charging_attempt(self):
        await self.learn()
        helper = self.manager.capabilities
        original = helper._async_reload_checked
        entered, release = asyncio.Event(), asyncio.Event()

        async def queued_reload(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        repair = None
        with patch.object(helper, "_async_reload_checked", queued_reload):
            try:
                await self.fail_rechecks()
                repair = await self.start_repair()
                await asyncio.wait_for(entered.wait(), 1)
                before = {entity: self.hass.states.get(entity).last_reported for entity in self.scope}
                self.room_temperature("guest", 69)
                release.set()
                await self.until(lambda: self.manager.diagnostics.get(
                    "capability_repair", {}).get("phase") == "minimum_off")
                self.reload.assert_not_awaited()
                self.assertNotIn(self.owner.entry_id, self.manager._attempts)
                self.assertFalse(repair.done())
                self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.assertTrue(all(self.hass.states.get(entity).last_reported > before[entity]
                                    for entity in self.scope))
                self.clock.return_value += 1
                self.assertFalse(await asyncio.wait_for(repair, 2))
                self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.assertTrue(self.controller.controls.enabled)
            finally:
                release.set()
                if repair and not repair.done():
                    repair.cancel()
                    await asyncio.gather(repair, return_exceptions=True)

    async def test_watchdog_job_in_queued_reload_wrapper_blocks_unload_until_safe_abort(self):
        await self.learn()
        helper = self.manager.capabilities
        original = helper._async_reload_checked
        queued, admit = asyncio.Event(), asyncio.Event()
        off_started, off_release = asyncio.Event(), asyncio.Event()

        async def queued_reload(*args, **kwargs):
            queued.set()
            await admit.wait()
            return await original(*args, **kwargs)

        async def slow_off(call):
            await self.handle(call)
            if call.data["entity_id"] == self.sibling:
                off_started.set()
                await off_release.wait()

        repair, watchdog = None, None
        with patch.object(helper, "_async_reload_checked", queued_reload):
            try:
                await self.fail_rechecks()
                repair = await self.start_repair()
                await asyncio.wait_for(queued.wait(), 1)
                self.hass.services.async_register("climate", "set_hvac_mode", slow_off)
                watchdog = asyncio.create_task(self.controller.controls._off(self.sibling))
                await asyncio.wait_for(off_started.wait(), 1)
                self.assertEqual(self.hass.states.get(self.sibling).state, "off")
                before = {entity: self.hass.states.get(entity).last_reported for entity in self.scope}
                admit.set()
                await asyncio.sleep(.02)
                self.reload.assert_not_awaited()
                self.assertNotIn(self.owner.entry_id, self.manager._attempts)
                self.assertFalse(repair.done())
                self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.assertTrue(self.controller.api.service_jobs(self.scope))
                off_release.set()
                self.assertTrue(await asyncio.wait_for(watchdog, 1))
                await self.until(lambda: self.manager.diagnostics.get(
                    "capability_repair", {}).get("phase") == "minimum_off")
                self.reload.assert_not_awaited()
                self.assertFalse(repair.done())
                self.assertTrue(all(self.hass.states.get(entity).last_reported > before[entity]
                                    for entity in self.scope))
                self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.clock.return_value += 1
                self.assertFalse(await asyncio.wait_for(repair, 2))
                self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
                self.assertNotIn(self.owner.entry_id, self.manager._attempts)
            finally:
                admit.set()
                off_release.set()
                self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
                if watchdog:
                    await asyncio.gather(watchdog, return_exceptions=True)
                if repair and not repair.done():
                    repair.cancel()
                    await asyncio.gather(repair, return_exceptions=True)

    async def test_post_pause_new_heat_demand_aborts_reload_then_rejoins_after_fresh_off(self):
        await self.learn()
        for room, temperature in {"primary": 76, "guest": 76, "office": 72,
                                  "entertainment": 71, "living": 68}.items():
            self.room_temperature(room, temperature)
        self.publish(self.head, modes=("off", "heat"))
        self.publish(self.sibling, mode="cool")
        initial = self.policy_report()
        self.assertEqual(initial["mode"], "cool")
        self.assertIn("cool", initial["unavailable_mode_requests"]["guest"])
        self.auto_reports = False
        await self.fail_rechecks(status=initial)
        repair = await self.start_repair(initial)
        await self.until(lambda: self.manager.diagnostics.get(
            "capability_repair", {}).get("phase") == "decision")
        self.assertTrue(all(self.hass.states.get(entity).state == "off" for entity in self.scope))
        before = {entity: self.hass.states.get(entity).last_reported for entity in self.scope}
        # A newer delivery of the pre-pause decision must not authorize reload.
        self.manager.observe(initial)
        await asyncio.sleep(.02)
        self.reload.assert_not_awaited()
        self.assertFalse(repair.done())
        self.room_temperature("guest", 72)
        self.room_temperature("primary", 64)
        changed = self.policy_report()
        self.assertEqual(changed["mode"], "heat")
        self.assertIn("primary", changed["head_targets_f"])
        self.assertNotIn("cool", changed["unavailable_mode_requests"].get("guest", {}))
        self.worker_report = changed
        self.manager.observe(changed)
        await self.until(lambda: self.manager.diagnostics.get(
            "capability_repair", {}).get("phase") == "minimum_off")
        self.reload.assert_not_awaited()
        self.assertFalse(repair.done())
        self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
        self.assertTrue(all(self.hass.states.get(entity).last_reported > before[entity]
                            for entity in self.scope))
        self.clock.return_value += 1
        self.assertFalse(await asyncio.wait_for(repair, 2))
        self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
        self.assertTrue(self.controller.controls.enabled)
        self.assertNotIn(self.owner.entry_id, self.manager._attempts)
        self.assertTrue(all(service == "set_hvac_mode" and data["hvac_mode"] == "off"
                            for domain, service, data in self.actuator_calls() if domain == "climate"))

    async def test_repair_drains_actual_job_then_stops_healthy_sibling_before_reload(self):
        await self.learn()
        self.publish(self.sibling, mode="cool")
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        reloaded = False

        async def delayed_positive(call):
            entered.set()
            await release.wait()
            order.append("old-positive-finished")
            self.publish(self.head, mode="cool")

        async def off(call):
            entity = call.data["entity_id"]
            order.append(("off", entity))
            self.calls.append((call.domain, call.service, dict(call.data)))
            # The broken head is allowed to ignore Off until its owner reloads.
            if entity != self.head or reloaded:
                self.publish(entity, mode="off")

        async def reload_owner(entry_id):
            nonlocal reloaded
            order.append("reload")
            self.assertEqual(entry_id, self.owner.entry_id)
            self.assertEqual(self.hass.states.get(self.sibling).state, "off")
            self.assertEqual(self.hass.states.get(self.head).state, "cool")
            self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
            reloaded = True
            self.publish(self.head, modes=("off", "heat", "cool"))
            return True

        self.hass.services.async_register("climate", "set_temperature", delayed_positive)
        self.hass.services.async_register("climate", "set_hvac_mode", off)
        self.reload.side_effect = reload_owner
        deadline = time.monotonic() + 5
        positive = asyncio.create_task(self.controller.api._async_request(
            "services/climate/set_temperature", {"entity_id": self.head, "hvac_mode": "cool", "temperature": 69},
            deadline, generation=self.controller.controls.ownership_generation,
            dispatch=NativeActivationDispatch(deadline)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.fail_rechecks()
            repair = await self.start_repair()
            await self.until(lambda: self.controller.api.repair_gate.held(self.head))
            self.reload.assert_not_awaited()
            self.assertEqual(order, [])
            release.set()
            await positive
            await self.finish_repair(repair)
            self.assertLess(order.index("old-positive-finished"), order.index(("off", self.sibling)))
            self.assertLess(order.index(("off", self.sibling)), order.index("reload"))
            self.assertEqual(self.manager.diagnostics["capability_repair"]["phase"], "ready")
            self.assertTrue(self.controller.controls.enabled)
            self.assertTrue(set(self.scope) <= set(self.controller.controls.owned))
            self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
            self.assertTrue(all(self.hass.states.get(entity).state == "off" for entity in self.scope))
        finally:
            release.set()
            await asyncio.gather(positive, return_exceptions=True)
            self.hass.services.async_register("climate", "set_temperature", self.handle)
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)

    async def test_failed_reload_safe_rejoin_keeps_incident_and_backoff_budget(self):
        await self.learn()
        self.reload.return_value = False
        await self.fail_rechecks()
        await self.finish_repair(await self.start_repair())
        self.assertEqual(self.manager.diagnostics["capability_repair"]["phase"], "ready")
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)
        self.assertIn(self.head, self.manager.diagnostics["capability_incidents"])
        self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
        self.calls.clear()
        self.publish(self.sibling, mode="cool")
        await self.observe(1199)
        await self.observe(1199)
        self.reload.assert_awaited_once()
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)
        self.assertIn("cooldown", self.manager.diagnostics["capability_incidents"][self.head]["reason"])
        self.assertEqual(self.actuator_calls(), [])

    async def test_three_reload_budget_requires_stable_fresh_restoration_to_reset(self):
        await self.learn()
        await self.fail_rechecks()
        await self.finish_repair(await self.start_repair())
        for at in (1200, 3000):
            await self.observe(at)
            await self.finish_repair(await self.start_repair())
        self.assertEqual(self.reload.await_count, 3)
        await self.observe(6600)
        await self.observe(6600)
        self.assertEqual(self.reload.await_count, 3)
        self.assertIn("three attempts", self.manager.diagnostics["capability_incidents"][self.head]["reason"])
        for at in (6600, 6660):
            for entity in self.scope:
                self.publish(entity, mode="off", modes=("off", "heat", "cool"))
            await self.observe(at)
            self.assertEqual(self.manager._attempts[self.owner.entry_id], 3)
        for entity in self.scope:
            self.publish(entity, mode="off", modes=("off", "heat", "cool"))
        await self.observe(6720)
        self.assertNotIn(self.head, self.manager.diagnostics["capability_incidents"])
        self.assertNotIn(self.owner.entry_id, self.manager._attempts)
        self.assertEqual(self.reload.await_count, 3)

    async def test_late_watchdog_off_during_minimum_off_keeps_lease_until_handler_drains(self):
        await self.learn()
        await self.fail_rechecks()
        repair = await self.start_repair()
        await self.until(lambda: self.manager.diagnostics.get(
            "capability_repair", {}).get("phase") == "minimum_off")
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_off(call):
            await self.handle(call)
            entered.set()
            await release.wait()

        self.hass.services.async_register("climate", "set_hvac_mode", slow_off)
        watchdog = asyncio.create_task(self.controller.controls._off(self.sibling))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.hass.states.get(self.sibling).state, "off")
            self.clock.return_value += 1
            await asyncio.sleep(.02)
            self.assertFalse(repair.done())
            self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
            self.assertTrue(self.controller.api.service_jobs(self.scope))
            release.set()
            self.assertTrue(await asyncio.wait_for(watchdog, 1))
            await asyncio.wait_for(repair, 2)
            self.assertTrue(all(not self.controller.api.repair_gate.held(entity) for entity in self.scope))
        finally:
            release.set()
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            await asyncio.gather(watchdog, return_exceptions=True)

    async def test_disable_during_real_reload_cannot_reenable_or_issue_new_calls(self):
        await self.learn()
        entered, release = asyncio.Event(), asyncio.Event()

        async def reload_owner(_entry_id):
            entered.set()
            await release.wait()
            return True

        self.reload.side_effect = reload_owner
        repair = None
        try:
            await self.fail_rechecks()
            repair = await self.start_repair()
            await asyncio.wait_for(entered.wait(), 1)
            await self.controller.controls.async_set_enabled(False)
            await self.settle_watchdog(self.controller.controls)
            self.assertFalse(self.controller.controls.enabled)
            self.assertFalse(self.controller.controls.owned)
            self.calls.clear()
            release.set()
            await asyncio.wait_for(repair, 2)
            self.assertFalse(self.controller.controls.enabled)
            self.assertFalse(self.controller.controls.owned)
            self.assertEqual(self.actuator_calls(), [])
            self.assertTrue(all(self.hass.states.get(entity).state == "off" for entity in self.scope))
        finally:
            release.set()
            if repair and not repair.done():
                await asyncio.gather(repair, return_exceptions=True)

    async def test_post_reload_off_without_actual_metadata_keeps_repair_fenced(self):
        await self.learn()
        off_after_reload = asyncio.Event()
        reloaded = False

        async def off(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            self.publish(call.data["entity_id"], mode="off", attrs={} if reloaded else None)
            if reloaded:
                off_after_reload.set()

        async def reload_owner(_entry_id):
            nonlocal reloaded
            reloaded = True
            for entity in self.scope:
                self.publish(entity, mode="off", attrs={})
            return True

        self.hass.services.async_register("climate", "set_hvac_mode", off)
        self.reload.side_effect = reload_owner
        repair = None
        try:
            await self.fail_rechecks()
            repair = await self.start_repair()
            await asyncio.wait_for(off_after_reload.wait(), 1)
            await asyncio.sleep(.02)
            self.assertFalse(repair.done())
            self.assertEqual(self.manager.diagnostics["capability_repair"]["phase"], "verifying")
            self.assertTrue(all(self.controller.api.repair_gate.held(entity) for entity in self.scope))
            for entity in self.scope:
                self.publish(entity, mode="off", modes=("off", "heat", "cool"))
            await self.finish_repair(repair)
            self.assertEqual(self.manager.diagnostics["capability_repair"]["phase"], "ready")
        finally:
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            if repair and not repair.done():
                repair.cancel()
                await asyncio.gather(repair, return_exceptions=True)

    async def test_cancel_after_modes_restore_cannot_release_head_still_reporting_cool(self):
        await self.learn()
        verifying, reloaded = asyncio.Event(), False

        async def off(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            entity = call.data["entity_id"]
            if not reloaded or entity != self.head:
                self.publish(entity, mode="off")
            elif entity == self.head:
                verifying.set()

        async def reload_owner(_entry_id):
            nonlocal reloaded
            reloaded = True
            for entity in self.scope:
                self.publish(entity, mode="cool" if entity == self.head else "off",
                             modes=("off", "heat", "cool"))
            return True

        self.hass.services.async_register("climate", "set_hvac_mode", off)
        self.reload.side_effect = reload_owner
        repair = None
        try:
            await self.fail_rechecks()
            repair = await self.start_repair()
            await asyncio.wait_for(verifying.wait(), 1)
            repair.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await repair
            self.assertEqual(self.hass.states.get(self.head).state, "cool")
            self.assertTrue(self.controller.api.repair_gate.held(self.head))
            self.assertTrue(self.controller.api.repair_gate.held(self.sibling))
            self.assertTrue(self.controller.controls.enabled)
        finally:
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
            if repair and not repair.done():
                repair.cancel()
                await asyncio.gather(repair, return_exceptions=True)

    async def test_explicit_reload_while_disabled_unowned_preserves_manual_cool_69(self):
        await self.controller.controls.async_set_enabled(False)
        await self.settle_watchdog(self.controller.controls)
        self.assertFalse(self.controller.controls.owned)
        for entity in self.scope:
            self.publish(entity, mode="cool")
            state = self.hass.states.get(entity)
            self.publish(entity, attrs={**state.attributes, "temperature": 69})
        self.calls.clear()
        await self.manager.async_reload_entity(self.head)
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        self.assertEqual(self.actuator_calls(), [])
        self.assertFalse(self.controller.controls.enabled)
        self.assertFalse(self.controller.controls.owned)
        for entity in self.scope:
            state = self.hass.states.get(entity)
            self.assertEqual((state.state, state.attributes["temperature"]), ("cool", 69))
