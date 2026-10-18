"""Delayed heat-pump target echoes must not interrupt healthy climate calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.executor import Executor
from house_climate.policy import Plan
from tests.test_controller import FakeHA, NIGHT, config, fixture


class DelayedTargetHA(FakeHA):
    """Accept a head command while its reported target temporarily stays old."""

    def __init__(self, states, delayed_head, *, echo_mode=True):
        super().__init__(states)
        self.delayed_head = delayed_head
        self.echo_mode = echo_mode
        self.now = 0

    def publish(self, entity, *, mode=None, target=None, at=None):
        state = self.data[entity]
        if mode is not None:
            state["state"] = mode
        if target is not None:
            state["attributes"]["temperature"] = target
        stamp = self.now if at is None else at
        state["last_reported"] = (NIGHT + timedelta(seconds=stamp)).isoformat()

    def service(self, domain, service, **data):
        entity = data["entity_id"]
        if (entity == self.delayed_head and entity not in self.ignore
                and service == "set_temperature"):
            self.calls.append((domain, service, data))
            # The write is accepted; a subsequent device report still carries
            # its previous target, as observed with the Mitsubishi bridge.
            self.publish(entity, mode=data.get("hvac_mode") if self.echo_mode else None)
            return
        super().service(domain, service, **data)


class DelayedClimateAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(
            config(), command_timeout_seconds=45, head_activation_timeout_seconds=120,
            head_minimum_run_seconds=0, head_minimum_off_seconds=0,
        )
        self.bonus = self.cfg.heads["bonus"]
        self.guest = self.cfg.heads["guest"]
        states = fixture(self.cfg)
        states[self.bonus]["attributes"]["temperature"] = 71
        states[self.guest]["state"] = "cool"
        states[self.guest]["attributes"]["temperature"] = 67
        self.ha = DelayedTargetHA(states, self.bonus)
        self.executor = Executor(self.cfg, self.ha, "°F")
        # The shared system is already cooling a healthy, acknowledged head.
        self.executor.phase, self.executor.direction = "running", "cool"

    def plan(self, target=64, *, bonus=True, unsafe=False):
        targets, calls = {"guest": 72}, {"guest": 67}
        if bonus:
            targets["bonus"], calls["bonus"] = 72, target
        return Plan(
            mode="cool", head_targets_f=targets, head_call_targets_f=calls,
            inhibited_heads={"bonus": "cooling blocked by opposite temperature limit"} if unsafe else {},
        )

    def step(self, now, plan=None):
        self.ha.now = now
        self.executor.step(deepcopy(plan if plan is not None else self.plan()), self.ha.states(), now)

    def calls(self, entity, service=None):
        return [call for call in self.ha.calls
                if call[2]["entity_id"] == entity and (service is None or call[1] == service)]

    def pending(self, now):
        return self.executor.recovery_status(now)["pending_head_commands"][self.bonus]

    def assert_no_head_interruption(self):
        self.assertEqual(self.executor.phase, "running")
        self.assertEqual(self.executor.direction, "cool")
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.calls(self.guest), [])
        self.assertFalse(any(
            service == "set_hvac_mode" and data.get("hvac_mode") == "off"
            for _, service, data in self.ha.calls
        ))

    def test_old_target_reports_for_ninety_seconds_do_not_stop_either_head(self):
        self.step(0)
        for now in (15, 45, 60, 89):
            self.ha.publish(self.bonus, mode="cool", target=71, at=now)
            self.step(now)
            self.assertNotIn(self.bonus, self.executor.device_issues)
            self.assert_no_head_interruption()
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 1)
        detail = self.pending(89)
        self.assertEqual(detail["requested_mode"], "cool")
        self.assertEqual(detail["requested_target_temperature"], 64)
        self.assertEqual(detail["reported_mode"], "cool")
        self.assertEqual(detail["reported_target_temperature"], 71)
        self.assertEqual(detail["temperature_unit"], "°F")
        self.assertEqual(detail["timeout_seconds"], 120)
        self.assertEqual(detail["remaining_seconds"], 31)
        self.ha.publish(self.bonus, mode="cool", target=64, at=90)
        self.step(90)
        self.assertNotIn(self.bonus, self.executor.pending)
        self.assertNotIn(self.bonus, self.executor.device_issues)
        self.assertNotIn(self.bonus, self.executor.recovery_status(90)["pending_head_commands"])
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 1)
        self.assert_no_head_interruption()

    def test_pending_diagnostics_count_from_first_dispatch_not_latest_wrong_report(self):
        self.step(0)
        for now, remaining in ((0, 120), (45, 75), (90, 30), (119.9, .1)):
            if now:
                self.ha.publish(self.bonus, mode="cool", target=71, at=now)
                self.step(now)
            detail = self.pending(now)
            self.assertAlmostEqual(detail["elapsed_seconds"], now)
            self.assertAlmostEqual(detail["remaining_seconds"], remaining)
            self.assertNotIn(self.bonus, self.executor.recoveries)
        self.assert_no_head_interruption()

    def test_same_direction_target_change_keeps_original_unconfirmed_deadline(self):
        self.step(0)
        self.ha.publish(self.bonus, mode="cool", target=71, at=90)
        changed = self.plan(65)
        self.step(90, changed)
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 2)
        self.assertEqual(self.pending(90)["requested_target_temperature"], 65)
        self.assertEqual(self.pending(90)["remaining_seconds"], 30)
        # A late echo of the superseded target cannot acknowledge the newer one.
        self.ha.publish(self.bonus, mode="cool", target=64, at=100)
        self.step(100, changed)
        self.assertIn(self.bonus, self.executor.pending)
        self.assertEqual(self.pending(100)["requested_target_temperature"], 65)
        self.assertEqual(self.pending(100)["remaining_seconds"], 20)
        self.assert_no_head_interruption()
        self.step(120, changed)
        self.assertIn(self.bonus, self.executor.recoveries)
        self.assertIn("timed out", self.executor.device_issues[self.bonus])
        self.assertIn("65", self.executor.device_issues[self.bonus])

    def test_acknowledged_old_command_gives_new_target_its_own_deadline(self):
        self.step(0)
        self.ha.publish(self.bonus, mode="cool", target=64, at=90)
        changed = self.plan(65)
        self.step(90, changed)
        self.assertEqual(self.pending(90)["requested_target_temperature"], 65)
        self.assertEqual(self.pending(90)["elapsed_seconds"], 0)
        self.assertEqual(self.pending(90)["remaining_seconds"], 120)
        self.step(120, changed)
        self.assertEqual(self.pending(120)["remaining_seconds"], 90)
        self.assertNotIn(self.bonus, self.executor.device_issues)
        self.ha.publish(self.bonus, mode="cool", target=65, at=180)
        self.step(180, changed)
        self.assertNotIn(self.bonus, self.executor.pending)
        self.assert_no_head_interruption()

    def test_unconfirmed_activation_enters_off_recovery_at_one_hundred_twenty_seconds(self):
        self.step(0)
        self.ha.publish(self.bonus, mode="cool", target=71, at=119.9)
        self.step(119.9)
        self.assertNotIn(self.bonus, self.executor.recoveries)
        self.assert_no_head_interruption()
        self.step(120)
        self.assertIn(self.bonus, self.executor.recoveries)
        self.assertIn("Command acknowledgement timed out", self.executor.device_issues[self.bonus])
        # Recovery must supersede the uncertain command, even if the head later
        # reports the desired mode. It must never activate an opposite mode.
        self.step(121)
        offs = [call for call in self.calls(self.bonus, "set_hvac_mode")
                if call[2]["hvac_mode"] == "off"]
        self.assertTrue(offs)
        self.assertEqual(self.calls(self.guest), [])
        self.assertEqual(self.executor.direction, "cool")

    def test_pending_activation_with_old_off_snapshot_does_not_restart_minimum_off_time(self):
        self.cfg = replace(self.cfg, head_minimum_off_seconds=180)
        self.ha.echo_mode = False
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"
        self.step(0)
        self.assertEqual(self.calls(self.bonus, "set_temperature"), [])
        self.step(180)
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 1)
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        for now in (195, 225, 269):
            self.step(now)
            self.assertIn(self.bonus, self.executor.pending)
            self.assertNotIn(self.bonus, self.executor.cycle_holds)
            self.assertNotIn(self.bonus, self.executor.recoveries)
            self.assertIsNone(self.executor.head_cycles[self.bonus].off_since)
        self.ha.publish(self.bonus, mode="cool", target=64, at=270)
        self.step(270)
        self.assertNotIn(self.bonus, self.executor.pending)
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 1)
        self.assert_no_head_interruption()

    def test_safety_off_supersedes_pending_activation_without_waiting_for_grace(self):
        self.step(0)
        unsafe = self.plan(bonus=False, unsafe=True)
        self.step(15, unsafe)
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        self.assertTrue(self.calls(self.bonus, "set_hvac_mode"))
        self.assertEqual(self.calls(self.guest), [])
        # A delayed device application must also be stopped while the room
        # remains inhibited; the acknowledgement grace is never permission to run.
        self.ha.publish(self.bonus, mode="cool", target=64, at=30)
        self.step(30, unsafe)
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.executor.phase, "running")

    def test_shared_off_request_is_not_delayed_by_pending_activation_grace(self):
        self.step(0)
        self.step(15, Plan())
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertTrue(self.calls(self.bonus, "set_hvac_mode"))
        self.assertTrue(self.calls(self.guest, "set_hvac_mode"))
        self.assertIn(self.executor.phase, ("stopping", "settling"))
        self.assertIsNone(self.executor.direction)

    def test_unavailable_head_enters_local_off_recovery_immediately(self):
        self.step(0)
        self.ha.ignore.add(self.bonus)
        self.ha.publish(self.bonus, mode="unavailable", at=15)
        self.step(15)
        self.assertIn(self.bonus, self.executor.recoveries)
        self.assertIn("unavailable", self.executor.device_issues[self.bonus].lower())
        self.assertTrue(self.calls(self.bonus, "set_hvac_mode"))
        self.assertEqual(len(self.calls(self.bonus, "set_temperature")), 1)
        self.assertEqual(self.calls(self.guest), [])
        self.assertEqual(self.executor.phase, "running")

    def test_off_acknowledgement_still_times_out_after_forty_five_seconds(self):
        self.ha.ignore.add(self.guest)
        for now in (0, 44.9):
            self.executor.off(self.guest, self.ha.states(), now)
            self.assertNotIn(self.guest, self.executor.recoveries)
        self.executor.off(self.guest, self.ha.states(), 45)
        self.assertIn(self.guest, self.executor.recoveries)
        self.assertIn("timed out", self.executor.device_issues[self.guest])
        self.assertEqual(len(self.calls(self.guest, "set_hvac_mode")), 1)

    def test_fan_acknowledgement_still_times_out_after_forty_five_seconds(self):
        fan = self.cfg.fans["office"]
        self.ha.ignore.add(fan)
        for now in (0, 44.9):
            self.executor.fan(fan, 60, Plan(), self.ha.states(), now)
            self.assertNotIn(fan, self.executor.fan_failures)
        self.executor.fan(fan, 60, Plan(), self.ha.states(), 45)
        self.assertIn(fan, self.executor.fan_failures)
        self.assertIn("timed out", self.executor.fan_failures[fan].message)
        self.assertEqual(len(self.calls(fan)), 1)

    def test_auxiliary_activation_still_times_out_after_forty_five_seconds(self):
        heater = self.cfg.heater_entity
        self.ha.ignore.add(heater)
        for now in (0, 44.9):
            self.executor.climate(heater, "heat", 86, self.ha.states(), now, heater=True)
            self.assertNotIn(heater, self.executor.recoveries)
        self.executor.climate(heater, "heat", 86, self.ha.states(), 45, heater=True)
        self.assertIn(heater, self.executor.recoveries)
        self.assertIn("timed out", self.executor.device_issues[heater])
        self.assertEqual(len(self.calls(heater, "set_temperature")), 1)
