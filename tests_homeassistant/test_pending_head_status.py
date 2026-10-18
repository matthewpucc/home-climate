"""Slow but valid heat-pump feedback is a visible confirmation wait."""
from copy import deepcopy
import unittest

from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.sensor import ControllerStatus, Overview, RecoveryStatus, RoomPlan
from tests.test_rooms import configuration
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_plan_entities as plan_fixtures


class PendingHeadStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    entry = plan_fixtures.PlanEntityTests.entry
    decide = plan_fixtures.PlanEntityTests.decide
    rendered = plan_fixtures.PlanEntityTests.rendered

    def live_entry(self, **detail):
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 76})
        pending = {
            "requested_mode": "cool", "requested_target_temperature": 65,
            "reported_mode": "cool", "reported_target_temperature": 71,
            "temperature_unit": "°F", "remaining_seconds": 119.2,
            "elapsed_seconds": .8, "timeout_seconds": 120,
        }
        pending.update(detail)
        entry.runtime_data.status.update(
            event="live", phase="running", pending_head_commands={"climate.test_shared": pending})
        return entry

    async def test_recovery_names_shared_rooms_and_shows_pending_target_confirmation(self):
        entry = self.live_entry()
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "North alcove, South nook: Waiting for head confirmation: 120s remaining; "
                         "requested cool 65 °F, reported cool 71 °F")
        self.assertIn("Waiting for head confirmation", self.rendered(Overview(entry)))
        self.assertNotIn("Ready", self.rendered(RecoveryStatus(entry)))
        self.assertNotIn("fault", self.rendered(RecoveryStatus(entry)).lower())

    async def test_room_plan_distinguishes_requested_target_from_reported_target(self):
        entry = self.live_entry()
        for room in ("alpha", "beta"):
            text = self.rendered(RoomPlan(entry, room))
            self.assertIn("shared head: waiting for head confirmation: 120s remaining", text)
            self.assertIn("requested cool 65 °F, reported cool 71 °F", text)
            self.assertIn("room target 72 °F", text)
        self.assertIn("cool head toward", self.rendered(RoomPlan(entry, "gamma")))

    async def test_wait_countdown_clears_when_confirmed_without_mutating_the_plan(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        original = deepcopy(status)
        for entity in (RecoveryStatus(entry), Overview(entry), RoomPlan(entry, "alpha")):
            self.rendered(entity)
        self.assertEqual(status, original)
        status["pending_head_commands"]["climate.test_shared"]["remaining_seconds"] = 14.1
        self.assertIn("15s remaining", self.rendered(RecoveryStatus(entry)))
        status["pending_head_commands"] = {}
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Ready")
        self.assertNotIn("Waiting for head confirmation", self.rendered(Overview(entry)))
        self.assertIn("cool shared head toward", self.rendered(RoomPlan(entry, "alpha")))
        self.assertEqual(self.calls, [])

    async def test_reported_off_is_shown_as_feedback_rather_than_success(self):
        entry = self.live_entry(reported_mode="off")
        text = self.rendered(RecoveryStatus(entry))
        self.assertIn("requested cool 65 °F, reported off 71 °F", text)
        self.assertIn("Waiting for head confirmation", text)

    async def test_pending_native_temperatures_convert_to_ha_display_units(self):
        entry = self.live_entry(
            requested_target_temperature=18, reported_target_temperature=22,
            temperature_unit="°C")
        self.assertIn("requested cool 64.4 °F, reported cool 71.6 °F",
                      self.rendered(RecoveryStatus(entry)))
        self.hass.config.units = METRIC_SYSTEM
        entry.runtime_data.status["pending_head_commands"]["climate.test_shared"].update(
            requested_target_temperature=65, reported_target_temperature=71, temperature_unit="°F")
        self.assertIn("requested cool 18.3 °C, reported cool 21.7 °C",
                      self.rendered(RecoveryStatus(entry)))
        self.assertIn("room target 22.2 °C", self.rendered(RoomPlan(entry, "alpha")))

    async def test_device_cycle_and_global_off_waits_take_precedence(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        status.update(device_issues={"climate.test_shared": "Waiting for device to reconnect"})
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "North alcove, South nook: Waiting for device to reconnect")
        self.assertIn("wait for shared head; off requested", self.rendered(RoomPlan(entry, "alpha")))
        self.assertNotIn("Waiting for head confirmation", self.rendered(Overview(entry)))
        status["device_issues"] = {}
        status["cycle_holds"] = {"climate.test_shared": {"kind": "minimum_off", "remaining_seconds": 45}}
        self.assertIn("minimum off time: 45s remaining", self.rendered(RecoveryStatus(entry)))
        self.assertNotIn("head confirmation", self.rendered(RoomPlan(entry, "alpha")))
        status["cycle_holds"] = {}
        status.update(phase="settling", settle_remaining_seconds=30)
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Settling: 30s remaining")
        self.assertNotIn("head confirmation", self.rendered(RoomPlan(entry, "alpha")))
        status["phase"] = "stopping"
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Waiting for all heat-pump heads off")

    async def test_shadow_fault_and_stopped_do_not_reuse_live_confirmation_wait(self):
        entry = self.live_entry()
        status = entry.runtime_data.status
        status.update(event="shadow", control_block_reason="Turn on Controller enabled")
        self.assertEqual(self.rendered(RecoveryStatus(entry)),
                         "Preview only · Turn on Controller enabled")
        self.assertTrue(self.rendered(RoomPlan(entry, "alpha")).startswith("Would cool shared head"))
        self.assertNotIn("Waiting for head confirmation", self.rendered(Overview(entry)))
        status.update(event="fault", error="Test transport failure")
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Controller fault: Test transport failure")
        status["event"] = "stopped"
        self.assertEqual(self.rendered(RecoveryStatus(entry)), "Controller stopped")

    async def test_pending_diagnostics_are_available_in_controller_recovery_and_room_attributes(self):
        entry = self.live_entry()
        expected = entry.runtime_data.status["pending_head_commands"]
        self.assertEqual(ControllerStatus(entry).extra_state_attributes["pending_head_commands"], expected)
        self.assertEqual(RecoveryStatus(entry).extra_state_attributes["pending_head_commands"], expected)
        self.assertEqual(RoomPlan(entry, "alpha").extra_state_attributes["pending_head_command"],
                         expected["climate.test_shared"])
        self.assertNotIn("pending_head_command", RoomPlan(entry, "gamma").extra_state_attributes)

    async def test_long_names_and_missing_countdown_still_produce_valid_ha_states(self):
        raw = configuration()
        raw["rooms"]["alpha"]["name"] = "A long room name " * 30
        entry = self.entry(Config.from_dict(raw))
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 76})
        entry.runtime_data.status.update(event="live", phase="running", pending_head_commands={
            "climate.test_shared": {
                "requested_mode": "cool", "requested_target_temperature": 65,
                "reported_mode": "off", "reported_target_temperature": 71,
                "temperature_unit": "°F"}})
        self.assertEqual(len(self.rendered(RecoveryStatus(entry))), 255)
        self.assertEqual(len(self.rendered(Overview(entry))), 255)
        room = self.rendered(RoomPlan(entry, "alpha"))
        self.assertLessEqual(len(room), 255)
        self.assertIn("waiting for head confirmation", room)
