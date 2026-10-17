"""Dyson-like fans expose independent power and remembered speed."""
import unittest

from house_climate.executor import Executor
from house_climate.policy import Plan
from tests.test_controller import FakeHA, config, fixture


class SeparatePowerFanHA(FakeHA):
    """Setting speed stores it while off; turn_on explicitly supplies power."""

    def __init__(self, states, fan, *, power_immediate=True, speed_immediate=True):
        super().__init__(states)
        self.fan_entity = fan
        self.power_immediate = power_immediate
        self.speed_immediate = speed_immediate

    def service(self, domain, service, **data):
        if domain != "fan" or data["entity_id"] != self.fan_entity:
            return super().service(domain, service, **data)
        self.calls.append((domain, service, data))
        if self.fan_entity in self.ignore:
            return
        state = self.data[self.fan_entity]
        if service == "turn_on":
            if self.power_immediate:
                state["state"] = "on"
            if self.speed_immediate and "percentage" in data:
                state["attributes"]["percentage"] = data["percentage"]
        elif service == "set_percentage":
            # This is the real Dyson contract that the ordinary FakeHA hides.
            if self.speed_immediate:
                state["attributes"]["percentage"] = data["percentage"]
        elif service == "turn_off":
            state["state"] = "off"


class FanPowerTests(unittest.TestCase):
    def setup_fan(self, *, remembered=20, **kwargs):
        self.cfg = config()
        self.fan = self.cfg.fans["primary"]
        data = fixture(self.cfg)
        data[self.fan]["attributes"]["percentage"] = remembered
        self.ha = SeparatePowerFanHA(data, self.fan, **kwargs)
        self.executor = Executor(self.cfg, self.ha, "°F")

    def step(self, now, percentage=60, *, cooling=False):
        plan = Plan(fans={self.fan: percentage})
        if cooling:
            plan.mode = "cool"
            plan.head_targets_f["bonus"] = 72
        self.executor.step(plan, self.ha.states(), now)
        return plan

    def calls(self):
        return [call for call in self.ha.calls
                if call[0] == "fan" and call[2]["entity_id"] == self.fan]

    def test_matching_remembered_speed_still_requires_explicit_power_on(self):
        self.setup_fan(remembered=60)
        self.step(0)
        self.assertEqual(self.ha.data[self.fan]["state"], "on")
        self.assertTrue(any(call[1] == "turn_on" for call in self.calls()))
        self.step(15)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertNotIn(self.fan, self.executor.fan_failures)
        self.assertEqual(len(self.calls()), 1)

    def test_split_power_and_speed_reports_keep_original_acknowledgement_deadline(self):
        for power_first in (False, True):
            with self.subTest(power_first=power_first):
                self.setup_fan(power_immediate=power_first, speed_immediate=not power_first)
                self.step(0)
                self.assertEqual(self.executor.pending[self.fan].since, 0)
                if power_first:
                    self.assertEqual(self.ha.data[self.fan]["state"], "on")
                    self.assertEqual(self.ha.data[self.fan]["attributes"]["percentage"], 20)
                else:
                    self.assertEqual(self.ha.data[self.fan]["state"], "off")
                    self.assertEqual(self.ha.data[self.fan]["attributes"]["percentage"], 60)
                self.step(15)
                self.assertEqual(self.executor.pending[self.fan].since, 0)
                self.assertEqual(len(self.calls()), 1)
                self.ha.data[self.fan]["state"] = "on"
                self.ha.data[self.fan]["attributes"]["percentage"] = 60
                self.step(30)
                self.assertNotIn(self.fan, self.executor.pending)
                self.assertNotIn(self.fan, self.executor.fan_failures)
                self.assertEqual(len(self.calls()), 1)

    def test_off_with_correct_speed_times_out_and_recovers_after_bounded_retry(self):
        self.setup_fan(power_immediate=False)
        self.step(0)
        self.assertEqual(self.ha.data[self.fan]["state"], "off")
        self.assertEqual(self.ha.data[self.fan]["attributes"]["percentage"], 60)
        warning = self.step(45)
        self.assertIn(self.fan, self.executor.fan_failures)
        self.assertTrue(any("reported off (60%)" in text for text in warning.warnings))
        failure = self.executor.fan_failures[self.fan]
        sent = len(self.calls())
        self.step(failure.retry_at - 1)
        self.assertEqual(len(self.calls()), sent)
        self.ha.power_immediate = True
        retry = self.step(failure.retry_at)
        self.assertEqual(self.ha.data[self.fan]["state"], "on")
        self.assertEqual(len(self.calls()), sent + 1)
        self.assertTrue(any(self.fan in text for text in retry.warnings))
        self.step(failure.retry_at + 15)
        self.assertNotIn(self.fan, self.executor.fan_failures)
        self.assertNotIn(self.fan, self.executor.pending)

    def test_explicit_off_supersedes_power_retry_even_if_report_is_already_off(self):
        self.setup_fan(power_immediate=False)
        self.step(0)
        self.step(45)
        self.assertIn(self.fan, self.executor.fan_failures)
        self.step(46, percentage=0)
        self.assertEqual(self.calls()[-1][1], "turn_off")
        self.assertIn(self.fan, self.executor.pending)
        self.step(61, percentage=0)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertNotIn(self.fan, self.executor.fan_failures)
        sent = len(self.calls())
        self.step(400, percentage=0)
        self.assertEqual(len(self.calls()), sent)

    def test_power_ack_failure_does_not_pause_operating_heat_pump(self):
        self.setup_fan(power_immediate=False)
        for now in (0, 15, 45, 90, 135, 195, 210):
            plan = self.step(now, cooling=True)
        self.assertEqual(self.executor.phase, "running")
        self.assertEqual(self.ha.data[self.cfg.heads["bonus"]]["state"], "cool")
        self.assertTrue(any(self.fan in text for text in plan.warnings))
        self.assertNotIn(self.fan, self.executor.device_issues)
        activations = [call for call in self.ha.calls if call[1] == "set_temperature"
                       and call[2]["entity_id"] == self.cfg.heads["bonus"]]
        self.assertEqual(len(activations), 1)


if __name__ == "__main__":
    unittest.main()
