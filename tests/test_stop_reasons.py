"""Keep evidence of why a head stopped after transient waits disappear."""
import json
import unittest
from copy import deepcopy
from dataclasses import replace

from house_climate.executor import Executor
from tests import test_cycle_guards as cycle_fixtures


class StopReasonTests(unittest.TestCase):
    request = cycle_fixtures.HeadCycleGuardTests.request
    step = cycle_fixtures.HeadCycleGuardTests.step
    start = cycle_fixtures.HeadCycleGuardTests.start
    start_two_with_off_guard = cycle_fixtures.HeadCycleGuardTests.start_two_with_off_guard

    def setUp(self):
        cycle_fixtures.HeadCycleGuardTests.setUp(self)
        self.cfg = replace(self.cfg, head_minimum_run_seconds=0)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"

    def test_safety_stop_reason_survives_deduped_off_polls_and_later_activation(self):
        self.start()
        plan = self.request(30, heads={"bonus": 72})
        plan.inhibited_heads["guest"] = "Cooling stopped at opposite room comfort limit"
        self.executor.step(plan, self.ha.states(), 30)
        reason = deepcopy(self.executor.head_stop_reasons[self.guest])
        self.assertEqual(reason["kind"], "safety")
        self.assertIn("opposite room comfort limit", reason["reason"])
        self.assertEqual(reason["requested_mode"], "cool")
        self.assertEqual(reason["requested_target_temperature"], 72)
        self.assertEqual(reason["observed_mode"], "cool")
        self.step(45, heads={"bonus": 72})
        self.assertEqual(self.executor.head_stop_reasons[self.guest], reason)
        self.step(60, heads={"bonus": 72, "guest": 72})
        self.step(75, heads={"bonus": 72, "guest": 72})
        self.assertEqual(self.executor.head_stop_reasons[self.guest], reason)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")

    def test_shared_direction_change_and_no_demand_have_specific_reasons(self):
        self.start()
        self.step(30, "heat", {"primary": 68})
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertEqual(reason["kind"], "changeover")
        self.assertIn("from cool to heat", reason["reason"])
        self.setUp()
        self.start()
        self.step(30, "off", {})
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertEqual(reason["kind"], "no_demand")
        self.assertIn("no eligible shared-system demand", reason["reason"])

    def test_startup_and_shutdown_records_survive_shutdown_cleanup(self):
        self.ha.data[self.guest]["state"] = "heat"
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.step(0)
        self.assertEqual(self.executor.head_stop_reasons[self.guest]["kind"], "startup")
        self.executor.shutdown(reason="Controller disabled")
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertEqual((reason["kind"], reason["reason"]), ("shutdown", "Controller disabled"))
        self.assertIn(self.guest, self.executor.recovery_status(15)["head_stop_reasons"])
        self.assertEqual(Executor(self.cfg, self.ha, "°F").head_stop_reasons, {})

    def test_observed_off_while_still_requested_is_retained_without_guessing_cause(self):
        self.start()
        self.cfg = replace(self.cfg, head_minimum_off_seconds=180)
        self.executor.config = self.cfg
        self.ha.data[self.guest]["state"] = "off"
        self.step(30)
        reason = deepcopy(self.executor.head_stop_reasons[self.guest])
        self.assertEqual(reason["kind"], "reported_off")
        self.assertEqual(reason["reason"], "Head reported off while cool was still requested")
        self.assertEqual(reason["requested_target_temperature"], 72)
        self.assertEqual(reason["observed_mode"], "off")
        hold = self.executor.cycle_holds[self.guest]
        self.assertEqual(hold["stop_reason"], reason["reason"])
        self.assertEqual(hold["stop_kind"], "reported_off")
        self.step(45)
        self.assertEqual(self.executor.head_stop_reasons[self.guest], reason)
        json.dumps(self.executor.recovery_status(45))

    def test_pending_activation_off_report_is_not_labeled_spontaneous(self):
        self.start()
        self.step(30, heads={"guest": 73})
        self.assertIn(self.guest, self.executor.pending)
        self.ha.data[self.guest]["state"] = "off"
        self.step(45, heads={"guest": 73})
        self.assertNotIn(self.guest, self.executor.head_stop_reasons)

    def test_pending_controller_off_is_not_labeled_spontaneous_when_ack_arrives(self):
        self.start(heads={"guest": 72, "bonus": 72})
        self.cfg = replace(self.cfg, head_minimum_off_seconds=180)
        self.executor.config = self.cfg
        self.ha.ignore.add(self.guest)
        self.step(30, heads={"bonus": 72})
        reason = deepcopy(self.executor.head_stop_reasons[self.guest])
        self.assertIn(self.guest, self.executor.pending)
        self.ha.ignore.remove(self.guest)
        self.ha.data[self.guest].update(state="off", last_reported="delayed_off_ack")
        self.step(45, heads={"guest": 72, "bonus": 72})
        self.assertEqual(self.executor.head_stop_reasons[self.guest], reason)
        self.assertNotEqual(reason["kind"], "reported_off")

    def test_activation_timeout_preserves_requested_and_reported_command_details(self):
        self.start()
        self.step(30, heads={"guest": 64})
        self.ha.data[self.guest]["attributes"]["temperature"] = 65
        self.ha.ignore.add(self.guest)
        self.step(75, heads={"guest": 64})
        message = self.executor.device_issues[self.guest]
        self.assertIn("requested service=set_temperature mode=cool target=64°F", message)
        self.assertIn("reported mode=cool target=65°F", message)
        self.assertIn("last report=", message)
        self.assertIn("s ago)", message)
        details = self.executor.recovery_status(75)["devices"][self.guest]
        self.assertEqual(details["pending_command"],
                         {"service": "set_temperature", "data": {"hvac_mode": "cool", "temperature": 64}})
        self.step(90, heads={"guest": 64})
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertEqual(reason["kind"], "recovery")
        self.assertEqual(reason["requested_target_temperature"], 64)
        self.assertEqual(reason["observed_target_temperature"], 65)
        self.step(105, heads={"guest": 64})
        details = self.executor.recovery_status(105)["devices"][self.guest]
        self.assertEqual(details["pending_command"]["service"], "set_temperature")
        self.assertEqual(self.executor.head_stop_reasons[self.guest]["requested_target_temperature"], 64)

    def test_off_timeout_is_distinguished_from_an_activation_timeout(self):
        self.start()
        self.ha.ignore.add(self.guest)
        self.step(30, heads={"bonus": 72})
        self.step(75, heads={"bonus": 72})
        message = self.executor.device_issues[self.guest]
        self.assertIn("requested service=set_hvac_mode mode=off target=unchanged", message)
        self.assertIn("reported mode=cool target=72°F", message)
        details = self.executor.recovery_status(75)["devices"][self.guest]
        self.assertEqual(details["pending_command"],
                         {"service": "set_hvac_mode", "data": {"hvac_mode": "off"}})
