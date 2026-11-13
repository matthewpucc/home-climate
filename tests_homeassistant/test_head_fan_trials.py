"""Opt-in trial journal/dispatch boundary, using synthetic states only."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from custom_components.house_climate.head_fan_trials import HeadFanTrials
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.ha import HAError, HAActivationDeferred
from tests.test_rooms import configuration


class TrialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.hass.config.time_zone = "America/Los_Angeles"
        self.config = Config.from_dict(configuration())
        self.entity = self.config.heads["second"]
        self.controls = SimpleNamespace(enabled=True, owned={self.entity}, command_generation=Mock(return_value=7),
                                        check_activation=Mock())
        self.runtime = SimpleNamespace(hass=self.hass, config=self.config, controls=self.controls,
             thermal_storage=SimpleNamespace(diagnostics={"status":"recording", "last_commit":time.time(), "queue_depth":0}, observe=Mock()),
             live=True, stop_event=threading.Event(), wake_event=threading.Event(), api=SimpleNamespace())
        self.manager = HeadFanTrials(self.runtime, "synthetic")
        self.manager.store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock(return_value=None))
        self.plan = SimpleNamespace(mode="cool", head_targets_f={"second":72}, inhibited_heads={}, rooms={"gamma":{"sources":2}})
        self.state("auto")
        def request(path, body, **kwargs):
            self.manager.validate_dispatch(kwargs["fan_trial_token"], body["entity_id"], body["fan_mode"])
            return []
        self.runtime.api.request = Mock(side_effect=request)

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    def state(self, fan, hvac="cool"):
        self.hass.states.async_set(self.entity, hvac,
            {"fan_mode":fan,"fan_modes":["auto","low"], "hvac_modes":["off","heat","cool"]})

    async def enroll(self, **kw):
        data = dict(entity_id=self.entity,room_ids=["gamma"],mode="low",duration_minutes=20,
                    sleep_sensitive=False,overnight_authorized=True)
        data.update(kw)
        return await self.manager.async_enroll(**data)

    async def tick(self, **kw):
        args = dict(pending=False,safe=True,plan=self.plan)
        args.update(kw)
        await self.manager.async_tick(**args)

    async def test_enrollment_does_not_dispatch(self):
        await self.enroll()
        self.runtime.api.request.assert_not_called()
        self.assertEqual(self.manager.trial.stage,"enrolled")
        self.assertEqual(self.manager.diagnostics["physical_confirmation"],"unknown")

    async def test_reserves_durably_before_single_call(self):
        await self.enroll()
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"active")
        writes = [call.args[0]["stage"] for call in self.manager.store.async_save.call_args_list]
        self.assertEqual(writes,["enrolled","request_reserved","active"])
        await self.tick()
        self.runtime.api.request.assert_called_once()
        self.assertEqual(self.manager.command.result.physical_confirmation,"unknown")

    async def test_hvac_pending_and_unsafe_have_priority(self):
        await self.enroll()
        await self.tick(pending=True)
        await self.tick(safe=False)
        self.runtime.api.request.assert_not_called()

    async def test_no_forced_demand_or_single_probe(self):
        await self.enroll()
        self.plan.head_targets_f = {}
        await self.tick()
        self.plan.head_targets_f = {"second":72}
        self.plan.rooms["gamma"]["sources"] = 1
        await self.tick()
        self.runtime.api.request.assert_not_called()

    async def test_expired_enrollment_never_dispatches(self):
        await self.enroll()
        self.manager.trial = replace(self.manager.trial, starts_at=time.time()-100, ends_at=time.time()-1)
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"cancelled")
        self.runtime.api.request.assert_not_called()

    async def test_cancel_restores_once_never_claims_physical(self):
        await self.enroll()
        await self.tick()
        self.state("low")
        await self.manager.async_cancel()
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"restore_reserved")
        self.assertEqual(self.runtime.api.request.call_args.args[1]["fan_mode"],"auto")
        await self.tick()
        self.assertEqual(self.runtime.api.request.call_count,2)
        self.manager._restore_started = time.time()-61
        self.manager._restore_monotonic = time.monotonic()-61
        await self.tick()
        self.assertEqual(self.manager.trial.reason,"restoration_report_unconfirmed")

    async def test_successful_restoration_report_is_still_unverified(self):
        await self.enroll()
        await self.tick()
        await self.manager.async_cancel()
        await self.tick()
        self.manager._restore_started = time.time()-11
        self.manager._restore_monotonic = time.monotonic()-11
        self.state("low")
        self.state("auto")
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"complete")
        self.assertEqual(self.manager.trial.reason,"baseline_reported_physical_unverified")

    async def test_service_exception_no_retries(self):
        await self.enroll()
        self.runtime.api.request.side_effect = HAError("uncertain")
        await self.tick()
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"needs_attention")
        self.runtime.api.request.assert_called_once()

    async def test_generation_change_while_save_awaiting_blocks_dispatch(self):
        await self.enroll()
        async def save(data):
            if data["stage"] == "request_reserved":
                self.controls.command_generation.return_value = 8
        self.manager.store.async_save.side_effect = save
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"needs_attention")
        self.assertEqual(self.manager.trial.reason,"request_outcome_uncertain")

    async def test_journal_failure_blocks_service(self):
        await self.enroll()
        self.manager.store.async_save.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            await self.tick()
        self.runtime.api.request.assert_not_called()
        self.assertIsNotNone(self.manager.error)

    async def test_manual_report_no_overwrite(self):
        await self.enroll()
        self.state("low")
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"cancelled")
        self.runtime.api.request.assert_not_called()

    async def test_restart_never_replays(self):
        await self.enroll()
        await self.tick()
        saved = self.manager.store.async_save.call_args.args[0]
        restarted = HeadFanTrials(self.runtime,"synthetic")
        restarted.store = SimpleNamespace(async_save=AsyncMock(),async_load=AsyncMock(return_value=saved))
        await restarted.async_load()
        await restarted.async_tick(pending=False,safe=True,plan=self.plan)
        self.assertEqual(restarted.trial.stage,"needs_attention")
        self.runtime.api.request.assert_called_once()

    async def test_complete_shared_room_scope_required(self):
        self.entity = self.config.heads["shared"]
        self.controls.owned.add(self.entity)
        self.state("auto")
        with self.assertRaisesRegex(ValueError,"every room"):
            await self.enroll(entity_id=self.entity,room_ids=["alpha"])

    async def test_no_enrollment_under_shadow_or_wrong_mode(self):
        self.runtime.live=False
        with self.assertRaises(ValueError): await self.enroll()
        self.runtime.live=True
        with self.assertRaises(ValueError): await self.enroll(mode="turbo")

    async def test_corrupt_journal_blocks_new_enrollment(self):
        self.manager.store.async_load.return_value={"mode":"low"}
        await self.manager.async_load()
        with self.assertRaises(ValueError): await self.enroll()

    async def test_close_does_not_delay_safety_with_device_writes(self):
        await self.enroll()
        await self.tick()
        await self.manager.async_close()
        self.assertEqual(self.manager.trial.stage,"needs_attention")
        self.runtime.api.request.assert_called_once()

    async def test_unhealthy_storage_blocks_enrollment(self):
        self.runtime.thermal_storage.diagnostics["status"]="error"
        with self.assertRaisesRegex(ValueError,"recording"): await self.enroll()

    async def test_backup_or_recording_failure_ends_active_trial(self):
        await self.enroll()
        await self.tick()
        self.state("low")
        self.runtime.thermal_storage.diagnostics["backup_paused"]=True
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"restore_reserved")
        self.assertEqual(self.runtime.api.request.call_args.args[1]["fan_mode"],"auto")

    async def test_external_same_mode_service_supersedes_trial(self):
        await self.enroll()
        event=SimpleNamespace(data={"domain":"climate","service":"set_fan_mode", "service_data":{
            "entity_id":self.entity,"fan_mode":"low"}},context=SimpleNamespace(id="external"))
        self.manager._service_event(event)
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"cancelled")
        self.runtime.api.request.assert_not_called()

    async def test_external_baseline_after_target_does_not_restore_over_manual(self):
        await self.enroll()
        await self.tick()
        self.state("low")
        await self.tick()
        self.state("auto")
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"cancelled")
        self.runtime.api.request.assert_called_once()

    async def test_late_cancel_preserves_bounded_ledger(self):
        await self.enroll()
        await self.tick()
        old=self.manager.trial
        with patch("custom_components.house_climate.head_fan_trials.time.time", return_value=old.ends_at+100000):
            await self.manager.async_cancel()
        self.manager.trial.validate(time.time())
        self.assertEqual(self.manager.trial.ends_at,old.ends_at)

    async def test_stopped_during_reserved_write_cannot_dispatch(self):
        await self.enroll()
        async def save(data):
            if data["stage"]=="request_reserved": self.runtime.stop_event.set()
        self.manager.store.async_save.side_effect=save
        await self.tick()
        self.assertEqual(self.manager.trial.stage,"needs_attention")

    async def test_startup_journal_rewrite_failure_does_not_block_climate_setup(self):
        await self.enroll()
        await self.tick()
        saved=self.manager.store.async_save.call_args.args[0]
        self.manager.store.async_load.return_value=saved
        self.manager.store.async_save.side_effect=OSError("disk full")
        await self.manager.async_load()
        self.assertEqual(self.manager.trial.stage,"needs_attention")
        self.assertIn("restart journal",self.manager.error)

    async def test_review_requires_reported_baseline_and_no_uncertain_service(self):
        await self.enroll()
        await self.tick()
        await self.manager.async_close()
        self.runtime.api.service_jobs=Mock(return_value=[])
        self.runtime.api.uncertain_service_jobs=Mock(return_value=[])
        self.state("low")
        with self.assertRaises(ValueError): await self.manager.async_acknowledge_restoration()
        self.state("auto")
        self.runtime.api.uncertain_service_jobs.return_value=[self.entity]
        with self.assertRaises(ValueError): await self.manager.async_acknowledge_restoration()
        self.runtime.api.uncertain_service_jobs.return_value=[]
        await self.manager.async_acknowledge_restoration()
        self.assertEqual(self.manager.trial.stage,"cancelled")
        self.runtime.api.request.assert_called_once()
