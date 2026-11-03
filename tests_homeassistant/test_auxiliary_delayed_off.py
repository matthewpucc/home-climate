"""Delayed native Off feedback must settle the stop before lease expiry."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError

from tests_homeassistant import test_heartbeat_delivery as heartbeat


class AuxiliaryDelayedOffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = heartbeat.HeartbeatDeliveryTests.asyncSetUp
    asyncTearDown = heartbeat.HeartbeatDeliveryTests.asyncTearDown
    handle = heartbeat.HeartbeatDeliveryTests.handle
    until = heartbeat.HeartbeatDeliveryTests.until
    runtime = heartbeat.HeartbeatDeliveryTests.runtime
    claim = heartbeat.HeartbeatDeliveryTests.claim
    settle_watchdog = heartbeat.HeartbeatDeliveryTests.settle_watchdog
    timed_runtime = heartbeat.HeartbeatDeliveryTests.timed_runtime
    controlled_clock = heartbeat.HeartbeatDeliveryTests.controlled_clock
    set_device_mode = heartbeat.HeartbeatDeliveryTests.set_device_mode

    async def delayed_off(self):
        runtime, _ = await self.timed_runtime()
        clock = self.controlled_clock()
        await self.claim(runtime)
        c = runtime.controls
        await self.settle_watchdog(c)
        c._schedule_check = lambda *_: None
        heater = runtime.config.heater_entity
        await asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=heater, hvac_mode="heat", temperature=86,
        )
        guest = runtime.config.heads["guest"]
        self.set_device_mode(guest, "cool")

        async def completed_without_report(call):
            if call.data.get("entity_id") == heater and call.data.get("hvac_mode") == "off":
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", completed_without_report)
        clock.value = 150
        await asyncio.to_thread(
            runtime.api.service, "climate", "set_hvac_mode",
            entity_id=heater, hvac_mode="off",
        )
        self.assertTrue(c._auxiliary_heartbeat_off_receipt)
        self.assertTrue(c._auxiliary_heat_possible)
        self.assertFalse(c._auxiliary_heartbeat_off_ack)
        self.assertFalse(c.pending_device_jobs({heater}))
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        return runtime, clock

    async def test_delayed_off_is_reconciled_before_watchdog_expiry(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        authorized = (c.auxiliary_authorized_at, c.auxiliary_authorized_wall)
        epoch = c._auxiliary_stop_epoch
        clock.value = 151
        self.set_device_mode(runtime.config.heater_entity, "off")
        baseline = list(self.calls)
        # The next watchdog read crosses the lease deadline. The completed Off
        # and subsequent report already suffice to settle this stop.
        clock.value = 221
        await c.async_check()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heat_possible)
        self.assertTrue(c._auxiliary_heartbeat_off_ack)
        self.assertEqual(c._auxiliary_stop_epoch, epoch)
        self.assertEqual((c.auxiliary_authorized_at, c.auxiliary_authorized_wall), authorized)
        self.assertEqual(self.calls, baseline)
        self.assertTrue(c.enabled)
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")

    def feedback(self, runtime, *, state="off", attributes=None, reported=None):
        """Inject only the read under test, without changing HA's report clock."""
        heater = runtime.config.heater_entity
        get = self.hass.states.get
        old = get(heater)
        snapshot = SimpleNamespace(
            state=state, attributes={**old.attributes, **(attributes or {})},
            last_reported=reported or old.last_reported,
            last_updated=old.last_updated, last_changed=old.last_changed,
        )
        return patch.object(type(self.hass.states), "get",
                            new=lambda registry, entity: snapshot if entity == heater else get(entity))

    async def test_shared_heartbeat_reconciles_without_renewing_auxiliary_lease(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        authorized = (c.auxiliary_authorized_at, c.auxiliary_authorized_wall)
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 221
        c.confirm_heartbeat()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heat_possible)
        self.assertEqual(c.heartbeat_monotonic, 221)
        self.assertEqual((c.auxiliary_authorized_at, c.auxiliary_authorized_wall), authorized)
        with self.assertRaises(HAActivationDeferred):
            c.check_activation(runtime.config.heater_entity, {"hvac_mode": "heat"})

    async def test_head_dispatch_reconciles_without_disturbing_cooling(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        authorized = c.auxiliary_authorized_at
        clock.value = 221
        guest = runtime.config.heads["guest"]
        await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                entity_id=guest, hvac_mode="cool", temperature=73)
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertEqual(c.auxiliary_authorized_at, authorized)
        self.assertEqual(self.hass.states.get(guest).state, "cool")

    async def test_explicit_new_authorization_after_delayed_off_is_admitted(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 221
        c.confirm_auxiliary_authorization()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heat_possible)
        self.assertEqual(c.auxiliary_authorized_at, 221)

    async def test_ack_before_deadline_remains_idle_after_deadline(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 151
        await c.async_check()
        self.assertFalse(c._auxiliary_heat_possible)
        clock.value = 221
        await c.async_check()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertEqual(c.auxiliary_authorized_at, 100)

    async def test_restored_assumed_and_active_feedback_cannot_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        for attributes in ({"restored": True}, {"assumed_state": True},
                           {"hvac_action": "heating"}, {"hvac_action": "cooling"}):
            with self.subTest(attributes=attributes), self.feedback(runtime, attributes=attributes):
                self.assertFalse(c._ack_auxiliary_heartbeat_off())
                self.assertTrue(c._auxiliary_heat_possible)
        with self.feedback(runtime, attributes={"restored": True}):
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertTrue(c.auxiliary_heartbeat_blocked)

    async def test_old_future_and_precommand_reports_cannot_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        reported = self.hass.states.get(runtime.config.heater_entity).last_reported
        # Equal-to-dispatch is not ordered after the stop, either.
        dispatch = c._auxiliary_heartbeat_off_receipt[2]
        for timestamp in (reported - timedelta(seconds=181),
                          reported + timedelta(seconds=60),
                          dispatch - timedelta(microseconds=1), dispatch):
            with self.subTest(timestamp=timestamp), self.feedback(runtime, reported=timestamp):
                self.assertFalse(c._ack_auxiliary_heartbeat_off())
                self.assertTrue(c._auxiliary_heat_possible)
        clock.value = 221
        with self.feedback(runtime, reported=dispatch):
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertTrue(c.auxiliary_heartbeat_blocked)

    async def test_same_value_postcommand_report_can_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        self.set_device_mode(heater, "off")
        receipt = c.auxiliary_off_started()
        c.auxiliary_off_finished(receipt)
        self.assertFalse(c._auxiliary_heartbeat_off_ack)
        # HA advances last_reported even for an unchanged state and attributes.
        self.set_device_mode(heater, "off")
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heat_possible)

    async def test_changed_controller_ownership_rejects_completed_receipt(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        c.ownership_generation += 1
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        self.assertTrue(c._auxiliary_heat_possible)

    async def test_changed_auxiliary_ownership_rejects_completed_receipt(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        c.auxiliary_generation += 1
        self.set_device_mode(runtime.config.heater_entity, "off")
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        self.assertTrue(c._auxiliary_heat_possible)

    async def test_late_positive_completion_invalidates_earlier_off(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        c.auxiliary_activation_started({"hvac_mode": "heat"})
        old_receipt = c.auxiliary_off_started()
        self.set_device_mode(heater, "off")
        c.auxiliary_off_finished(old_receipt)
        self.assertFalse(c._auxiliary_heartbeat_off_ack)
        c.auxiliary_activation_finished()
        c.auxiliary_off_finished(old_receipt)
        self.set_device_mode(heater, "off")
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        self.assertTrue(c._auxiliary_heat_possible)
        await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                entity_id=heater, hvac_mode="off")
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertTrue(c.auxiliary_heartbeat_blocked)

    async def test_report_before_actual_off_handler_completion_cannot_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        entered, release = asyncio.Event(), asyncio.Event()
        async def held_off(call):
            await self.handle(call)
            entered.set()
            await release.wait()
        self.hass.services.async_register("climate", "set_hvac_mode", held_off)
        command = asyncio.create_task(asyncio.to_thread(
            runtime.api.service, "climate", "set_hvac_mode",
            entity_id=heater, hvac_mode="off"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.hass.states.get(heater).state, "off")
            self.assertTrue(c.pending_device_jobs({heater}))
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertTrue(c.auxiliary_heartbeat_blocked)
            self.assertFalse(c._auxiliary_heartbeat_off_ack)
        finally:
            release.set()
            await command
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        self.assertTrue(c.auxiliary_heartbeat_blocked)

    async def test_off_report_from_failed_handler_is_not_a_receipt(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                entity_id=heater, hvac_mode="heat", temperature=86)
        async def failed_off(call):
            await self.handle(call)
            raise RuntimeError("Synthetic Off handler failure after reporting")
        self.hass.services.async_register("climate", "set_hvac_mode", failed_off)
        try:
            with self.assertRaises(HAError):
                await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                        entity_id=heater, hvac_mode="off")
        finally:
            self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heartbeat_off_ack)

    async def test_queued_heater_command_prevents_retiring_stop_obligation(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.api._async_service_job
        async def queued(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(runtime.api, "_async_service_job", new=queued):
            command = asyncio.create_task(asyncio.to_thread(
                runtime.api.service, "climate", "set_temperature",
                entity_id=heater, hvac_mode="heat", temperature=86))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(c.auxiliary_inflight, 0)
                self.assertTrue(c.pending_device_jobs({heater}))
                self.set_device_mode(heater, "off")
                clock.value = 221
                c._check_auxiliary_heartbeat_deadline(clock.value)
                self.assertTrue(c.auxiliary_heartbeat_blocked)
                self.assertTrue(c._auxiliary_heat_possible)
            finally:
                release.set()
                with self.assertRaises(HAError):
                    await command
        self.assertEqual(self.hass.states.get(heater).state, "off")

    async def test_uncertain_heater_job_prevents_retiring_stop_obligation(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        self.set_device_mode(heater, "off")
        c.activation_job_uncertain(heater)
        try:
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertTrue(c.auxiliary_heartbeat_blocked)
            self.assertTrue(c._auxiliary_heat_possible)
            self.assertIn(heater, c._uncertain_device_jobs)
        finally:
            c._uncertain_device_jobs.discard(heater)

    async def test_pending_watchdog_stop_cannot_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        self.set_device_mode(heater, "off")
        pending = self.hass.loop.create_future()
        c._stop_jobs[pending] = heater
        try:
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertTrue(c.auxiliary_heartbeat_blocked)
            self.assertFalse(c._auxiliary_heartbeat_off_ack)
        finally:
            pending.set_result(None)
            c._stop_jobs.pop(pending)

    async def test_unrelated_head_jobs_do_not_prevent_heater_ack(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        self.set_device_mode(runtime.config.heater_entity, "off")
        guest = runtime.config.heads["guest"]
        pending = self.hass.loop.create_future()
        c._stop_jobs[pending] = guest
        c.inflight = 1
        try:
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertFalse(c.auxiliary_heartbeat_blocked)
            self.assertFalse(c._auxiliary_heat_possible)
        finally:
            c.inflight = 0
            pending.set_result(None)
            c._stop_jobs.pop(pending)

    async def test_confirmed_power_isolation_remains_independent_stop_proof(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        guard = SimpleNamespace(power_cut_confirmed=True, emergency_fallback_enabled=False)
        # The unpowered native device still reports Heat.
        with patch.object(c, "power_guard", guard):
            clock.value = 221
            c._check_auxiliary_heartbeat_deadline(clock.value)
            self.assertFalse(c.auxiliary_heartbeat_blocked)
            self.assertFalse(c._auxiliary_heat_possible)
            self.assertEqual(c.auxiliary_authorized_at, 100)

    async def test_power_isolation_does_not_erase_uncertain_or_pending_jobs(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        guard = SimpleNamespace(power_cut_confirmed=True, emergency_fallback_enabled=False)
        with patch.object(c, "power_guard", guard):
            c._uncertain_device_jobs.add(heater)
            try:
                self.assertFalse(c._ack_auxiliary_heartbeat_off())
                self.assertTrue(c._auxiliary_heat_possible)
            finally:
                c._uncertain_device_jobs.discard(heater)
            with patch.object(c, "pending_device_jobs", return_value=(object(),)):
                self.assertFalse(c._ack_auxiliary_heartbeat_off())
                self.assertTrue(c._auxiliary_heat_possible)
            self.assertTrue(c._ack_auxiliary_heartbeat_off())

    async def test_genuine_expiry_stays_latched_after_fresh_stop(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        heater = runtime.config.heater_entity
        authorized = (c.auxiliary_authorized_at, c.auxiliary_authorized_wall)
        clock.value = 220  # The exact 120-second boundary remains valid.
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        clock.value = 221
        c._check_auxiliary_heartbeat_deadline(clock.value)
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        # A later report from the old stop cannot acknowledge the expiry epoch.
        self.set_device_mode(heater, "off")
        self.assertFalse(c._ack_auxiliary_heartbeat_off())
        await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                entity_id=heater, hvac_mode="off")
        self.assertTrue(c._ack_auxiliary_heartbeat_off())
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        self.assertEqual((c.auxiliary_authorized_at, c.auxiliary_authorized_wall), authorized)
        c.confirm_heartbeat()
        self.assertTrue(c.auxiliary_heartbeat_blocked)
        c.confirm_auxiliary_authorization()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertEqual(c.auxiliary_authorized_at, 221)
