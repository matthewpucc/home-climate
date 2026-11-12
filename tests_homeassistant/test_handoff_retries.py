"""Bounded preset retries use real HA feedback and actual service lifetimes."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import math
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.house_climate import handoff
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.repair_gate import RepairGate
from tests.test_rooms import configuration


class HandoffRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        raw = configuration()
        raw.update(fallback={"heat_low_f": 68, "cool_high_f": 73, "fan_percentage": 40},
                   head_activation_timeout_seconds=120)
        self.cfg = Config.from_dict(raw)
        self.controls = NativeControls(self.hass, self.cfg, True, "synthetic-retries", lambda: None)
        # No active watchdog/listeners: every observation below is deliberate.
        self.assertFalse(self.controls.active)
        self.head, self.other = self.cfg.heads.values()
        self.fan = next(iter(self.cfg.fans.values()))
        self.wall = float(math.floor(datetime.now(timezone.utc).timestamp()))
        self.clock = patch.object(handoff, "now", side_effect=lambda: self.wall)
        self.clock.start()
        self.calls = []
        self.responses = {}
        self.default_response = "mismatch"
        self.held_entity = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        for entity in self.cfg.heads.values():
            self.report(entity, "off", at=self.wall - 1)
        for entity in self.cfg.fans.values():
            self.report(entity, "off", at=self.wall - 1, percentage=0)
        self.hass.services.async_register("climate", "set_temperature", self.handle)
        self.hass.services.async_register("fan", "turn_on", self.handle)
        self.hass.services.async_register("fan", "turn_off", self.handle)
        self.reset()

    async def asyncTearDown(self):
        self.release.set()
        jobs = list(self.controls._handoff_jobs.values())
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self.controls.handoff = None
        self.controls.owned = ()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.clock.stop()
        self.directory.cleanup()

    def reset(self, entities=None):
        self.controls.enabled = False
        self.controls.recovering = True
        self.controls.owned = tuple(entities or (self.head,))
        for entity in self.controls.owned:
            self.report(entity, "off", at=self.wall - 1)
        self.controls._uncertain_device_jobs.clear()
        self.controls.handoff = handoff.arm(self.controls, self.controls.owned)
        self.controls.handoff["stage"] = "applying"
        self.plan = self.controls.handoff
        self.calls.clear()
        self.responses.clear()

    def report(self, entity, mode="heat_cool", *, at=None, correct=False, attrs=None,
               context=None, percentage=None, force_update=True):
        attributes = {"hvac_modes": ["off", "heat", "cool", "heat_cool"],
                      "target_temp_low": 68 if correct else 70,
                      "target_temp_high": 73 if correct else 77}
        if entity.startswith("fan."):
            attributes = {"percentage": (40 if correct else 20) if percentage is None else percentage,
                          "supported_features": 1 | 16 | 32, "percentage_step": 10}
            if mode == "heat_cool":
                mode = "on"
        attributes.update(attrs or {})
        self.hass.states.async_set(entity, mode, attributes, force_update=force_update,
                                   context=context, timestamp=self.wall if at is None else at)

    async def handle(self, call):
        entity = call.data["entity_id"]
        self.calls.append((call.domain, call.service, dict(call.data)))
        if entity == self.held_entity:
            self.entered.set()
            await self.release.wait()
        response = self.responses.get(entity, [])
        response = response.pop(0) if response else self.default_response
        self.wall += 1
        if response == "error":
            raise RuntimeError("Synthetic preset service failed")
        if response == "rejected":
            raise ServiceValidationError("Synthetic provider rejected Auto")
        if response == "off":
            self.fresh_off()
            return
        if response != "no_report":
            self.report(entity, correct=response == "correct")

    async def step(self, at=None):
        if at is not None:
            self.wall = at
        await handoff.step(self.controls)
        # Service task done callbacks publish accounting after task completion.
        await asyncio.sleep(0)

    def request(self, entity=None):
        return self.plan["requests"][entity or self.head]

    def count(self, entity=None):
        return sum(call[2]["entity_id"] == (entity or self.head) for call in self.calls)

    async def first(self, entity=None):
        if entity is not None:
            self.reset((entity,))
        await self.step()
        self.assertEqual(self.count(entity), 1)
        request = self.request(entity)
        self.assertEqual(request["attempts"], 1)
        self.assertTrue(request["dispatched"])
        self.assertEqual(self.controls.service_inflight, 0)
        return request

    async def test_fresh_mismatch_is_corrected_by_second_attempt_and_completion_releases_ownership(self):
        self.responses[self.head] = ["mismatch", "correct"]
        initial = await self.first()
        await self.step(initial["finished_at"] + 15)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.request()["attempts"], 2)
        self.assertEqual(self.request()["first_at"], initial["first_at"])
        self.assertEqual(self.hass.states.get(self.head).state, "heat_cool")
        self.assertEqual(self.hass.states.get(self.head).attributes["target_temp_low"], 68)
        self.assertEqual(self.hass.states.get(self.head).attributes["target_temp_high"], 73)
        await self.step()
        self.assertIsNone(self.controls.handoff)
        self.assertEqual(self.controls.owned, ())
        self.assertEqual(self.controls.service_inflight, 0)

    async def test_notification_disposition_uses_actual_retry_evidence(self):
        initial = await self.first()
        self.plan["problems"][self.head] = "Retryable mismatch"
        self.wall = initial["finished_at"] + 1
        self.report(self.head, at=self.wall)
        self.assertEqual(handoff.notification_disposition(self.controls)[self.head], "recovering")
        self.plan["requests"][self.head]["dispatched"] = False
        self.assertEqual(handoff.notification_disposition(self.controls)[self.head], "human_required")
        self.plan["requests"][self.head]["dispatched"] = True
        self.plan["requests"][self.head]["data"]["target_temp_low"] = 55
        self.assertEqual(handoff.notification_disposition(self.controls)[self.head], "human_required")

    async def test_reserved_retry_publication_does_not_alert(self):
        initial = await self.first()
        self.plan["problems"][self.head] = "Prior retryable mismatch"
        observations = []
        original = handoff._save
        async def capture(controls):
            if controls.handoff and controls.handoff["requests"].get(self.head, {}).get("attempts") == 2:
                observations.append(handoff.notification_disposition(controls).get(self.head))
            await original(controls)
        with patch.object(handoff, "_save", side_effect=capture):
            await self.step(initial["finished_at"] + 15)
        self.assertIn("recovering", observations)
        self.assertNotIn("human_required", observations)
        self.assertFalse(self.request()["automatic_retry_pending"])

    async def test_final_attempt_waits_for_feedback_but_not_forever(self):
        initial = await self.first()
        self.plan["problems"][self.head] = "Mismatch"
        request = self.request()
        request["attempts"] = 3
        self.wall = request["finished_at"] + 1
        self.report(self.head, at=self.wall)
        self.assertEqual(handoff.notification_disposition(self.controls)[self.head], "recovering")
        self.wall = request["finished_at"] + 15
        self.assertEqual(handoff.notification_disposition(self.controls)[self.head], "human_required")

    async def first_off(self):
        self.default_response = "off"
        return await self.first()

    def fresh_off(self, *, at=None, attrs=None, context=None):
        self.report(self.head, "off", at=at, context=context, force_update=False, attrs={
            "target_temp_low": None, "target_temp_high": None, **(attrs or {})})

    async def test_mode_then_range_retry_share_budget_and_original_deadline(self):
        self.responses[self.head] = ["off", "mismatch", "correct"]
        first = await self.first()
        self.wall = first["finished_at"] + 15
        self.fresh_off()
        await self.step()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.calls[-1][1], "set_temperature")
        self.assertEqual(self.calls[-1][2], {
            "entity_id": self.head, "hvac_mode": "heat_cool",
            "target_temp_low": 68, "target_temp_high": 73})
        await self.step(self.request()["finished_at"] + 15)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.request()["first_at"], first["first_at"])
        await self.step()
        self.assertIsNone(self.controls.handoff)
        self.assertFalse(self.controls.owned)

    async def test_off_mode_retries_exhaust_at_three_but_late_success_can_complete(self):
        first = await self.first_off()
        for _ in range(4):
            self.wall += 16
            self.fresh_off()
            await self.step()
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.request()["first_at"], first["first_at"])
        await self.step(first["first_at"] + 121)
        self.assertIn("automatic retries exhausted", self.plan["problems"][self.head])
        self.report(self.head, correct=True)
        await self.step()
        self.assertIsNone(self.controls.handoff)
        self.assertEqual(self.count(), 3)

    async def test_off_mode_retry_requires_report_after_handler_and_fifteen_second_spacing(self):
        first = await self.first_off()
        await self.step(first["finished_at"] + 15)
        self.assertEqual(self.count(), 1)  # Handler's own Off report is insufficient.
        self.wall = first["finished_at"] + 14
        self.fresh_off()
        await self.step()
        self.assertEqual(self.count(), 1)
        await self.step(first["finished_at"] + 15)
        self.assertEqual(self.count(), 2)

    async def test_off_mode_retry_rejects_untrusted_evidence_and_post_activation_off(self):
        for case in ("old", "future", "restored", "assumed_state", "returned_off", "operator"):
            with self.subTest(case=case):
                self.wall += 200
                self.reset()
                first = await self.first_off()
                self.wall = first["finished_at"] + 15
                if case == "returned_off":
                    self.report(self.head)
                    self.wall += 1
                self.fresh_off(at=first["at"] if case == "old" else self.wall + 1 if case == "future" else None,
                               attrs={case: True} if case in ("restored", "assumed_state") else None,
                               context=Context(user_id="synthetic_operator") if case == "operator" else None)
                await self.step()
                self.assertEqual(self.count(), 1)

    async def test_off_mode_retry_preserves_uncertain_rejected_and_restart_vetoes(self):
        for case in ("uncertain", "cancelled_before_dispatch", "rejected", "service_failed", "interrupted"):
            with self.subTest(case=case):
                self.wall += 200
                self.reset()
                first = await self.first_off()
                self.wall = first["finished_at"] + 15
                self.fresh_off()
                if case == "interrupted":
                    self.controls.handoff = handoff.restored(deepcopy(self.plan))
                    self.plan = self.controls.handoff
                else:
                    first[case] = True
                await self.step()
                self.assertEqual(self.count(), 1)

    async def test_off_mode_retry_requires_current_compatible_reports_from_all_configured_heads(self):
        for mode, attrs, age in (
            ("heat", {}, 0), ("cool", {}, 0), ("dry", {}, 0), ("fan_only", {}, 0),
            ("unknown", {}, 0), ("unavailable", {}, 0), ("off", {"restored": True}, 0),
            ("heat_cool", {"assumed_state": True}, 0), ("off", {}, 181), ("off", {}, -1),
        ):
            with self.subTest(mode=mode, attrs=attrs, age=age):
                self.wall += 200
                self.reset()
                first = await self.first_off()
                self.wall = first["finished_at"] + 15
                self.fresh_off()
                self.report(self.other, mode, at=self.wall - age, attrs=attrs)
                await self.step()
                self.assertEqual(self.count(), 1)
                self.assertEqual(self.request()["attempts"], 1)
        self.report(self.other, "heat_cool", correct=True)
        await self.step()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.count(self.other), 0)

    async def test_off_mode_retry_waits_for_peer_jobs_uncertainty_and_repair(self):
        first = await self.first_off()
        self.wall = first["finished_at"] + 15
        self.fresh_off()
        self.controls.repair_gate = RepairGate(self.cfg.heads.values())
        pending = self.hass.loop.create_future()
        for case in ("stop", "pending", "handoff", "uncertain", "repair"):
            with self.subTest(case=case):
                if case == "stop":
                    self.controls._stop_jobs[pending] = self.other
                elif case == "pending":
                    self.controls.pending_device_jobs = lambda entities: (pending,) if self.other in entities else ()
                elif case == "handoff":
                    self.controls._handoff_jobs[self.other] = pending
                elif case == "uncertain":
                    self.controls._uncertain_device_jobs.add(self.other)
                else:
                    lease = self.controls.repair_gate.begin("synthetic_owner", (self.other,))
                await self.step()
                self.assertEqual(self.count(), 1)
                self.controls._stop_jobs.clear()
                self.controls.pending_device_jobs = lambda entities: ()
                self.controls._handoff_jobs.pop(self.other, None)
                self.controls._uncertain_device_jobs.clear()
                if case == "repair":
                    self.controls.repair_gate.release(lease)
        pending.set_result(None)
        await self.step()
        self.assertEqual(self.count(), 2)

    def kumo_provider(self, profile):
        cls = type("SyntheticKumo", (), {"__module__": "custom_components.kumo.climate"})
        entity = cls()
        entity.available = True
        entity._pykumo = SimpleNamespace(_profile=profile)
        self.hass.data[DATA_INSTANCES] = {"climate": SimpleNamespace(
            get_entity=lambda entity_id: entity if entity_id == self.head else None)}
        return entity

    async def test_kumo_profile_rejection_blocks_initial_combined_write_despite_advertised_auto(self):
        self.kumo_provider({"hasModeAuto": False})
        await self.step()
        self.assertEqual(self.count(), 0)
        self.assertNotIn(self.head, self.plan["requests"])
        self.assertIn("rejected by the current Kumo profile", self.plan["problems"][self.head])

    async def test_unknown_kumo_profile_and_missing_native_entity_do_not_fall_back_to_cached_modes(self):
        for profile in (None, [], {"hasModeAuto": "true"}, {"hasModeAuto": 1}):
            with self.subTest(profile=profile):
                self.kumo_provider(profile)
                await self.step()
                self.assertEqual(self.count(), 0)
                self.assertIn("unverified", self.plan["problems"][self.head])
        provider = self.kumo_provider({"hasModeAuto": True})
        provider.available = False
        await self.step()
        self.assertEqual(self.count(), 0)
        # HA 2026.11 registry loading waits for initialized config entries.
        from homeassistant.config_entries import ConfigEntries
        if self.hass.config_entries is None:
            self.hass.config_entries = ConfigEntries(self.hass, {})
            await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        registry = er.async_get(self.hass)
        self.hass.states.async_remove(self.head)
        registry.async_get_or_create("climate", "kumo", "synthetic-head", suggested_object_id=self.head.split(".")[1])
        self.report(self.head, "off")
        self.assertEqual(registry.async_get(self.head).platform, "kumo")
        self.hass.data[DATA_INSTANCES].pop("climate")
        await self.step()
        self.assertEqual(self.count(), 0)
        self.assertIn("unverified", self.plan["problems"][self.head])

    async def test_provider_profile_loss_during_initial_persistence_blocks_all_writes(self):
        provider = self.kumo_provider({"hasModeAuto": True})
        original = self.controls.store.async_save

        async def save(data):
            await original(data)
            provider._pykumo._profile = {"hasModeAuto": False}

        with patch.object(self.controls.store, "async_save", side_effect=save):
            await self.step()
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.request().get("dispatched"))
        self.assertIn("Kumo profile", self.plan["problems"][self.head])

    async def test_kumo_profile_rejection_blocks_off_retry_until_support_returns_within_same_budget(self):
        provider = self.kumo_provider({"hasModeAuto": True})
        first = await self.first_off()
        provider._pykumo._profile = {}  # Missing flag is rejection in pykumo too.
        self.wall = first["finished_at"] + 15
        self.fresh_off()
        await self.step()
        self.assertEqual(self.count(), 1)
        self.assertIn("rejected by the current Kumo profile", self.plan["problems"][self.head])
        provider._pykumo._profile = {"hasModeAuto": True}
        self.default_response = "correct"
        await self.step()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.request()["first_at"], first["first_at"])
        await self.step()
        self.assertIsNone(self.controls.handoff)

    async def test_explicit_validation_rejection_cannot_authorize_retry_from_fresh_off(self):
        self.responses[self.head] = ["rejected"]
        first = await self.first()
        self.wall = first["finished_at"] + 15
        self.fresh_off()
        await self.step()
        self.assertTrue(first["rejected"])
        self.assertEqual(self.count(), 1)
        self.assertIn("command was rejected", self.plan["problems"][self.head])

    async def test_fifteen_second_spacing_starts_after_actual_service_completion(self):
        request = await self.first()
        await self.step(request["finished_at"] + 14)
        self.assertEqual(self.count(), 1)
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 2)

    async def test_retry_budget_caps_three_actual_service_attempts(self):
        first = await self.first()
        for _ in range(2):
            await self.step(self.request()["finished_at"] + 15)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.request()["attempts"], 3)
        self.assertEqual(self.request()["first_at"], first["first_at"])
        for elapsed in (60, 90, 119):
            await self.step(first["first_at"] + elapsed)
        self.assertEqual(self.count(), 3)
        self.assertIn(self.head, self.controls.owned)

    async def test_later_retry_does_not_restart_original_120_second_feedback_deadline(self):
        first = await self.first()
        await self.step(first["first_at"] + 105)
        self.assertEqual(self.count(), 2)
        second = self.request()
        self.assertEqual(second["first_at"], first["first_at"])
        await self.step(first["first_at"] + 121)
        self.assertEqual(self.count(), 2)
        self.assertIn("unconfirmed", self.plan["problems"][self.head])

    async def test_unknown_unavailable_old_restored_assumed_or_changed_mode_reports_do_not_retry(self):
        for mode, attrs, old in (
            ("unknown", {}, False), ("unavailable", {}, False),
            ("heat_cool", {}, True), ("heat_cool", {"restored": True}, False),
            ("heat_cool", {"assumed_state": True}, False),
            ("off", {}, False), ("heat", {}, False), ("cool", {}, False),
        ):
            with self.subTest(mode=mode, attrs=attrs, old=old):
                self.wall += 200
                self.reset()
                first = await self.first()
                self.wall = first["finished_at"] + 15
                self.report(self.head, mode, attrs=attrs,
                            at=first["at"] if old else self.wall)
                await self.step()
                self.assertEqual(self.count(), 1)
                self.assertIn(self.head, self.controls.owned)

    async def test_future_dated_report_is_not_current_retry_evidence(self):
        first = await self.first()
        self.wall = first["finished_at"] + 15
        self.report(self.head, at=self.wall + 300)
        await self.step()
        self.assertEqual(self.count(), 1)

    async def test_invalid_or_missing_head_ranges_never_authorize_a_retry(self):
        invalid_ranges = ((None, 73), (True, 73), ("invalid", 73),
                          (math.nan, 73), (math.inf, 73), (73, 68),
                          (68, None), (68, False), (68, math.nan),
                          (68, math.inf), ("missing", 73), (68, "missing"))
        for low, high in invalid_ranges:
            with self.subTest(low=low, high=high):
                self.wall += 200
                self.reset()
                request = await self.first()
                self.wall = request["finished_at"] + 15
                self.report(self.head, attrs={"target_temp_low": low, "target_temp_high": high})
                state = self.hass.states.get(self.head)
                attrs = dict(state.attributes)
                for key, value in (("target_temp_low", low), ("target_temp_high", high)):
                    if value == "missing":
                        attrs.pop(key)
                self.hass.states.async_set(self.head, "heat_cool", attrs,
                                           force_update=True, timestamp=self.wall)
                await self.step()
                self.assertEqual(self.count(), 1)
                self.assertEqual(self.request()["attempts"], 1)
                self.assertIn(self.head, self.controls.owned)

    async def test_collapsed_but_finite_head_range_can_receive_bounded_retry(self):
        request = await self.first()
        self.wall = request["finished_at"] + 15
        self.report(self.head, attrs={"target_temp_low": 73, "target_temp_high": 73})
        await self.step()
        self.assertEqual(self.count(), 2)

    async def test_invalid_or_missing_running_fan_percentage_never_authorizes_retry(self):
        for percentage in (None, True, "invalid", math.nan, math.inf, -1, 101, "missing"):
            with self.subTest(percentage=percentage):
                self.wall += 200
                self.reset()
                request = await self.first(self.fan)
                self.wall = request["finished_at"] + 15
                self.report(self.fan, attrs={"percentage": percentage})
                if percentage == "missing":
                    attrs = dict(self.hass.states.get(self.fan).attributes)
                    attrs.pop("percentage")
                    self.hass.states.async_set(self.fan, "on", attrs,
                                               force_update=True, timestamp=self.wall)
                await self.step()
                self.assertEqual(self.count(self.fan), 1)
                self.assertEqual(self.request(self.fan)["attempts"], 1)

    async def test_available_fan_off_without_speed_still_provides_valid_mismatch(self):
        request = await self.first(self.fan)
        self.wall = request["finished_at"] + 15
        state = self.hass.states.get(self.fan)
        attrs = {key: value for key, value in state.attributes.items() if key != "percentage"}
        self.hass.states.async_set(self.fan, "off", attrs, force_update=True, timestamp=self.wall)
        await self.step()
        self.assertEqual(self.count(self.fan), 2)

    async def test_invalid_running_fan_state_does_not_authorize_retry(self):
        request = await self.first(self.fan)
        self.wall = request["finished_at"] + 15
        self.report(self.fan, "invalid", percentage=20)
        await self.step()
        self.assertEqual(self.count(self.fan), 1)
        self.assertEqual(self.request(self.fan)["attempts"], 1)

    async def test_operator_edit_and_later_integration_echo_cannot_resume_automatic_retries(self):
        first = await self.first()
        self.wall = first["finished_at"] + 15
        self.report(self.head, context=Context(user_id="synthetic_operator"))
        await self.step()
        self.assertEqual(self.count(), 1)
        self.assertIn("Operator changed", self.plan["problems"][self.head])
        self.wall += 1
        self.report(self.head)  # A later integration report cannot erase the veto.
        await self.step()
        self.assertEqual(self.count(), 1)

    async def coalesced_operator_echo(self, *, correct=False):
        request = await self.first()
        self.wall = request["finished_at"] + 15
        busy = asyncio.create_task(asyncio.Event().wait())
        self.controls._task = busy
        self.controls.active = True
        observed = []

        def changed(event):
            observed.append(event.data["new_state"].context.user_id)
            self.controls._schedule_check(event)

        remove = async_track_state_change_event(self.hass, (self.head,), changed)
        try:
            self.report(self.head, correct=correct, context=Context(user_id="synthetic_operator"))
            self.wall += 1
            self.report(self.head, correct=correct)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertIn("synthetic_operator", observed)
            self.assertIsNone(self.hass.states.get(self.head).context.user_id)
            self.assertTrue(request["operator_changed"])
        finally:
            self.controls.active = False
            remove()
            busy.cancel()
            await asyncio.gather(busy, return_exceptions=True)
            self.controls._task = None

    async def test_operator_event_is_not_lost_when_watchdog_busy_and_integration_echo_replaces_context(self):
        await self.coalesced_operator_echo()
        await self.step()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.request()["attempts"], 1)
        self.assertIn("Operator changed", self.plan["problems"][self.head])
        self.assertIn(self.head, self.controls.owned)

    async def test_matching_operator_correction_completes_even_when_intervention_was_latched(self):
        await self.coalesced_operator_echo(correct=True)
        await self.step()
        self.assertEqual(self.count(), 1)
        self.assertIsNone(self.controls.handoff)
        self.assertEqual(self.controls.owned, ())

    async def test_uncertain_or_cancelled_request_cannot_grant_another_write(self):
        for flag in ("uncertain", "cancelled_before_dispatch", "uncertain_device"):
            with self.subTest(flag=flag):
                self.wall += 200
                self.reset()
                request = await self.first()
                if flag == "uncertain_device":
                    self.controls._uncertain_device_jobs.add(self.head)
                else:
                    request[flag] = True
                await self.step(request["finished_at"] + 15)
                self.assertEqual(self.count(), 1)

    async def test_pending_external_device_job_and_stop_job_both_block_retry(self):
        request = await self.first()
        pending = self.hass.loop.create_future()
        self.controls.pending_device_jobs = lambda entities: (pending,) if self.head in entities else ()
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 1)
        self.controls.pending_device_jobs = lambda entities: ()
        self.controls._stop_jobs[pending] = self.head
        await self.step()
        self.assertEqual(self.count(), 1)
        self.controls._stop_jobs.clear()
        pending.set_result(None)

    async def test_running_service_cannot_overlap_a_retry_even_after_a_mismatching_report(self):
        self.held_entity = self.head
        checking = asyncio.create_task(self.step())
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            request = self.request()
            self.wall = request["at"] + 30
            self.report(self.head)
            await self.step()
            self.assertEqual(self.count(), 1)
            self.assertEqual(self.controls.service_inflight, 1)
            self.assertFalse(self.controls._handoff_jobs[self.head].done())
            self.release.set()
            await asyncio.wait_for(checking, 2)
            finished = self.request()["finished_at"]
            self.held_entity = None
            await self.step(finished + 14)
            self.assertEqual(self.count(), 1)
            await self.step(finished + 15)
            self.assertEqual(self.count(), 2)
        finally:
            self.release.set()
            await asyncio.gather(checking, return_exceptions=True)

    async def test_actual_cancellation_after_dispatch_retains_uncertainty_and_blocks_retry(self):
        self.held_entity = self.head
        checking = asyncio.create_task(self.step())
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            job = self.controls._handoff_jobs[self.head]
            self.assertTrue(self.request()["dispatched"])
            job.cancel()
            await asyncio.gather(job, checking, return_exceptions=True)
            await asyncio.sleep(0)
            request = self.request()
            self.assertTrue(request["uncertain"])
            self.assertIn(self.head, self.controls._uncertain_device_jobs)
            self.wall = request["finished_at"] + 15
            self.report(self.head)
            await self.step()
            self.assertEqual(self.count(), 1)
            self.assertIn(self.head, self.controls.owned)
        finally:
            self.release.set()
            await asyncio.gather(checking, return_exceptions=True)

    async def test_report_after_reservation_but_before_actual_dispatch_cannot_grant_retry(self):
        self.default_response = "no_report"
        queued, release = asyncio.Event(), asyncio.Event()
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name == "Home Climate recovery setting":
                async def held():
                    queued.set()
                    await release.wait()
                    return await coroutine
                return original(held(), name, **kwargs)
            return original(coroutine, name, **kwargs)

        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            checking = asyncio.create_task(self.step())
            try:
                await asyncio.wait_for(queued.wait(), 1)
                reserved = self.request()["at"]
                self.wall += 5
                self.report(self.head)
                earlier_report = self.hass.states.get(self.head).last_reported.timestamp()
                self.wall += 5
                release.set()
                await checking
            finally:
                release.set()
                await asyncio.gather(checking, return_exceptions=True)
        request = self.request()
        self.assertGreater(earlier_report, reserved)
        self.assertLess(earlier_report, request["dispatched_at"])
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 1)
        self.report(self.head)
        await self.step()
        self.assertEqual(self.count(), 2)

    async def test_capability_removed_during_retry_reservation_save_blocks_dispatch(self):
        request = await self.first()
        original = self.controls.store.async_save
        changed = False

        async def save(data):
            nonlocal changed
            await original(data)
            if self.request()["attempts"] == 2 and not changed:
                changed = True
                self.wall += 1
                self.report(self.head, attrs={"hvac_modes": ["off", "heat"]})

        with patch.object(self.controls.store, "async_save", side_effect=save):
            await self.step(request["finished_at"] + 15)
        self.assertTrue(changed)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.request()["attempts"], 2)
        self.assertFalse(self.request().get("dispatched"))
        self.assertEqual(self.controls.service_inflight, 0)

    async def test_service_exception_requires_fresh_mismatching_feedback_before_retry(self):
        self.responses[self.head] = ["error"]
        request = await self.first()
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 1)
        self.wall += 1
        self.report(self.head)
        await self.step()
        self.assertEqual(self.count(), 2)

    async def test_completed_heads_and_fans_are_not_replayed_while_another_head_retries(self):
        self.reset((self.head, self.other, self.fan))
        self.responses[self.other] = ["correct"]
        self.responses[self.fan] = ["correct"]
        await self.step()
        await self.step()
        self.assertIn(self.other, self.plan["completed"])
        self.assertIn(self.fan, self.plan["completed"])
        for _ in range(2):
            await self.step(self.request()["finished_at"] + 15)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count(self.other), 1)
        self.assertEqual(self.count(self.fan), 1)

    async def test_fan_mismatch_uses_same_bounded_retry_and_completion_contract(self):
        request = await self.first(self.fan)
        self.responses[self.fan] = ["correct"]
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(self.fan), 2)
        self.assertEqual(self.hass.states.get(self.fan).attributes["percentage"], 40)
        await self.step()
        self.assertIsNone(self.controls.handoff)

    async def test_persistence_failure_prevents_retry_dispatch(self):
        request = await self.first()
        with patch.object(self.controls.store, "async_save", side_effect=OSError("synthetic disk full")):
            with self.assertRaisesRegex(OSError, "synthetic disk full"):
                await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.controls.service_inflight, 0)
        self.assertTrue(self.controls.owned)

    async def test_interrupted_or_restored_handoff_never_automatically_replays_requests(self):
        request = await self.first()
        self.controls.handoff = handoff.restored(deepcopy(self.plan))
        self.plan = self.controls.handoff
        await self.step(request["finished_at"] + 15)
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.plan["interrupted"])

    async def queued_retry(self, change, *, off=False):
        first = await (self.first_off() if off else self.first())
        self.wall = first["finished_at"] + 15
        if off:
            self.fresh_off()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name != "Home Climate recovery setting":
                return original(coroutine, name, **kwargs)

            async def held():
                entered.set()
                await release.wait()
                return await coroutine

            return original(held(), name, **kwargs)

        checking = None
        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            try:
                checking = asyncio.create_task(self.step())
                await asyncio.wait_for(entered.wait(), 1)
                change()
                release.set()
                await asyncio.wait_for(checking, 2)
                self.assertEqual(self.count(), 1)
                self.assertEqual(self.controls.service_inflight, 0)
            finally:
                release.set()
                if checking is not None:
                    await asyncio.gather(checking, return_exceptions=True)

    async def test_controller_on_revokes_a_retry_queued_under_disabled_ownership(self):
        await self.queued_retry(lambda: setattr(self.controls, "enabled", True))

    async def test_peer_direction_change_revokes_queued_off_mode_retry(self):
        await self.queued_retry(lambda: self.report(self.other, "heat"), off=True)

    async def test_peer_repair_epoch_change_revokes_queued_off_mode_retry(self):
        self.controls.repair_gate = RepairGate(self.cfg.heads.values())

        def repaired():
            lease = self.controls.repair_gate.begin("synthetic_owner", (self.other,))
            self.controls.repair_gate.release(lease)

        await self.queued_retry(repaired, off=True)

    async def test_controller_on_revokes_queued_off_mode_retry(self):
        await self.queued_retry(lambda: setattr(self.controls, "enabled", True), off=True)

    async def test_new_uncertainty_revokes_queued_off_mode_retry(self):
        await self.queued_retry(lambda: self.controls._uncertain_device_jobs.add(self.head), off=True)

    async def test_provider_profile_change_revokes_queued_off_mode_retry(self):
        provider = self.kumo_provider({"hasModeAuto": True})
        await self.queued_retry(lambda: provider._pykumo._profile.update(hasModeAuto=False), off=True)
        self.assertIn("Kumo profile", self.plan["problems"][self.head])

    async def test_late_success_revokes_queued_off_mode_retry(self):
        await self.queued_retry(lambda: self.report(self.head, correct=True), off=True)

    async def test_original_deadline_revokes_queued_off_mode_retry(self):
        await self.queued_retry(lambda: setattr(self, "wall", self.request()["first_at"] + 121), off=True)

    async def test_rotated_generation_revokes_queued_retry(self):
        await self.queued_retry(lambda: setattr(self.controls, "ownership_generation", 1))

    async def test_deadline_expiry_while_retry_is_queued_prevents_dispatch(self):
        await self.queued_retry(lambda: setattr(self, "wall", self.request()["first_at"] + 121))

    async def test_new_uncertainty_while_retry_is_queued_prevents_dispatch(self):
        await self.queued_retry(lambda: self.controls._uncertain_device_jobs.add(self.head))

    async def test_late_matching_report_cancels_queued_retry_write(self):
        def correct():
            self.wall += 1
            self.report(self.head, correct=True)

        await self.queued_retry(correct)
        await self.step()
        self.assertIsNone(self.controls.handoff)

    async def test_repair_epoch_change_revokes_queued_retry_even_after_repair_releases(self):
        self.controls.repair_gate = RepairGate(self.cfg.heads.values())

        def repaired():
            lease = self.controls.repair_gate.begin("synthetic_owner", (self.head,))
            self.controls.repair_gate.release(lease)

        await self.queued_retry(repaired)

    async def test_changed_mode_or_operator_edit_while_retry_queued_prevents_dispatch(self):
        def changed():
            self.wall += 1
            self.report(self.head, "off", context=Context(user_id="synthetic_operator"))

        await self.queued_retry(changed)

    async def test_integration_mode_change_without_operator_context_revokes_queued_retry(self):
        def changed():
            self.wall += 1
            self.report(self.head, "off")

        await self.queued_retry(changed)

    async def test_held_integration_repair_blocks_retry_before_reservation(self):
        self.controls.repair_gate = RepairGate(self.cfg.heads.values())
        request = await self.first()
        lease = self.controls.repair_gate.begin("synthetic_owner", (self.head,))
        try:
            await self.step(request["finished_at"] + 15)
            self.assertEqual(self.count(), 1)
            self.assertEqual(self.request()["attempts"], 1)
        finally:
            self.controls.repair_gate.release(lease)

    async def test_malformed_retry_bookkeeping_never_authorizes_another_write(self):
        invalid = (("attempts", True), ("attempts", 0), ("attempts", 4),
                   ("attempts", 1.5), ("first_at", math.nan),
                   ("first_at", None), ("at", None), ("at", math.nan),
                   ("dispatched_at", True), ("finished_at", math.inf),
                   ("finished_at", "old"))
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.wall += 200
                self.reset()
                request = await self.first()
                self.wall = request["finished_at"] + 15
                request[key] = value
                await self.step()
                self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
