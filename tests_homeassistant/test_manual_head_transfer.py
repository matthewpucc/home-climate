"""Live opt-in evidence, durable transfer, and off-only scoped resume."""
import asyncio
import json
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.manual_head_transfer import OPT_IN_CONFIG
from tests_homeassistant import test_manual_head_disposition as durable


class ManualHeadTransferTests(IsolatedAsyncioTestCase):
    asyncSetUp = durable.ManualHeadPersistenceTests.asyncSetUp
    asyncTearDown = durable.ManualHeadPersistenceTests.asyncTearDown
    handle = durable.ManualHeadPersistenceTests.handle
    runtime = durable.ManualHeadPersistenceTests.runtime
    claim = durable.ManualHeadPersistenceTests.claim
    settle_watchdog = durable.ManualHeadPersistenceTests.settle_watchdog
    store = durable.ManualHeadPersistenceTests.store

    async def setup(self, opt_in):
        self.role, self.head = next(iter(self.cfg.heads.items()))
        self.other = tuple(self.cfg.heads.values())[1]
        self.attrs = dict(self.hass.states.get(self.head).attributes)
        self.hass.states.async_remove(self.head)
        self.entry = er.async_get(self.hass).async_get_or_create(
            "climate", "fixture", "remote-test", suggested_object_id=self.head.split(".")[1])
        self.assertEqual(self.entry.entity_id, self.head)
        self.hass.states.async_set(self.head, "off", self.attrs, force_update=True)
        path = Path(self.hass.config.path(OPT_IN_CONFIG))
        if opt_in:
            path.write_text(json.dumps([self.role]))
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        runtime.controls._manual_head_boundary = time.time() - 10
        self.calls.clear()
        return runtime

    async def confirmed_off(self):
        base = time.time()
        for delta, mode in ((-4, "heat"), (-2, "off"), (0, "off")):
            self.hass.states.async_set(self.head, mode, self.attrs,
                                       timestamp=base + delta, force_update=False)
            await self.hass.async_block_till_done()

    async def test_default_warning_never_transfers(self):
        runtime = await self.setup(False)
        with self.assertLogs("custom_components.house_climate.unexpected_off", level="WARNING"):
            await self.confirmed_off()
        self.assertIn(self.head, runtime.controls.owned)
        self.assertNotIn("manual_heads", await self.store("native").async_load())
        self.assertEqual(self.calls, [])

    async def test_live_transfer_survives_restart_does_not_write_and_resumes_only_when_stopped(self):
        runtime = await self.setup(True)
        controls = runtime.controls
        self.assertEqual(controls.manual_handoff_opt_in, {self.role})
        await self.confirmed_off()
        self.assertNotIn(self.head, controls.owned)
        self.assertIn(self.other, controls.owned)
        self.assertEqual(self.calls, [])
        saved = await self.store("native").async_load()
        self.assertEqual(saved["manual_heads"][self.role]["registry_id"], self.entry.id)
        self.assertNotIn(self.head, saved["devices"])
        self.assertNotIn("handoff", saved)
        reopened = NativeControls(self.hass, runtime.config, True, "native", lambda: None)
        await reopened.async_load()
        self.assertIn(self.head, reopened.manual_head_entities)
        self.assertNotIn(self.head, reopened.owned)
        status = controls.manual_handoff_status
        self.assertEqual(status["manual_heads"][self.role]["reason"], "confirmed_unexplained_off")
        with self.assertRaisesRegex(HAError, "Switch Controller Off"):
            await controls.async_resume_manual_head(self.head)

        await controls.async_set_enabled(False)
        await self.settle_watchdog(controls)
        self.assertFalse(controls.owned)
        self.assertNotIn(self.head, {call[2]["entity_id"] for call in self.calls})
        self.calls.clear()
        self.hass.states.async_set(self.head, "off", self.attrs, force_update=True)
        await controls.async_resume_manual_head(self.head)
        self.assertEqual(self.calls, [])
        self.assertFalse(controls.manual_head_entities)
        self.assertFalse(controls.owned)
        self.assertNotIn("manual_heads", await self.store("native").async_load())

    async def test_unresolved_old_command_blocks_transfer(self):
        runtime = await self.setup(True)
        controls = runtime.controls
        controls._head_inflight[self.head] = 1
        try:
            await self.confirmed_off()
            self.assertIn(self.head, controls.owned)
            self.assertFalse(controls.manual_head_entities)
            self.assertIsNone(controls.manual_handoff_pending)
            self.assertEqual(self.calls, [])
        finally:
            controls._head_inflight[self.head] = 0

    async def test_mode_changes_before_transaction_discards_candidate(self):
        runtime = await self.setup(True)
        controls = runtime.controls
        base = time.time()
        for delta, mode in ((-4, "heat"), (-2, "off")):
            self.hass.states.async_set(self.head, mode, self.attrs, timestamp=base + delta)
            await self.hass.async_block_till_done()
        async with controls._check_lock:
            self.hass.states.async_set(self.head, "off", self.attrs, timestamp=base)
            for _ in range(10):
                if controls.manual_handoff_pending:
                    break
                await asyncio.sleep(0)
            self.assertEqual(controls.manual_handoff_pending, self.head)
            self.hass.states.async_set(self.head, "cool", self.attrs, force_update=True)
        await self.hass.async_block_till_done()
        self.assertIn(self.head, controls.owned)
        self.assertFalse(controls.manual_head_entities)
        self.assertIsNone(controls.manual_handoff_pending)
        controls.head_write_fence.check(self.head, controls.head_write_fence.capture(self.head))
        self.assertEqual(self.calls, [])

    async def test_unverified_write_rolls_back_and_reopens_fence(self):
        runtime = await self.setup(True)
        controls = runtime.controls
        real_save = controls.store.async_save

        async def skip_candidate(data):
            if "manual_heads" not in data:
                await real_save(data)

        with patch.object(controls.store, "async_save", side_effect=skip_candidate):
            await self.confirmed_off()
        self.assertIn(self.head, controls.owned)
        self.assertIn(self.head, (await self.store("native").async_load())["devices"])
        self.assertFalse(controls.manual_head_entities)
        self.assertIsNone(controls.manual_handoff_pending)
        self.assertIn("retained ownership", controls.manual_handoff_issue)
        controls.head_write_fence.check(self.head, controls.head_write_fence.capture(self.head))
        self.assertEqual(self.calls, [])


