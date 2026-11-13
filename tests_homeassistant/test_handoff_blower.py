"""Synthetic HA service-registry tests for independent blower cleanup."""
import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant, Context
from custom_components.house_climate.handoff_blower import step, validate, pending


class BlowerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.entity = "climate.synthetic"
        self.record = dict(trial_id="test", mode="low", baseline="auto",
                           armed_at=time.time()-30, stage="pending", attempts=0)
        self.plan = {"heads": [self.entity], "authorized": True, "blower_recovery": {self.entity:self.record}, "problems":{}}
        self.controls = SimpleNamespace(hass=self.hass, handoff=self.plan, enabled=False,
            takeover_pending=False, owned=[self.entity], pending_fans=[], _handoff_jobs={},
            _uncertain_device_jobs=set(), pending_device_jobs=Mock(return_value=False),
            stop_jobs=Mock(return_value=False), repair_gate=None, ownership_generation=1,
            config=SimpleNamespace(heads={"test":self.entity}, head_activation_timeout_seconds=120),
            _save=AsyncMock(), service_inflight=0, head_activation_started=Mock(),
            head_activation_finished=Mock(), activation_job_uncertain=Mock(), _schedule_check=Mock())
        self.calls=[]
        async def handle(call):
            self.assertEqual(self.record["stage"], "reserved")
            self.assertTrue(self.controls._save.await_count)
            self.calls.append(call)
        self.hass.services.async_register("climate", "set_fan_mode", handle)
        self.state("low")

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    def state(self, fan, context=None, hvac="off"):
        self.hass.states.async_set(self.entity,hvac,{"fan_mode":fan,"fan_modes":["auto","low","quiet"]},context=context,force_update=True)

    async def test_dispatch_durable_and_fresh_auto_completes_without_physical_claim(self):
        self.assertFalse(await step(self.controls,self.plan))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0].data["fan_mode"],"auto")
        self.assertEqual(self.calls[0].context.id,self.record["context_id"])
        self.state("auto")
        self.assertTrue(await step(self.controls,self.plan))
        self.assertEqual(self.record["reason"],"auto_reported_physical_unverified")

    async def test_reserved_without_receipt_is_never_replayed(self):
        self.record.update(stage="reserved",attempts=1,dispatched_at=time.time()-20)
        self.assertFalse(await step(self.controls,self.plan))
        self.assertFalse(self.calls)
        self.state("auto")
        self.assertTrue(await step(self.controls,self.plan))

    async def test_manual_context_and_different_report_are_preserved(self):
        self.state("low",Context(user_id="operator"))
        self.assertTrue(await step(self.controls,self.plan))
        self.assertEqual(self.record["stage"],"superseded")
        self.assertFalse(self.calls)

    async def test_pending_and_uncertain_jobs_prevent_cached_completion(self):
        self.state("auto")
        self.controls._uncertain_device_jobs.add(self.entity)
        self.assertFalse(await step(self.controls,self.plan))
        self.assertFalse(self.calls)
        self.controls._uncertain_device_jobs.clear()
        self.controls.pending_device_jobs.return_value=True
        self.assertFalse(await step(self.controls,self.plan))

    async def test_heat_mode_not_safe_for_recovery(self):
        self.state("low",hvac="heat")
        self.assertFalse(await step(self.controls,self.plan))
        self.assertFalse(self.calls)

    async def test_bounded_retry_after_fresh_mismatch(self):
        await step(self.controls,self.plan)
        self.record["finished_at"]=time.time()-16
        self.state("low")
        await step(self.controls,self.plan)
        self.assertEqual(len(self.calls),2)
        self.record.update(attempts=3,finished_at=time.time()-16)
        self.state("low")
        self.assertFalse(await step(self.controls,self.plan))
        self.assertEqual(len(self.calls),2)

    async def test_manual_action_during_save_preempts_dispatch(self):
        async def save(*args):
            self.record["operator_changed"]=True
        self.controls._save.side_effect=save
        await step(self.controls,self.plan)
        self.assertFalse(self.calls)
        self.assertEqual(self.record["stage"],"superseded")

    async def test_no_record_is_noop(self):
        self.assertTrue(await step(self.controls,{"problems":{}}))
        self.assertFalse(self.calls)

    async def test_superseded_stage_during_save_preempts_dispatch(self):
        async def save(*args):
            self.record["stage"]="superseded"
        self.controls._save.side_effect=save
        self.controls.head_fan_trials=SimpleNamespace(async_fallback_resolved=AsyncMock())
        await step(self.controls,self.plan)
        self.assertFalse(self.calls)
        self.controls.head_fan_trials.async_fallback_resolved.assert_awaited()

    async def test_validation_and_pending(self):
        validate(self.plan)
        self.assertTrue(pending(self.plan))
        self.record["stage"]="complete"
        self.assertFalse(pending(self.plan))
        self.assertFalse(pending(None))
        self.record["attempts"]=True
        with self.assertRaises(ValueError):
            validate(self.plan)

    async def test_unavailable_and_unsupported_capability_block(self):
        self.hass.states.async_set(self.entity,"unavailable",{})
        self.assertFalse(await step(self.controls,self.plan))
        self.hass.states.async_set(self.entity,"off",{"fan_mode":"low","fan_modes":["low"]})
        self.assertFalse(await step(self.controls,self.plan))
        self.assertFalse(self.calls)

    async def test_new_mode_is_preserved(self):
        self.state("quiet")
        self.assertTrue(await step(self.controls,self.plan))
        self.assertFalse(self.calls)
        self.assertEqual(self.record["reason"],"new_fan_setting_preserved")

    async def test_notification_quiets_only_bounded_automatic_recovery(self):
        from custom_components.house_climate.handoff_blower import notification_disposition
        from custom_components.house_climate.handoff import notification_disposition as dispositions
        await step(self.controls,self.plan)
        self.plan["stage"]="applying"
        self.plan["problems"][self.entity+":blower"]="Transient service failure"
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"recovering")
        self.assertEqual(dispositions(self.controls)[self.entity+":blower"],"recovering")
        self.record["attempts"]=3
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"recovering")
        self.record["first_at"]=time.time()-30
        self.record["finished_at"]=time.time()-16
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"human_required")
        self.record["attempts"]=2
        self.state("low")
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"recovering")
        for flag in ("uncertain","rejected","manual_hvac","operator_changed"):
            self.record[flag]=True
            self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"human_required")
            self.record.pop(flag)
        self.controls.enabled=True
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"human_required")
        self.controls.enabled=False
        self.hass.states.async_set(self.entity,"unavailable",{})
        self.assertEqual(notification_disposition(self.controls,self.plan,self.entity),"human_required")
