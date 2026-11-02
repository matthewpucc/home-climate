"""Alert behavior uses a real HA registry and synthetic notification handlers."""
import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

from homeassistant.components import persistent_notification
from homeassistant.components import notify as ha_notify
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate.controller.notifications import NotificationRoute, NotificationSettings
from custom_components.house_climate.notifications import AlertManager


class NotificationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        await persistent_notification.async_setup(self.hass, {})
        self.calls = []
        self.clock = 100.0
        self.manager = None

        async def send(call):
            self.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("notify", "send_message", send)
        self.hass.services.async_register("notify", "fixture_critical", send)
        self.hass.states.async_set("notify.fixture_group", "unknown")

    async def asyncTearDown(self):
        if self.manager:
            await self.manager.async_close()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def start(self, *, critical=False, action="notify.send_message", callback=None):
        normal = (NotificationRoute(action, ("notify.fixture_group",))
                  if action == "notify.send_message" else NotificationRoute(action))
        self.manager = AlertManager(self.hass, "fixture", NotificationSettings(
            normal, NotificationRoute("notify.fixture_critical", data={"data": {"priority": "high"}},
                                      critical_capable=True) if critical else None),
            callback, clock=lambda: self.clock)
        return self.manager

    @property
    def notice(self):
        return persistent_notification._async_get_or_create_notifications(self.hass)

    def battery(self, active=True, *, severity="critical", message="Battery reserve reached"):
        self.manager.update("battery", active, "Home Climate battery", message, severity)

    async def test_normal_target_and_unverified_critical_fallback(self):
        manager = self.start()
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [("send_message", {"entity_id": ["notify.fixture_group"],
            "message": "URGENT: Battery reserve reached", "title": "Home Climate battery"})])
        status = manager.diagnostics
        self.assertTrue(status["critical_delivery_unverified"])
        self.assertEqual(status["active"][0]["delivery_state"], "service_accepted")
        self.assertIn("Critical delivery is not verified", self.notice["home_climate_fixture_battery"]["message"])

    async def test_real_notify_entity_schema_accepts_structured_entity_target(self):
        calls = self.calls

        class FixtureNotify(ha_notify.NotifyEntity):
            _attr_name = "Fixture group"
            _attr_unique_id = "notification_route_fixture"
            _attr_supported_features = ha_notify.NotifyEntityFeature.TITLE

            async def async_send_message(self, message, title=None):
                calls.append(("entity", {"message": message, "title": title}))

        self.hass.services.async_remove("notify", "send_message")
        self.hass.states.async_remove("notify.fixture_group")
        await ha_notify.async_setup(self.hass, {})
        await self.hass.data[ha_notify.DATA_COMPONENT].async_add_entities([FixtureNotify()])
        self.start()
        self.battery(severity="normal")
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [("entity", {"message": "Battery reserve reached", "title": "Home Climate battery"})])

    async def test_absent_service_retry_budget_stops_after_three_checks(self):
        manager = self.start(action="notify.fixture_absent")
        for _ in range(8):
            self.battery()
            await self.hass.async_block_till_done()
            self.clock += 300
        self.assertEqual(manager.diagnostics["active"][0]["attempts"], 3)
        self.assertEqual(self.calls, [])

    async def test_missing_entity_is_not_reported_as_service_acceptance(self):
        manager = self.start()
        self.hass.states.async_remove("notify.fixture_group")
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertEqual(manager.diagnostics["active"][0]["delivery_state"], "target_unavailable")
        self.assertIn("missing or unavailable", manager.diagnostics["active"][0]["error"])

    async def test_capable_critical_route_and_poll_deduplication(self):
        manager = self.start(critical=True)
        for _ in range(4):
            self.battery()
        await self.hass.async_block_till_done()
        for _ in range(4):
            self.battery(message="Battery still below reserve")
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "fixture_critical")
        self.assertEqual(self.calls[0][1]["data"], {"priority": "high"})
        self.assertFalse(manager.diagnostics["critical_route_unverified"])
        self.assertTrue(manager.diagnostics["critical_delivery_unverified"])
        self.assertEqual(manager.diagnostics["active"][0]["endpoint_delivery"], "unknown")
        self.assertIn("does not prove", manager.diagnostics["delivery_evidence"])

    async def test_clear_and_recurrence_are_new_incident(self):
        self.start()
        self.battery()
        await self.hass.async_block_till_done()
        self.battery(False)
        self.assertEqual(self.manager.diagnostics["active"], [])
        self.assertNotIn("home_climate_fixture_battery", self.notice)
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 2)

    async def test_severity_escalation_replaces_queued_normal_dispatch(self):
        self.start(critical=True)
        self.battery(severity="normal")
        self.battery(severity="critical")
        await self.hass.async_block_till_done()
        self.assertEqual([kind for kind, _ in self.calls], ["fixture_critical"])

    async def test_recovery_before_dispatch_sends_nothing(self):
        self.start()
        self.battery()
        self.battery(False)
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])

    async def test_missing_service_retries_are_bounded_and_can_recover(self):
        manager = self.start(action="notify.fixture_missing")
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(manager.diagnostics["active"][0]["attempts"], 1)
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(manager.diagnostics["active"][0]["attempts"], 1)

        async def recovered(call):
            self.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("notify", "fixture_missing", recovered)
        self.clock += 300
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(manager.diagnostics["active"][0]["delivery_state"], "service_accepted")
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_result_does_not_generate_repeated_sends(self):
        manager = self.start(action="notify.fixture_failure")

        async def failed(call):
            self.calls.append((call.service, dict(call.data)))
            raise RuntimeError("Provider failed after possible dispatch")

        self.hass.services.async_register("notify", "fixture_failure", failed)
        self.battery()
        await self.hass.async_block_till_done()
        self.clock += 3600
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.diagnostics["active"][0]["delivery_state"], "unknown")

    async def test_slow_provider_does_not_block_update_or_retry_unknown_delivery(self):
        manager = self.start(action="notify.fixture_slow")
        entered = asyncio.Event()

        async def slow(call):
            self.calls.append((call.service, dict(call.data)))
            entered.set()
            await asyncio.Event().wait()

        self.hass.services.async_register("notify", "fixture_slow", slow)
        with patch("custom_components.house_climate.notifications.SERVICE_TIMEOUT_SECONDS", .02):
            self.battery()
            await entered.wait()
            await self.hass.async_block_till_done()
        self.assertEqual(manager.diagnostics["active"][0]["delivery_state"], "unknown")
        self.clock += 3600
        self.battery()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)

    async def test_diagnostic_callback_failure_does_not_prevent_notification(self):
        self.start(callback=Mock(side_effect=RuntimeError("Fixture diagnostic failure")))
        with self.assertLogs("custom_components.house_climate.notifications", level="ERROR"):
            self.battery()
            await self.hass.async_block_till_done()
        self.manager.notify = lambda: None
        self.assertEqual(len(self.calls), 1)

    async def test_status_routes_failed_stop_critically_and_suppresses_duplicate_device_issue(self):
        manager = self.start(critical=True)
        status = {"event": "live", "control_enabled": True,
                  "shared_stop": {"climate.fixture_head": {"failed": True, "confirmed": False,
                                                             "reason": "Three Off attempts exhausted"}},
                  "device_issues": {"climate.fixture_head": "Waiting for Off"}}
        manager.observe_status(status)
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "fixture_critical")
        self.assertEqual(len(manager.diagnostics["active"]), 1)
        status["shared_stop"]["climate.fixture_head"]["confirmed"] = True
        status["device_issues"] = {}
        manager.observe_status(status)
        self.assertEqual(manager.diagnostics["active"], [])

    async def test_status_ordinary_timers_are_quiet_but_missing_room_source_is_actionable(self):
        manager = self.start()
        status = {"event": "live", "control_enabled": True,
                  "phase": "settling", "settle_remaining_seconds": 60,
                  "cycle_holds": {"climate.fixture_head": {"reason": "Minimum off time"}},
                  "shared_stop": {"climate.fixture_head": {"failed": False, "attempts": 1}},
                  "room_targets": {"fixture_room": {}}, "rooms": {"fixture_room": {"sources": 1}}}
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        status["rooms"] = {}
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.diagnostics["active"][0]["severity"], "normal")

    async def test_fault_attention_survives_following_stopped_report_until_control_resumes(self):
        manager = self.start()
        manager.observe_status({"event": "fault", "error": "Fixture transport failed"})
        manager.observe_status({"event": "stopped"})
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.diagnostics["active"][0]["key"], "controller_fault")
        manager.observe_status({"event": "live", "control_enabled": True})
        self.assertEqual(manager.diagnostics["active"], [])

    async def test_handoff_failure_remains_critical_when_controller_is_stopped(self):
        manager = self.start(critical=True)
        manager.observe_status({"event": "stopped", "handoff": {
            "stage": "verify", "problems": {"climate.fixture_head": "Range not confirmed"}}})
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls[0][0], "fixture_critical")
        self.assertEqual(manager.diagnostics["active"][0]["key"], "recovery_handoff")

    async def test_outdoor_primary_warmup_quiet_but_fallback_or_unverified_requires_attention(self):
        manager = self.start()
        status = {"event": "live", "control_enabled": True, "seasonal": {
            "selected_role": "primary", "degraded": False, "evidence_status": "warming"}}
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        status["seasonal"].update(selected_role="fallback", degraded=True, evidence_status="warming",
                                  reason="Local outdoor source unavailable; fallback is warming")
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(manager.diagnostics["active"][0]["key"], "outdoor_source")
        status["seasonal"].update(selected_role="primary", degraded=False, evidence_status="qualified")
        manager.observe_status(status)
        self.assertEqual(manager.diagnostics["active"], [])
        status["seasonal"].update(degraded=True, evidence_status="unverified",
                                  reason="Physical observation time is unavailable")
        manager.observe_status(status)
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
