"""Per-head cycle timing is readable without inspecting HA state attributes."""
from copy import deepcopy
import unittest

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.sensor import ControllerStatus, Overview, RecoveryStatus, RoomPlan
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_plan_entities as plan_fixtures


class CycleStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    entry = plan_fixtures.PlanEntityTests.entry
    decide = plan_fixtures.PlanEntityTests.decide
    rendered = plan_fixtures.PlanEntityTests.rendered

    def live_entry(self, holds=None):
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 76})
        entry.runtime_data.status.update(event="live", phase="running", cycle_holds=holds or {})
        return entry

    async def test_recovery_names_shared_rooms_and_shows_rounded_off_countdowns(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 119.2,
                                    "reason": "Waiting for minimum heat-pump off time"},
            "climate.test_second": {"kind": "minimum_off", "remaining_seconds": 45},
        })
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "North alcove, South nook: Waiting for minimum off time: 120s remaining; "
                         "Quiet space: Waiting for minimum off time: 45s remaining")
        self.assertIn("Waiting for head cycle timing", self.rendered(Overview(entry)))
        self.assertNotIn("Ready", self.rendered(RecoveryStatus(entry)))

    async def test_room_plan_makes_held_off_target_a_future_request(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 30}})
        for room in ("alpha", "beta"):
            text = self.rendered(RoomPlan(entry, room))
            self.assertIn("shared head: waiting for minimum off time: 30s remaining", text)
            self.assertIn("then request cool toward 72 °F", text)
        self.assertIn("cool head toward", self.rendered(RoomPlan(entry, "gamma")))

    async def test_minimum_run_is_finishing_rather_than_waiting_off(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_run", "remaining_seconds": 89.1}})
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "North alcove, South nook: Finishing minimum run: 90s remaining")
        self.assertIn("Finishing minimum head run", self.rendered(Overview(entry)))
        text = self.rendered(RoomPlan(entry, "alpha"))
        self.assertIn("finishing minimum run: 90s remaining; cool toward 72 °F", text)
        self.assertNotIn("then request", text)

    async def test_off_acknowledgement_does_not_pretend_idle_timer_has_started(self):
        entry = self.live_entry({
            "climate.test_shared": {
                "kind": "minimum_off", "remaining_seconds": 180,
                "reason": "Waiting for heat-pump off acknowledgement before timing idle"}})
        recovery = self.rendered(RecoveryStatus(entry))
        self.assertIn("Waiting for off confirmation; minimum off time of 180s follows", recovery)
        self.assertNotIn("180s remaining", recovery)
        self.assertIn("then request cool", self.rendered(RoomPlan(entry, "alpha")))

    async def test_device_recovery_takes_precedence_over_same_head_cycle_hold(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 120}})
        entry.runtime_data.status.update(
            device_issues={"climate.test_shared": "Waiting for device to reconnect"},
            waiting_devices=["climate.test_shared"],
            recovery_details={"climate.test_shared": {
                "kind": "recovery", "reason": "Waiting for a newer off report"}})
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "North alcove, South nook: Waiting for a newer off report")
        self.assertIn("wait for shared head; off requested", self.rendered(RoomPlan(entry, "alpha")))
        self.assertNotIn("then request", self.rendered(RoomPlan(entry, "alpha")))
        self.assertNotIn("Waiting for head cycle timing", self.rendered(Overview(entry)))

    async def test_shadow_stopped_and_fault_keep_their_existing_meaning(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 120}})
        entry.runtime_data.status.update(event="shadow", control_block_reason="Turn on Controller enabled")
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "Preview only · Turn on Controller enabled")
        self.assertNotIn("Waiting for head cycle timing", self.rendered(Overview(entry)))
        self.assertTrue(self.rendered(RoomPlan(entry, "alpha")).startswith("Would cool shared head"))
        entry.runtime_data.status.update(event="stopped")
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Controller stopped")
        entry.runtime_data.status.update(event="fault", error="Test transport failure")
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Controller fault: Test transport failure")

    async def test_clearing_holds_restores_ready_without_changing_plan_or_writing_devices(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 15}})
        status = entry.runtime_data.status
        before = deepcopy(status)
        self.rendered(RecoveryStatus(entry))
        self.rendered(Overview(entry))
        self.rendered(RoomPlan(entry, "alpha"))
        self.assertEqual(status, before)
        status["cycle_holds"] = {}
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertNotIn("Waiting for head cycle timing", self.rendered(Overview(entry)))
        self.assertIn("cool shared head toward 72 °F", self.rendered(RoomPlan(entry, "alpha")))
        self.assertEqual(self.calls, [])

    async def test_unknown_countdown_and_long_names_remain_valid_ha_states(self):
        raw = configuration()
        raw["rooms"]["alpha"]["name"] = "A very long room name " * 30
        entry = self.entry(Config.from_dict(raw))
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 76})
        entry.runtime_data.status.update(event="live", phase="running", cycle_holds={
            "climate.test_shared": {"kind": "minimum_off"},
            "climate.test_second": {"kind": "minimum_run", "remaining_seconds": 60}})
        self.assertEqual(len(self.rendered(RecoveryStatus(entry))), 255)
        self.assertEqual(len(self.rendered(Overview(entry))), 255)
        self.assertIn("waiting for minimum off time", self.rendered(RoomPlan(entry, "alpha")))

    async def test_last_stop_explains_off_hold_without_appearing_as_a_current_fault(self):
        entry = self.live_entry({
            "climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 120}})
        previous = {"reason": "Room cooling request ended", "kind": "controller_off", "mode": "cool"}
        entry.runtime_data.status["head_stop_reasons"] = {"climate.test_shared": previous}
        recovery = RecoveryStatus(entry)
        text = self.rendered(recovery)
        self.assertIn("North alcove, South nook: Waiting for minimum off time: 120s remaining", text)
        self.assertIn("last stop: Room cooling request ended", text)
        room = RoomPlan(entry, "alpha")
        self.assertIn("last stop: room cooling request ended", self.rendered(room))
        self.assertEqual(room.extra_state_attributes["last_stop"], previous)
        self.assertEqual(ControllerStatus(entry).extra_state_attributes["head_stop_reasons"],
                         {"climate.test_shared": previous})
        self.assertNotIn("fault", text.lower())
        entry.runtime_data.status["cycle_holds"] = {}
        self.assertEqual(self.rendered(recovery), "Ready")
        self.assertNotIn("last stop", self.rendered(room))
        self.assertIn("cool shared head toward", self.rendered(room))
        self.assertEqual(room.extra_state_attributes["last_stop"], previous)
        self.assertEqual(self.calls, [])
