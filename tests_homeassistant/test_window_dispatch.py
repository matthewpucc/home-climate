"""Window-relieved cooling must retain its original proof through HA dispatch."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch

from homeassistant.core import State

from custom_components.house_climate.controller.cadence import DecisionCadence
from custom_components.house_climate.controller.config import Config, Probe, Settings
from custom_components.house_climate.controller.executor import Executor
from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.controller.protection import ProtectionTemperatures
from custom_components.house_climate.controller.window_authorization import check_window_authorization
from tests.test_rooms import configuration, states
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


CONTACT = "binary_sensor.dispatch_window"
OUTDOOR = "sensor.dispatch_outdoor"


class WindowDispatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        raw = configuration()
        for room in raw["rooms"].values():
            room["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
            room["schedules"] = []
            room["fans"] = []
        cfg = Config.from_dict(raw)
        cfg = replace(cfg, timezone=self.hass.config.time_zone,
            protection=ProtectionTemperatures(qualification_seconds=0),
            outdoor_probes=(Probe(OUTDOOR, "dispatch_outside"),),
            zones={**cfg.zones, "alpha": replace(cfg.zones["alpha"], windows=(CONTACT,))},
            head_heat_call_offset_f=5, head_cool_call_offset_f=5,
            mode_hold_seconds=.001, switch_confirmation_seconds=.001,
            head_minimum_off_seconds=0, off_settle_seconds=.001,
            equipment_timing_confirmed=True, heater_setpoint_confirmed=True)
        for entity, observed in states(cfg, {"alpha": 71.1, "beta": 75.6, "gamma": 70.6},
                                       datetime.now(timezone.utc)).items():
            self.hass.states.async_set(entity, observed["state"], observed.get("attributes", {}))
        self.controller, _ = await self.runtime(cfg=cfg, entry_id="window_dispatch")
        self.hass.states.async_set(CONTACT, "on")
        self.hass.states.async_set(OUTDOOR, "51", {"unit_of_measurement": "°F"})
        await self.claim(self.controller)
        await self.settle_watchdog(self.controller.controls)
        self.head = cfg.heads["shared"]
        self.calls.clear()
        self.plan = self.make_plan()
        self.ticket = self.plan.window_authorizations["shared"]

    def snapshot(self):
        return {observed["entity_id"]: observed for observed in self.controller.api._read_states()}

    def make_plan(self, snapshot=None):
        snapshot = self.snapshot() if snapshot is None else snapshot
        now = datetime.now(timezone.utc)
        stamp = time.monotonic()
        cadence = DecisionCadence(self.controller.config)
        settings = self.controller.comfort.settings(snapshot, self.controller.config, now)
        # Advance contact confirmation and the ordinary comfort-decision gate
        # without sleeping through production's two-minute/90-second periods.
        cadence.decide(snapshot, now, settings, monotonic_now=stamp - 211)
        cadence.decide(snapshot, now, settings, monotonic_now=stamp - 91)
        plan = cadence.decide(snapshot, now + timedelta(seconds=.002), settings, monotonic_now=stamp)
        self.assertEqual(plan.mode, "cool", (plan.window_states, plan.head_constraints, plan.reasons))
        self.assertIn("shared", plan.head_targets_f)
        self.assertIn("shared", plan.window_authorization_heads)
        return plan

    async def assert_no_jobs(self):
        await self.until(lambda: self.controller.controls.service_inflight == 0)
        self.assertEqual(self.controller.controls.inflight, 0)
        self.assertFalse(self.controller.api.service_jobs({self.head}))
        self.assertFalse(self.controller.api.uncertain_service_jobs({self.head}))
        self.assertTrue(self.controller.controls.enabled)

    async def queued(self, change, *, ticket=None, accepted=False):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller.api._async_service_job

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        request = None
        with patch.object(self.controller.api, "_async_service_job", side_effect=held):
            try:
                request = asyncio.create_task(asyncio.to_thread(
                    self.controller.api.service_guarded, "climate", "set_temperature",
                    ticket=self.ticket if ticket is None else ticket, entity_id=self.head,
                    hvac_mode="cool", temperature=67))
                await asyncio.wait_for(entered.wait(), 1)
                result = change()
                if asyncio.iscoroutine(result):
                    await result
                release.set()
                if accepted:
                    await asyncio.wait_for(request, 2)
                    self.assertEqual(self.hass.states.get(self.head).state, "cool")
                    self.assertEqual(len(self.calls), 1)
                    self.assertNotIn("ticket", self.calls[0][2])
                else:
                    with self.assertRaises(HAActivationDeferred):
                        await asyncio.wait_for(request, 2)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.hass.states.get(self.head).state, "off")
                await self.assert_no_jobs()
            finally:
                release.set()
                if request and not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async def test_queued_close_cannot_dispatch_an_old_open_window_plan(self):
        await self.queued(lambda: self.hass.states.async_set(CONTACT, "off"))
        # An invalid relief proof is not a prohibition on ordinary shutdown.
        await asyncio.to_thread(self.controller.api.service_guarded,
            "climate", "set_hvac_mode", ticket=None, entity_id=self.head, hvac_mode="off")
        self.assertEqual(self.calls[-1][1], "set_hvac_mode")
        await self.assert_no_jobs()

    async def test_queued_unknown_unavailable_restored_and_assumed_contacts_defer(self):
        for value, attrs in (("unknown", {}), ("unavailable", {}),
                             ("on", {"restored": True}), ("on", {"assumed_state": True})):
            with self.subTest(value=value, attrs=attrs):
                self.hass.states.async_set(CONTACT, "on")
                self.ticket = self.make_plan().window_authorizations["shared"]
                await self.queued(lambda: self.hass.states.async_set(CONTACT, value, attrs))

    async def test_close_reopen_before_dispatch_invalidates_same_value_contact(self):
        def bounce():
            self.hass.states.async_set(CONTACT, "off")
            self.hass.states.async_set(CONTACT, "on")
        await self.queued(bounce)

    async def test_relevant_probe_crossing_hard_floor_defers_warm_and_freezing_cases(self):
        probe = self.controller.config.zones["alpha"].probes[0].entity_id
        for outside, below in (("51", "59.9"), ("20", "64.9")):
            with self.subTest(outside=outside):
                self.hass.states.async_set(OUTDOOR, outside, {"unit_of_measurement": "°F"})
                self.hass.states.async_set(probe, "71.1", {"unit_of_measurement": "°F"})
                self.ticket = self.make_plan().window_authorizations["shared"]
                await self.queued(lambda: self.hass.states.async_set(
                    probe, below, {"unit_of_measurement": "°F"}))

    async def test_probe_expiry_without_state_event_is_checked_at_actual_dispatch(self):
        probe = self.controller.config.zones["alpha"].probes[0].entity_id
        def expire():
            old = self.hass.states.get(probe)
            stamp = datetime.now(timezone.utc) - timedelta(
                seconds=self.controller.config.sensor_stale_seconds + 1)
            self.hass.states._states[probe] = State(probe, old.state, dict(old.attributes),
                last_reported=stamp, last_updated=stamp, last_changed=stamp)
        await self.queued(expire)

    async def test_redundant_missing_probe_does_not_block_other_valid_room_source(self):
        probe = self.controller.config.zones["alpha"].probes[1].entity_id
        self.hass.states.async_remove(probe)
        self.ticket = self.make_plan().window_authorizations["shared"]
        self.assertGreater(self.ticket.expires_at_monotonic, time.monotonic())
        await self.queued(lambda: None, accepted=True)

    async def test_redundant_stale_probe_does_not_expire_other_valid_room_source(self):
        probe = self.controller.config.zones["alpha"].probes[1].entity_id
        old = self.hass.states.get(probe)
        stamp = datetime.now(timezone.utc) - timedelta(
            seconds=self.controller.config.sensor_stale_seconds + 1)
        self.hass.states._states[probe] = State(probe, old.state, dict(old.attributes),
            last_reported=stamp, last_updated=stamp, last_changed=stamp)
        self.ticket = self.make_plan().window_authorizations["shared"]
        self.assertGreater(self.ticket.expires_at_monotonic, time.monotonic())
        await self.queued(lambda: None, accepted=True)

    async def test_excluded_probe_recovery_requires_a_new_plan_at_dispatch(self):
        probe = self.controller.config.zones["alpha"].probes[1].entity_id
        old = self.hass.states.get(probe)
        self.hass.states.async_remove(probe)
        self.ticket = self.make_plan().window_authorizations["shared"]
        await self.queued(lambda: self.hass.states.async_set(
            probe, old.state, dict(old.attributes)))

    async def test_loss_of_all_attached_room_sources_defers_queued_activation(self):
        def lose_sources():
            for probe in self.controller.config.zones["alpha"].probes:
                self.hass.states.async_remove(probe.entity_id)
        await self.queued(lose_sources)

    async def test_original_deadline_cannot_be_renewed_by_same_value_fresh_reports(self):
        ticket = self.ticket
        def expire_then_refresh():
            self.hass.states.async_set(CONTACT, "on")
            self.hass.states.async_set(OUTDOOR, "51", {"unit_of_measurement": "°F"})
            refreshed = self.make_plan().window_authorizations["shared"]
            self.assertGreater(refreshed.expires_at_monotonic, ticket.expires_at_monotonic)
        def expired(*args, **kwargs):
            return check_window_authorization(*args, **{**kwargs,
                "monotonic_now": ticket.expires_at_monotonic + .001})
        with patch("custom_components.house_climate.runtime.check_window_authorization",
                   side_effect=expired):
            await self.queued(expire_then_refresh, ticket=ticket)

    async def test_source_expiry_after_warm_authorization_cannot_be_healed_while_queued(self):
        old = self.hass.states.get(OUTDOOR)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=1799)
        self.hass.states._states[OUTDOOR] = State(OUTDOOR, old.state, dict(old.attributes),
            last_reported=stamp, last_updated=stamp, last_changed=stamp)
        ticket = self.make_plan().window_authorizations["shared"]
        self.assertLess(ticket.expires_at_monotonic - time.monotonic(), 1.1)
        def gap_then_same_value():
            self.hass.states.async_set(OUTDOOR, "51", {"unit_of_measurement": "°F"}, force_update=True)
        def expired(*args, **kwargs):
            return check_window_authorization(*args, **{**kwargs,
                "monotonic_now": ticket.expires_at_monotonic + .001})
        with patch("custom_components.house_climate.runtime.check_window_authorization",
                   side_effect=expired):
            await self.queued(gap_then_same_value, ticket=ticket)

    async def test_unrelated_head_mode_and_report_noise_do_not_self_invalidate(self):
        def unrelated_and_noise():
            other = self.controller.config.heads["second"]
            previous = self.hass.states.get(other)
            self.hass.states.async_set(other, "cool", dict(previous.attributes))
            self.hass.states.async_set(CONTACT, "on")
            for room in self.controller.config.zones.values():
                for probe in room.probes:
                    old = self.hass.states.get(probe.entity_id)
                    self.hass.states.async_set(probe.entity_id, old.state, dict(old.attributes))
        await self.queued(unrelated_and_noise, accepted=True)

    async def test_native_request_does_not_replace_original_inputs_with_current_closed_state(self):
        self.hass.states.async_set(CONTACT, "off")
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(self.controller.api.service_guarded,
                "climate", "set_temperature", ticket=self.ticket, entity_id=self.head,
                hvac_mode="cool", temperature=67)
        self.assertEqual(self.calls, [])
        await self.assert_no_jobs()

    async def test_missing_ticket_deferred_but_ordinary_start_and_off_remain_supported(self):
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(self.controller.api.service_guarded,
                "climate", "set_temperature", ticket=None, entity_id=self.head,
                hvac_mode="cool", temperature=67)
        self.assertEqual(self.calls, [])
        await asyncio.to_thread(self.controller.api.service, "climate", "set_temperature",
                                entity_id=self.head, hvac_mode="cool", temperature=72)
        self.assertEqual(self.hass.states.get(self.head).state, "cool")
        await asyncio.to_thread(self.controller.api.service, "climate", "set_hvac_mode",
                                entity_id=self.head, hvac_mode="off")
        self.assertEqual(self.hass.states.get(self.head).state, "off")
        await self.assert_no_jobs()

    async def test_executor_uses_original_ticket_after_successful_plan_construction(self):
        executor = Executor(self.controller.config, self.controller.api, "°F")
        executor._diagnostic_plan = self.plan
        observed = self.snapshot()
        self.hass.states.async_set(CONTACT, "off")
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(executor.ensure, self.head, "set_temperature",
                {"hvac_mode": "cool", "temperature": 67}, lambda state: False,
                observed, time.monotonic())
        self.assertFalse(executor.pending)
        self.assertFalse(executor.recoveries)
        self.assertEqual(self.calls, [])
        await self.assert_no_jobs()


if __name__ == "__main__":
    unittest.main()
