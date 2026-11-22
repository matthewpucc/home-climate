"""A settled Off plan can still have demand held by shared-mode policy."""
import unittest

from custom_components.house_climate.sensor import Overview, RecoveryStatus
from tests_homeassistant import test_plan_entities as fixtures


class ModeHoldStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PlanEntityTests.asyncSetUp
    asyncTearDown = fixtures.PlanEntityTests.asyncTearDown
    handle = fixtures.PlanEntityTests.handle
    entry = fixtures.PlanEntityTests.entry
    rendered = fixtures.PlanEntityTests.rendered

    def settled(self):
        entry = self.entry()
        status = entry.runtime_data.status
        status.update(event="live", phase="settling", mode="off", head_targets_f={},
                      settle_remaining_seconds=0,
                      reasons=["Holding shared direction to avoid frequent mode changes"],
                      room_targets={"alpha": {"cool_requested": True}})
        return entry, status

    def both_labels(self, entry, label):
        self.assertEqual(self.rendered(RecoveryStatus(entry)), label)
        self.assertIn(label, self.rendered(Overview(entry)))

    async def test_direction_single_or_ambiguous(self):
        entry, status = self.settled()
        self.both_labels(entry, "Cooling requested; waiting to switch from heat")
        status["room_targets"] = {"alpha": {"heat_requested": True}}
        self.both_labels(entry, "Heating requested; waiting to switch from cooling")
        status["room_targets"]["gamma"] = {"cool_requested": True}
        self.both_labels(entry, "Heat-pump demand pending; waiting to change shared direction")
        self.assertEqual(self.calls, [])

    async def test_idle_and_hardware_wait_unchanged(self):
        entry, status = self.settled()
        status["room_targets"] = {"alpha": {"cool_requested": False}}
        self.both_labels(entry, "Ready; no heat-pump demand")
        status["room_targets"]["alpha"]["cool_requested"] = True
        status["settle_remaining_seconds"] = 10.1
        self.both_labels(entry, "Settling: 11s remaining")
        status.update(phase="stopping", settle_remaining_seconds=0)
        self.both_labels(entry, "Waiting for all heat-pump heads off")
        status.update(phase="settling", reasons=["Confirming changed emergency direction; both protection requests remain active"])
        self.assertNotIn("waiting to switch", self.rendered(RecoveryStatus(entry)))
        self.assertNotIn("waiting to switch", self.rendered(Overview(entry)))

    async def test_handoff_and_shared_guard_take_priority(self):
        entry, status = self.settled()
        status["mode_hold_remaining_seconds"] = 65
        status["head_constraints"] = {"shared": {"cool": {
            "kind": "comfort_limit", "room": "beta", "mode": "cool",
            "temperature_f": 69, "boundary_f": 70, "required_f": 70.5}}}
        for entity in (Overview(entry), RecoveryStatus(entry)):
            value = self.rendered(entity)
            self.assertLess(value.index("comfort floor"), value.index("Cooling requested"))
            self.assertNotIn("65s", value)
        self.assertIsNone(RecoveryStatus(entry).extra_state_attributes["mode_hold_remaining_seconds"])
        status["handoff"] = {"stage": "blocked"}
        self.both_labels(entry, "Recovery handoff incomplete; operator attention required")

    async def test_time_gate_is_distinct_from_settling_and_hidden_for_stronger_blockers(self):
        entry, status = self.settled()
        status["mode_hold_remaining_seconds"] = 59.5
        recovery = RecoveryStatus(entry)
        self.assertIn("(mode hold: about 60s remaining)", self.rendered(recovery))
        self.assertEqual(recovery.extra_state_attributes["mode_hold_remaining_seconds"], 59.5)
        self.assertEqual(recovery.extra_state_attributes["settle_remaining_seconds"], 0)
        self.assertNotIn("60s", self.rendered(Overview(entry)))
        status["settle_remaining_seconds"] = 10
        self.assertEqual(self.rendered(recovery), "Settling: 10s remaining")
        self.assertIsNone(recovery.extra_state_attributes["mode_hold_remaining_seconds"])
        status.update(settle_remaining_seconds=0, device_issues={"climate.test_shared": "Off not confirmed"})
        self.assertIn("Off not confirmed", self.rendered(recovery))
        self.assertIsNone(recovery.extra_state_attributes["mode_hold_remaining_seconds"])
        status.update(device_issues={}, mode_hold_remaining_seconds=0)
        self.assertNotIn("remaining", self.rendered(recovery))
        self.assertIsNone(recovery.extra_state_attributes["mode_hold_remaining_seconds"])
