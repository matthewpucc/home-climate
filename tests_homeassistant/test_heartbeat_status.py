"""Delayed native heartbeat delivery is visible without misreporting a global stop."""
from copy import deepcopy
import unittest

from custom_components.house_climate.sensor import (
    ControllerStatus, CurrentIssues, Overview, RecoveryStatus,
)
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_plan_entities as plan_fixtures


class HeartbeatStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    entry = plan_fixtures.PlanEntityTests.entry
    decide = plan_fixtures.PlanEntityTests.decide
    rendered = plan_fixtures.PlanEntityTests.rendered

    def live_entry(self, state="overdue", blocked=True, reason=None):
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 68})
        entry.runtime_data.status.update(
            event="live", phase="running", control_enabled=True,
            heartbeat={
                "state": state, "blocked": blocked,
                "reason": reason or ("Waiting for Home Assistant to confirm the controller heartbeat; new activations are deferred"
                                     if blocked else None),
                "pending_seconds": 6, "lease_remaining_seconds": 114,
            })
        return entry

    async def test_overdue_heartbeat_outweighs_running_and_other_warnings(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        status["warnings"] = ["Optional circulation fan unavailable"]
        reason = status["heartbeat"]["reason"]
        before = deepcopy(status)
        self.assertEqual(self.rendered(Overview(entry)), "Live plan · " + reason)
        self.assertEqual(self.rendered(RecoveryStatus(entry)), reason)
        self.assertEqual(self.rendered(CurrentIssues(entry)),
                         reason + "; Optional circulation fan unavailable")
        self.assertNotIn("Ready", self.rendered(RecoveryStatus(entry)))
        self.assertNotIn("fault", self.rendered(CurrentIssues(entry)).lower())
        self.assertEqual(status, before)
        self.assertEqual(self.calls, [])

    async def test_normal_pending_pulse_is_quiet_and_confirmation_clears_block(self):
        entry = self.live_entry(state="pending", blocked=False)
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")
        self.assertIn("cool North alcove, South nook", self.rendered(Overview(entry)))
        status = entry.runtime_data.status
        status["heartbeat"].update(state="overdue", blocked=True, reason="Heartbeat confirmation delayed")
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Heartbeat confirmation delayed")
        status["heartbeat"].update(state="confirmed", blocked=False, reason=None)
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")

    async def test_fault_stopped_shadow_and_saved_reload_take_precedence(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        for event, expected in (("fault", "Controller fault: Test transport failure"),
                                ("stopped", "Controller stopped")):
            status.update(event=event, error="Test transport failure")
            for entity in (Overview(entry), RecoveryStatus(entry), CurrentIssues(entry)):
                self.assertEqual(self.rendered(entity), expected)
        status.update(event="shadow", control_block_reason="Turn on Controller enabled")
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "Preview only · Turn on Controller enabled")
        self.assertNotIn("heartbeat", self.rendered(CurrentIssues(entry)).lower())
        status.update(event="live", configuration_reload_pending=True,
                      configuration_reload_reason="Configuration saved; waiting for owned devices off")
        for entity in (Overview(entry), RecoveryStatus(entry)):
            self.assertEqual(self.rendered(entity), status["configuration_reload_reason"])

    async def test_attributes_preserve_heartbeat_state_without_changing_ages(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        expected = {key: status["heartbeat"][key] for key in ("state", "blocked", "reason")}
        entities = (ControllerStatus(entry), RecoveryStatus(entry), CurrentIssues(entry))
        before = [deepcopy(entity.extra_state_attributes) for entity in entities]
        for attributes in before:
            self.assertEqual(attributes["heartbeat"], expected)
            self.assertNotIn("pending_seconds", attributes["heartbeat"])
            self.assertNotIn("lease_remaining_seconds", attributes["heartbeat"])
        status["heartbeat"].update(pending_seconds=21, lease_remaining_seconds=99)
        self.assertEqual([entity.extra_state_attributes for entity in entities], before)
        self.assertEqual(self.calls, [])
