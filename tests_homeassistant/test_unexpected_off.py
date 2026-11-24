"""Exercise the real HA state event/report and native service recorder entry points."""
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant.core import Context, HomeAssistant

from custom_components.house_climate.controls import NativeControls


class UnexpectedOffTests(IsolatedAsyncioTestCase):
    HEAD = "climate.fixture_head"
    OTHER = "climate.other"

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.now = datetime.now(timezone.utc).timestamp()
        fake_now = patch("custom_components.house_climate.mode_diagnostics._now",
                         side_effect=lambda: self.now)
        fake_now.start()
        self.addCleanup(fake_now.stop)
        # Keep the actual NativeControls lease guard while avoiding unrelated
        # control setup, paired equipment, or disk persistence in these tests.
        controls = self.controls = NativeControls.__new__(NativeControls)
        controls.live = controls.active = controls.enabled = True
        controls.recovering = controls._claiming = controls._closing = controls.takeover_pending = False
        controls.owned = (self.HEAD,)
        controls.ownership_generation = 0
        controls._uncertain_device_jobs = set()
        controls._head_stop_pending = set()
        controls.pending_device_jobs = lambda _entities: ()
        from custom_components.house_climate.mode_diagnostics import ModeDiagnostics
        self.diagnostic = ModeDiagnostics(self.hass, "test_entry", (self.HEAD,),
                                          controls._off_observation_eligible)
        self.diagnostic._save = lambda: None
        self.diagnostic.start()
        self.addCleanup(self.diagnostic.close)
        warning = patch("custom_components.house_climate.unexpected_off._LOGGER.warning")
        self.log = warning.start()
        self.addCleanup(warning.stop)

    async def report(self, mode, *, elapsed=2, context=None, attrs=None, entity=None, stale=0):
        self.now += elapsed
        self.hass.states.async_set(entity or self.HEAD, mode, attrs, context=context, timestamp=self.now - stale)
        await self.hass.async_block_till_done()

    async def off(self):
        await self.report("off")
        await self.report("off")

    async def test_distinct_reports_log_once_and_never_issue_a_command(self):
        with patch.object(type(self.hass.services), "async_call", autospec=True) as service:
            await self.report("heat")
            await self.report("off")
            await self.report("off", elapsed=0)
            self.log.assert_not_called()
            await self.report("off")
            await self.report("off")
            self.log.assert_called_once()
            self.assertEqual(self.log.call_args.args[1:], (self.HEAD, "heat"))
            service.assert_not_called()
        self.assertEqual(self.controls.owned, (self.HEAD,))
        self.assertTrue(self.controls.enabled)

    async def test_startup_reconnection_stale_and_restored_do_not_establish_baseline(self):
        await self.off()
        await self.report("heat")
        await self.report("unavailable")
        await self.off()
        await self.report("heat", stale=30)
        await self.off()
        await self.report("heat", attrs={"restored": True})
        await self.off()
        self.log.assert_not_called()

    async def test_recent_controller_dispatch_and_matching_context_are_not_remote(self):
        await self.report("heat")
        called = []
        self.hass.services.async_register("climate", "set_hvac_mode", called.append)
        await self.diagnostic.call("climate", "set_hvac_mode",
                                   {"entity_id": self.HEAD, "hvac_mode": "off"},
                                   path="normal", reason="worker_command")
        self.assertEqual(len(called), 1)
        await self.report("heat")  # Delayed active readback until our command applies.
        await self.off()
        await self.report("heat", elapsed=50)
        await self.report("off", context=called[0].context)
        await self.report("off")
        await self.report("heat")
        await self.report("off")
        # State-reported carries the live event context even if HA reuses the
        # prior State (and therefore its old context) for same-value updates.
        await self.report("off", context=Context(user_id="synthetic_user"))
        self.log.assert_not_called()

    async def test_uncertain_mode_write_requires_completed_off_and_new_feedback(self):
        def fails(_):
            raise RuntimeError("synthetic dispatch uncertainty")

        async def send(mode):
            await self.diagnostic.call("climate", "set_hvac_mode",
                {"entity_id": self.HEAD, "hvac_mode": mode}, path="normal", reason="worker_command")

        self.hass.services.async_register("climate", "set_hvac_mode", fails)
        with self.assertRaises(RuntimeError):
            await send("off")
        self.assertTrue(self.diagnostic.blocks_manual_release(self.HEAD))
        await self.off()
        self.assertTrue(self.diagnostic.blocks_manual_release(self.HEAD))
        self.hass.services.async_register("climate", "set_hvac_mode", lambda _: None)
        await send("heat")
        self.assertTrue(self.diagnostic.blocks_manual_release(self.HEAD))
        await send("off")
        self.assertTrue(self.diagnostic.blocks_manual_release(self.HEAD))
        await self.report("off")
        self.assertFalse(self.diagnostic.blocks_manual_release(self.HEAD))

    async def test_lease_change_and_uncertain_operation_cannot_join_evidence(self):
        await self.report("cool")
        await self.report("off")
        self.controls.ownership_generation += 1
        await self.report("off")
        await self.report("cool")
        await self.report("off")
        self.controls.pending_device_jobs = lambda _entities: (object(),)
        await self.report("off")
        self.log.assert_not_called()

    async def test_preview_and_unowned_heads_do_not_log(self):
        self.controls.live = False
        await self.report("heat")
        await self.off()
        self.controls.live = True
        await self.report("cool", entity=self.OTHER)
        await self.report("off", entity=self.OTHER)
        await self.report("off", entity=self.OTHER)
        self.log.assert_not_called()
