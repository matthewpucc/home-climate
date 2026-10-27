"""A quick owner reload cannot reuse a pre-reload cooling acknowledgement."""
from dataclasses import replace
import unittest

from house_climate.executor import Executor
from tests import test_cycle_guards as fixtures


class RepairEpochCycleTests(unittest.TestCase):
    setUp = fixtures.HeadCycleGuardTests.setUp
    request = fixtures.HeadCycleGuardTests.request
    step = fixtures.HeadCycleGuardTests.step
    start = fixtures.HeadCycleGuardTests.start

    def test_quick_reload_restarts_only_affected_head_off_timing_without_unavailable(self):
        calls = {"guest": 72, "bonus": 72}
        self.start(calls)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.ha.data[self.guest]["integration_repair"] = {"epoch": 1, "held": True, "off_since": None}
        self.step(30, heads=calls)
        self.assertIsNone(self.executor.head_cycles[self.guest].run_since)
        # Native repair obtains Off without any sampled unavailable transition.
        self.ha.data[self.guest]["state"] = "off"
        self.step(45, heads=calls)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.assertEqual(self.executor.head_stop_reasons[self.guest]["kind"], "integration_repair")
        self.cfg = replace(self.cfg, head_minimum_off_seconds=180)
        self.executor.config = self.cfg
        self.ha.data[self.guest]["integration_repair"] = {"epoch": 2, "held": False, "off_since": 45}
        self.step(60, heads=calls)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertEqual(self.executor.head_cycles[self.guest].off_since, 45)
        self.assertEqual(self.executor.cycle_holds[self.guest]["remaining_seconds"], 165)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.step(225, heads=calls)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertIsNone(self.executor.head_cycles[self.guest].run_since)


if __name__ == "__main__":
    unittest.main()