class ManualHeadResumeServiceTests(IsolatedAsyncioTestCase):
    async def test_service_requires_direct_admin_and_boolean_confirmation(self):
        from tempfile import TemporaryDirectory
        from threading import Event
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        import voluptuous as vol
        from homeassistant.core import Context, HomeAssistant
        from homeassistant.exceptions import HomeAssistantError
        import custom_components.house_climate as integration
        from custom_components.house_climate.const import DOMAIN
        from custom_components.house_climate.controller.config import Config
        from tests.test_rooms import configuration

        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            config = Config.from_dict(configuration())
            resume = AsyncMock()
            runtime = SimpleNamespace(config=config, controls=SimpleNamespace(
                async_resume_manual_head=resume), stop_event=Event(), async_setup_controls=AsyncMock(),
                async_stop=AsyncMock(), async_start=AsyncMock())
            entry = SimpleNamespace(entry_id="manual-resume-registration", data={}, options={},
                title="Home Climate", async_on_unload=Mock(), add_update_listener=Mock())
            hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
            hass.auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda key:
                SimpleNamespace(is_admin=key == "admin")))
            with (patch.object(integration, "entry_configuration", return_value=config),
                  patch.object(integration, "validate_actuators"),
                  patch.object(integration, "ControllerRuntime", return_value=runtime),
                  patch.object(integration, "async_at_started", return_value=lambda: None)):
                self.assertTrue(await integration.async_setup_entry(hass, entry))

            async def invoke(context, confirmed=True):
                await hass.services.async_call(DOMAIN, "resume_manual_head_control",
                    {"entity_id": "climate.synthetic", "confirm": confirmed}, blocking=True, context=context)

            for context in (Context(), Context(user_id="guest"), Context(user_id="admin", parent_id="automation")):
                with self.assertRaisesRegex(HomeAssistantError, "directly"):
                    await invoke(context)
            with self.assertRaises(vol.Invalid):
                await invoke(Context(user_id="admin"), confirmed=1)
            resume.assert_not_awaited()
            await invoke(Context(user_id="admin"))
            resume.assert_awaited_once_with("climate.synthetic")
            await hass.async_stop(force=True)
