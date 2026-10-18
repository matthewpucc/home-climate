"""Heartbeat uncertainty delays activation without inventing device failures."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import timedelta

from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.ha import HAActivationDeferred, HAError
from house_climate.policy import Plan, Policy
from tests.test_controller import FakeHA, NIGHT, config, fixture


class DeferringHA(FakeHA):
    """Reject positive commands before dispatch, while keeping Off available."""

    def __init__(self, states):
        super().__init__(states)
        self.defer = False
        self.error_type = HAActivationDeferred
        self.deferred = []

    def service(self, domain, service, **data):
        positive = (
            domain == "climate"
            and (service == "set_temperature"
                 or data.get("hvac_mode") in ("heat", "cool", "fan_only"))
        ) or (
            domain == "fan"
            and service in ("turn_on", "set_percentage")
            and data.get("percentage", 100) > 0
        )
        if self.defer and positive:
            self.deferred.append((domain, service, data))
            raise self.error_type("Waiting for a confirmed heartbeat")
        return super().service(domain, service, **data)


class ActivationDeferralTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(config(), head_minimum_run_seconds=300,
                           head_minimum_off_seconds=0)
        self.ha = DeferringHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")
        # Isolate local dispatch from the separately tested shared off barrier.
        self.executor.phase, self.executor.direction = "running", "cool"
        self.head = self.cfg.heads["guest"]
        self.fan = self.cfg.fans["office"]

    def request(self, now, *, target=72, mode="cool", heads=None):
        stamp = NIGHT + timedelta(seconds=now)
        for room in self.cfg.zones.values():
            for probe in room.probes:
                self.ha.data[probe.entity_id]["last_reported"] = stamp.isoformat()
        states = self.ha.states()
        plan = Policy(self.cfg).decide(states, stamp, Settings.read(states, self.cfg))
        plan.mode = mode
        plan.head_targets_f = dict({"guest": target} if heads is None else heads)
        plan.head_call_targets_f = {}
        plan.heater_on = plan.auxiliary_fan_only = False
        plan.fans = {}
        return plan

    def step(self, now, **kwargs):
        plan = self.request(now, **kwargs)
        self.executor.step(plan, self.ha.states(), now)
        return plan

    def assert_no_device_failure(self):
        self.assertEqual(self.executor.device_issues, {})
        self.assertEqual(self.executor.recoveries, {})
        self.assertEqual(self.executor.fan_failures, {})

    def test_initial_heat_and_cool_wait_without_starting_acknowledgement_budget(self):
        for mode in ("heat", "cool"):
            with self.subTest(mode=mode):
                ha = DeferringHA(fixture(self.cfg))
                ha.defer = True
                executor = Executor(self.cfg, ha, "°F")
                executor.phase, executor.direction = "running", mode
                for now in (0, 15, 90):
                    executor.step(Plan(mode=mode, head_targets_f={"guest": 72}),
                                  ha.states(), now)
                    self.assertNotIn(self.head, executor.pending)
                    self.assertNotIn(self.head, executor.device_issues)
                    self.assertNotIn(self.head, executor.recoveries)
                    self.assertIsNone(executor.head_cycles[self.head].run_since)
                self.assertEqual(len(ha.deferred), 3)
                self.assertEqual(ha.calls, [])
                ha.defer = False
                executor.step(Plan(mode=mode, head_targets_f={"guest": 72}),
                              ha.states(), 105)
                self.assertEqual(executor.pending[self.head].activation_since, 105)
                executor.step(Plan(mode=mode, head_targets_f={"guest": 72}),
                              ha.states(), 120)
                self.assertNotIn(self.head, executor.pending)
                self.assertEqual(executor.head_cycles[self.head].run_since, 120)

    def test_deferred_retargets_preserve_outstanding_command_and_original_deadline(self):
        self.ha.ignore.add(self.head)
        self.step(0)
        original = self.executor.pending[self.head]
        original_values = copy.deepcopy(vars(original))
        cycle_before = copy.deepcopy(self.executor.head_cycles[self.head])
        self.ha.calls.clear()
        self.ha.defer = True

        for now, target in ((15, 73), (30, 74)):
            self.step(now, target=target)
            self.assertIs(self.executor.pending[self.head], original)
            self.assertEqual(vars(original), original_values)
            self.assertEqual(self.executor.head_cycles[self.head], cycle_before)
            self.assert_no_device_failure()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(len(self.ha.deferred), 2)

        # Deferral must not renew the budget for a command already dispatched.
        self.step(45, target=74)
        self.assertIs(self.executor.pending[self.head], original)
        self.assertIn(self.head, self.executor.recoveries)
        self.assertEqual(self.executor.recoveries[self.head].command, original.command)
        self.assertIn("acknowledgement timed out", self.executor.device_issues[self.head])
        self.step(60, target=74)
        self.assertTrue(any(service == "set_hvac_mode"
                            and data == {"entity_id": self.head, "hvac_mode": "off"}
                            for _, service, data in self.ha.calls))
        self.assertEqual(len(self.ha.deferred), 2)

    def test_previously_dispatched_command_can_acknowledge_during_deferral(self):
        self.step(0)
        self.assertIn(self.head, self.executor.pending)
        self.ha.calls.clear()
        self.ha.defer = True
        self.step(15)
        self.assertNotIn(self.head, self.executor.pending)
        self.assertEqual(self.executor.head_cycles[self.head].run_since, 15)
        self.assert_no_device_failure()
        self.assertEqual(self.ha.deferred, [])
        plan = self.step(30, mode="off", heads={})
        self.assertEqual(plan.head_targets_f, {"guest": 72})
        self.assertEqual(self.executor.cycle_holds[self.head]["remaining_seconds"], 285)
        self.assertEqual(self.ha.calls, [])

    def test_deferred_running_retarget_keeps_acknowledged_minimum_run(self):
        self.step(0)
        self.step(15)
        cycle_before = copy.deepcopy(self.executor.head_cycles[self.head])
        self.ha.calls.clear()
        self.ha.defer = True
        self.step(30, target=73)
        self.assertNotIn(self.head, self.executor.pending)
        self.assertEqual(self.executor.head_cycles[self.head], cycle_before)
        self.assert_no_device_failure()
        plan = self.step(45, mode="off", heads={})
        self.assertEqual(plan.head_targets_f, {"guest": 72})
        self.assertEqual(self.executor.cycle_holds[self.head]["remaining_seconds"], 270)
        self.assertEqual(self.ha.data[self.head]["attributes"]["temperature"], 72)
        self.assertEqual(self.ha.calls, [])

    def test_safety_off_bypasses_deferral_and_minimum_run(self):
        self.step(0)
        self.step(15)
        self.ha.calls.clear()
        self.ha.defer = True
        plan = self.request(30, mode="off", heads={})
        plan.inhibited_heads = {"guest": "Room temperature protection"}
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.head]["state"], "off")
        self.assertNotIn(self.head, self.executor.cycle_holds)
        self.assertTrue(any(data.get("hvac_mode") == "off"
                            and data["entity_id"] == self.head
                            for _, _, data in self.ha.calls))
        self.assertEqual(self.ha.deferred, [])
        self.assert_no_device_failure()

    def test_auxiliary_heat_and_fan_only_wait_without_off_recovery(self):
        for plan in (Plan(heater_on=True), Plan(auxiliary_fan_only=True)):
            with self.subTest(plan=plan):
                ha = DeferringHA(fixture(self.cfg))
                executor = Executor(self.cfg, ha, "°F")
                entity = self.cfg.heater_entity
                ha.defer = True
                for now in (0, 60):
                    executor.auxiliary(plan, ha.states(), now)
                    self.assertNotIn(entity, executor.pending)
                    self.assertNotIn(entity, executor.recoveries)
                    self.assertNotIn(entity, executor.device_issues)
                self.assertEqual(ha.calls, [])
                ha.defer = False
                executor.auxiliary(plan, ha.states(), 75)
                executor.auxiliary(plan, ha.states(), 90)
                self.assertNotIn(entity, executor.pending)
                ha.defer = True
                executor.auxiliary(Plan(), ha.states(), 105)
                self.assertEqual(ha.data[entity]["state"], "off")
                self.assertEqual(ha.calls[-1][2]["hvac_mode"], "off")

    def test_fan_activation_deferral_has_no_pending_or_retry_but_off_proceeds(self):
        self.ha.defer = True
        for now in (0, 60):
            plan = Plan()
            self.executor.fan(self.fan, 60, plan, self.ha.states(), now)
            self.assertEqual(plan.warnings, [])
            self.assertNotIn(self.fan, self.executor.pending)
            self.assert_no_device_failure()
        self.assertEqual(self.ha.calls, [])
        self.ha.data[self.fan].update(state="on")
        self.ha.data[self.fan]["attributes"]["percentage"] = 60
        self.executor.fan(self.fan, 0, Plan(), self.ha.states(), 75)
        self.assertEqual(self.ha.data[self.fan]["state"], "off")
        self.assertEqual(self.ha.calls[-1][1], "turn_off")

    def test_deferred_fan_retarget_preserves_pending_acknowledgement(self):
        self.ha.ignore.add(self.fan)
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), 0)
        original = self.executor.pending[self.fan]
        original_values = copy.deepcopy(vars(original))
        self.ha.defer = True
        self.executor.fan(self.fan, 70, Plan(), self.ha.states(), 15)
        self.assertIs(self.executor.pending[self.fan], original)
        self.assertEqual(vars(original), original_values)
        self.assertEqual(self.executor.fan_failures, {})
        # An already dispatched fan command still has its ordinary deadline.
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), 45)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertEqual(self.executor.fan_failures[self.fan].attempts, 1)

    def test_deferred_fan_retry_does_not_increase_or_reset_backoff(self):
        self.ha.ignore.add(self.fan)
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), 0)
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), 45)
        failure = self.executor.fan_failures[self.fan]
        original_values = copy.deepcopy(vars(failure))
        self.ha.calls.clear()
        self.ha.defer = True
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), failure.retry_at)
        self.assertIs(self.executor.fan_failures[self.fan], failure)
        self.assertEqual(vars(failure), original_values)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertEqual(self.ha.calls, [])
        self.ha.ignore.remove(self.fan)
        self.executor.fan(self.fan, 0, Plan(), self.ha.states(), failure.retry_at + 1)
        self.assertEqual(self.ha.calls[-1][1], "turn_off")
        self.executor.fan(self.fan, 0, Plan(), self.ha.states(), failure.retry_at + 15)
        self.assertNotIn(self.fan, self.executor.fan_failures)
        self.assertNotIn(self.fan, self.executor.pending)

    def test_ordinary_transport_failures_still_create_local_recovery_and_retry(self):
        self.ha.defer = True
        self.ha.error_type = HAError
        self.step(0)
        self.assertIn(self.head, self.executor.pending)
        self.assertFalse(self.executor.pending[self.head].receipt)
        self.assertIn(self.head, self.executor.recoveries)
        self.assertIn(self.head, self.executor.device_issues)
        self.executor.fan(self.fan, 60, Plan(), self.ha.states(), 0)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertEqual(self.executor.fan_failures[self.fan].attempts, 1)


if __name__ == "__main__":
    unittest.main()
