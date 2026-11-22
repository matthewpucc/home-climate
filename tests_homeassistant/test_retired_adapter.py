"""Retiring a physically disconnected adapter does not activate any equipment."""
from copy import deepcopy
from datetime import datetime, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.handoff import arm
from tests.test_rooms import configuration


class RetiredAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        raw = configuration()
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha", heater_cutoff_f=70)
        raw["fallback"] = {"heat_low_f": 68, "cool_high_f": 73, "fan_percentage": 40}
        self.cfg = Config.from_dict(raw)
        self.retired = "climate.retired_adapter"
        self.replacement, self.other = self.cfg.heads.values()
        self.fan = next(iter(self.cfg.fans.values()))
        self.wall = datetime.now(timezone.utc).timestamp()
        self.controls = self.make_controls()
        self.controls.owned = (self.retired, self.other, self.fan)
        self.controls.recovering = self.controls.takeover_pending = True
        self.controls.handoff = arm(self.controls, self.controls.owned)
        self.controls.handoff["heads"].append(self.retired)
        self.controls.handoff.update(stage="superseded", interrupted=True, resume_after=self.wall - 30)
        self.controls.handoff["problems"] = {self.retired: "adapter disconnected", self.other: "other problem"}
        self.controls.handoff["requests"][self.retired] = {
            "at": self.wall - 25, "dispatched_at": self.wall - 24, "finished_at": self.wall - 22}
        self.controls.handoff["requests"][self.fan] = {"at": self.wall - 20}
        self.controls._uncertain_device_jobs.update((self.retired, self.other))
        self.controls._head_stop_pending.add(self.retired)
        self.native = SimpleNamespace(available=True, hvac_mode="off")
        self.hass.data[DATA_INSTANCES] = {
            "climate": SimpleNamespace(get_entity=lambda entity: self.native if entity == self.replacement else None)}
        self.hass.states.async_set(self.retired, "unavailable", timestamp=self.wall - 10)
        self.report(at=self.wall - 10)
        self.hass.states.async_set(self.other, "off", timestamp=self.wall - 10)
        self.hass.states.async_set(self.fan, "off", timestamp=self.wall - 10)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def make_controls(self):
        return NativeControls(self.hass, self.cfg, True, "retired-test", lambda: None)

    def report(self, state="off", *, at=None, attrs=None, context=None):
        self.hass.states.async_set(self.replacement, state, attrs or {}, context=context,
                                   force_update=True, timestamp=at or self.wall)

    async def retire(self, confirm=True):
        await self.controls.async_retire_recovery_adapter(
            self.retired, self.replacement, confirm_disconnected_same_head=confirm)

    async def test_exact_prune_persists_with_no_positive_dispatch_even_after_restart(self):
        previous = deepcopy(self.controls.handoff)
        self.controls._off = AsyncMock()
        await self.retire()
        self.controls._off.assert_not_awaited()
        self.assertEqual(self.controls.owned, (self.other, self.fan))
        self.assertEqual(self.controls.handoff["heads"], [self.other])
        self.assertEqual(self.controls.handoff["requests"], {self.fan: previous["requests"][self.fan]})
        self.assertEqual(self.controls.handoff["problems"][self.other], "other problem")
        self.assertNotIn(self.retired, self.controls.handoff["problems"])
        self.assertIn("turn Controller enabled On", self.controls.hint)
        self.assertEqual(self.controls._uncertain_device_jobs, {self.other})
        self.assertNotIn(self.retired, self.controls._head_stop_pending)
        self.assertTrue(self.controls.takeover_pending)
        self.assertFalse(self.controls.enabled_intent)
        saved = await self.controls.store.async_load()
        self.assertTrue(saved.get("retirement_review_required"))
        recreated = self.make_controls()
        await recreated.async_load()
        recreated._off = AsyncMock()
        from homeassistant.core import EVENT_CALL_SERVICE
        calls = []
        self.hass.bus.async_listen(EVENT_CALL_SERVICE, calls.append)
        await recreated.async_check()
        await self.hass.async_block_till_done()
        self.assertEqual(calls, [])
        self.assertTrue(recreated._retirement_review_required)
        self.assertTrue(recreated.takeover_pending)
        self.assertEqual(recreated.owned, (self.other, self.fan))
        self.assertIn(self.other, recreated._uncertain_device_jobs)

    async def test_rejects_unattested_live_old_or_stale_or_fabricated_new_feedback(self):
        with self.assertRaises(HAError):
            await self.retire(confirm=1)
        self.controls.enabled_intent = True
        with self.assertRaisesRegex(HAError, "Controller Off"):
            await self.retire()
        self.controls.enabled_intent = False
        self.hass.states.async_set(self.retired, "off", timestamp=self.wall)
        with self.assertRaisesRegex(HAError, "still reports available"):
            await self.retire()
        self.hass.states.async_set(self.retired, "unavailable", timestamp=self.wall)
        for report in (
                {"at": self.wall - 22}, {"at": self.wall - 181},
                {"attrs": {"restored": True}}, {"attrs": {"hvac_action": "heating"}},
                {"context": Context(user_id="set-state-admin")}):
            self.report(**report)
            with self.assertRaisesRegex(HAError, "fresh native Off"):
                await self.retire()
        self.report()
        self.native.hvac_mode = "heat"
        with self.assertRaisesRegex(HAError, "fresh native Off"):
            await self.retire()

    async def test_restart_fence_blocks_otherwise_eligible_preset_path(self):
        import asyncio
        from unittest.mock import patch
        from custom_components.house_climate import handoff

        self.controls.takeover_pending = False
        self.controls._uncertain_device_jobs.clear()
        self.controls.handoff.update(stage="stopping", interrupted=False, requests={}, problems={})
        await self.retire()
        restarted = self.make_controls()
        await restarted.async_load()
        self.assertFalse(restarted.takeover_pending)
        self.assertFalse(restarted.handoff["interrupted"])
        self.assertEqual(restarted.handoff["stage"], "stopping")
        self.assertEqual(restarted.handoff["requests"], {})
        restarted._off = AsyncMock()
        with patch.object(handoff, "step", new=AsyncMock()) as preset_handoff:
            await restarted.async_check()
            preset_handoff.assert_not_awaited()
            # A direct enable after review is the documented unlock. Control
            # still goes through ownership/takeover, never the stale plan.
            restarted.active = True
            with patch.object(type(self.cfg), "validate"):
                await restarted.async_set_enabled(True)
            self.assertFalse(restarted._retirement_review_required)
            self.assertTrue(restarted.takeover_pending)
            self.assertNotIn("retirement_review_required", await restarted.store.async_load())
            if restarted._task is not None:
                await asyncio.gather(restarted._task, return_exceptions=True)

    async def test_fence_preserves_owned_auxiliary_off_cleanup(self):
        heater = self.cfg.heater_entity
        self.assertIsNotNone(heater)
        self.controls.owned = (*self.controls.owned, heater)
        self.hass.states.async_set(heater, "heat", {"hvac_action": "heating"})
        await self.retire()
        self.controls._off = AsyncMock()
        await self.controls.async_check()
        self.assertIn(heater, self.controls.owned)
        self.assertIn(heater, [call.args[0] for call in self.controls._off.await_args_list])
        self.assertTrue(self.controls._retirement_review_required)

    async def test_rejects_pending_work_blower_and_keeps_old_records_on_save_failure(self):
        self.controls.pending_device_jobs = lambda entities: (object(),)
        with self.assertRaisesRegex(HAError, "finish"):
            await self.retire()
        self.controls.pending_device_jobs = lambda entities: ()
        queued = self.hass.loop.create_future()
        self.controls._handoff_jobs[self.fan] = queued
        with self.assertRaisesRegex(HAError, "finish"):
            await self.retire()
        queued.set_result(None)
        self.controls.handoff["blower_recovery"] = {self.retired: {"stage": "blocked"}}
        with self.assertRaisesRegex(HAError, "Fan trial"):
            await self.retire()
        del self.controls.handoff["blower_recovery"]
        original = deepcopy(self.controls.handoff)
        generation = self.controls.ownership_generation
        self.controls.store.async_save = AsyncMock(side_effect=OSError("disk error"))
        with self.assertRaises(OSError):
            await self.retire()
        self.assertIn(self.retired, self.controls.owned)
        self.assertEqual(self.controls.handoff, original)
        self.assertIn(self.retired, self.controls._uncertain_device_jobs)
        self.assertFalse(self.controls._retirement_review_required)
        self.assertGreater(self.controls.ownership_generation, generation)


class RetiredAdapterServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_requires_exact_attestation_and_direct_admin(self):
        from threading import Event
        from unittest.mock import Mock, patch
        import voluptuous as vol
        from homeassistant.exceptions import HomeAssistantError
        import custom_components.house_climate as integration
        from custom_components.house_climate.const import DOMAIN

        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            config = Config.from_dict(configuration())
            retire = AsyncMock()
            runtime = SimpleNamespace(config=config, controls=SimpleNamespace(
                async_retire_recovery_adapter=retire), stop_event=Event(), async_setup_controls=AsyncMock(),
                async_stop=AsyncMock(), async_start=AsyncMock())
            entry = SimpleNamespace(entry_id="registration-fixture", data={}, options={},
                title="Home Climate", async_on_unload=Mock(), add_update_listener=Mock())
            hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
            hass.auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda key:
                SimpleNamespace(is_admin=key == "admin")))
            with (patch.object(integration, "entry_configuration", return_value=config),
                  patch.object(integration, "validate_actuators"),
                  patch.object(integration, "ControllerRuntime", return_value=runtime),
                  patch.object(integration, "async_at_started", return_value=lambda: None)):
                self.assertTrue(await integration.async_setup_entry(hass, entry))
            data = {"retired_entity_id": "climate.old", "replacement_entity_id": "climate.new",
                    "confirm_disconnected_same_head": True}

            async def call(context, fields=data):
                await hass.services.async_call(DOMAIN, "retire_recovery_adapter",
                                               fields, blocking=True, context=context)

            for context in (Context(), Context(user_id="guest"),
                            Context(user_id="admin", parent_id="automation")):
                with self.assertRaisesRegex(HomeAssistantError, "directly"):
                    await call(context)
            with self.assertRaises(vol.Invalid):
                await call(Context(user_id="admin"), {**data, "confirm_disconnected_same_head": 1})
            retire.assert_not_awaited()
            await call(Context(user_id="admin"))
            retire.assert_awaited_once_with(
                "climate.old", "climate.new", confirm_disconnected_same_head=True)
            await hass.async_stop(force=True)
