"""Distinguish controller intent, service receipts and delayed head reports."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from house_climate.diagnostics import HEAD_TRANSITION_LIMIT
from house_climate.executor import Executor
from house_climate.ha import HAActivationDeferred, HAError
from tests import test_cycle_guards as fixtures


class HeadTransitionHistoryTests(unittest.TestCase):
    request = fixtures.HeadCycleGuardTests.request
    step = fixtures.HeadCycleGuardTests.step
    start = fixtures.HeadCycleGuardTests.start

    def setUp(self):
        fixtures.HeadCycleGuardTests.setUp(self)
        self.cfg = replace(self.cfg, head_minimum_run_seconds=0)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"

    def history(self, entity=None):
        return self.executor.recovery_status(30)["head_transition_history"].get(entity or self.guest, [])

    def test_service_return_does_not_claim_device_acknowledgement(self):
        self.step(0)
        events = self.history()
        requested = next(item for item in events if item["event"] == "command_requested")
        receipt = next(item for item in events if item["event"] == "service_returned")
        self.assertEqual(receipt["command_id"], requested["sequence"])
        self.assertEqual(requested["data"], {"hvac_mode": "cool", "temperature": 72})
        self.assertFalse(any(item["event"] == "command_acknowledged" for item in events))
        self.step(15)
        acknowledged = next(item for item in self.history() if item["event"] == "command_acknowledged")
        self.assertEqual(acknowledged["command_id"], requested["sequence"])
        self.assertEqual((acknowledged["observed_mode"], acknowledged["observed_target_temperature"]), ("cool", 72))

    def test_spontaneous_off_is_not_misreported_as_controller_off_request(self):
        self.start()
        self.executor.config = replace(self.cfg, head_minimum_off_seconds=180)
        previous_sequence = self.history()[-1]["sequence"]
        self.ha.data[self.guest].update(state="off", last_reported="external_off_report")
        self.step(30)
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertEqual(reason["kind"], "reported_off")
        self.assertFalse(reason["controller_off_requested"])
        events = [item for item in self.history() if item["sequence"] > previous_sequence]
        self.assertTrue(any(item["event"] == "state_reported" and item["observed_mode"] == "off" for item in events))
        self.assertFalse(any(item["event"] == "command_requested" for item in events))

    def test_controller_off_request_is_identified_separately(self):
        self.start()
        self.step(30, heads={"bonus": 72})
        reason = self.executor.head_stop_reasons[self.guest]
        self.assertTrue(reason["controller_off_requested"])
        off = [item for item in self.history() if item["event"] == "command_requested"
               and item["service"] == "set_hvac_mode"]
        self.assertEqual(len(off), 1)
        self.assertEqual(off[0]["data"], {"hvac_mode": "off"})

    def test_pending_activation_off_report_retains_actual_pending_command(self):
        self.start()
        self.step(30, heads={"guest": 73})
        pending = self.executor.pending[self.guest]
        self.ha.data[self.guest].update(state="off", last_reported="late_old_off")
        self.step(45, heads={"guest": 73})
        self.assertNotIn(self.guest, self.executor.head_stop_reasons)
        self.assertIs(self.executor.pending[self.guest], pending)
        last = next(item for item in reversed(self.history()) if item["event"] == "state_reported")
        self.assertEqual((last["observed_mode"], last["requested_mode"]), ("off", "cool"))

    def test_identical_polls_do_not_churn_or_evict_transition_evidence(self):
        self.start()
        expected = deepcopy(self.history())
        for now in range(30, 900, 15):
            self.ha.data[self.guest]["last_reported"] = f"same_value_report_{now}"
            self.step(now)
        self.assertEqual(self.history(), expected)

    def test_transition_output_is_bounded_and_detached(self):
        self.start()
        matches = lambda observed: observed.get("state") == "cool" and observed["attributes"].get("temperature") == target
        for index in range(20):
            target = 65 + index % 10
            self.executor.ensure(self.guest, "set_temperature", {"hvac_mode": "cool", "temperature": target},
                                 matches, self.ha.states(), 30 + index * 30)
            self.executor.ensure(self.guest, "set_temperature", {"hvac_mode": "cool", "temperature": target},
                                 matches, self.ha.states(), 45 + index * 30)
        events = self.history()
        self.assertLessEqual(len(events), HEAD_TRANSITION_LIMIT)
        self.assertEqual(len({item["sequence"] for item in events}), len(events))
        events[-1]["event"] = "caller_mutated"
        self.assertNotEqual(self.history()[-1]["event"], "caller_mutated")
        json.dumps(self.executor.recovery_status(900), allow_nan=False)

    def test_transport_error_records_uncertainty_without_an_acknowledgement(self):
        self.ha.ignore.add(self.guest)
        with patch.object(self.ha, "service", side_effect=HAError("transport failure")):
            with self.assertRaises(HAError):
                self.executor.ensure(self.guest, "set_temperature", {"hvac_mode": "cool", "temperature": 72},
                                     lambda observed: False, self.ha.states(), 0)
        events = self.history()
        requested = next(item for item in events if item["event"] == "command_requested")
        failure = next(item for item in events if item["event"] == "service_error")
        self.assertEqual(failure["command_id"], requested["sequence"])
        self.assertEqual(failure["error_type"], "HAError")
        self.assertFalse(any(item["event"] in {"service_returned", "command_acknowledged"} for item in events))

    def test_deferred_undispatched_command_does_not_become_a_recovery(self):
        with patch.object(self.ha, "service", side_effect=HAActivationDeferred("queued call abandoned")):
            self.executor.climate(self.guest, "cool", 72, self.ha.states(), 0)
        self.assertFalse(self.executor.pending)
        self.assertFalse(self.executor.recoveries)
        self.assertTrue(any(item["event"] == "dispatch_deferred" for item in self.history()))
        self.assertFalse(any(item["event"] == "service_error" for item in self.history()))

    def test_loss_of_known_capability_is_diagnostic_not_a_new_recovery_trigger(self):
        self.start()
        self.ha.data[self.guest]["attributes"]["hvac_modes"] = ["off", "cool"]
        self.assertFalse(self.executor._supports(self.guest, "heat", self.ha.states()))
        detail = self.executor.recovery_status(30)["devices"][self.guest]
        self.assertEqual(detail["previously_supported_modes"], ["heat"])
        self.assertEqual(detail["kind"], "capability")
        self.assertFalse(self.executor.recoveries)

    def test_genuinely_cooling_only_head_does_not_gain_invented_heat_capability(self):
        self.ha.data[self.guest]["attributes"]["hvac_modes"] = ["off", "cool"]
        self.assertFalse(self.executor._supports(self.guest, "heat", self.ha.states()))
        detail = self.executor.recovery_status(30)["devices"][self.guest]
        self.assertEqual(detail["previously_supported_modes"], [])
        self.assertEqual(detail["kind"], "capability")

    def test_unavailable_metadata_is_not_learned_as_trustworthy_capability(self):
        self.ha.data[self.guest]["state"] = "unavailable"
        self.executor.head_transitions.observe_capabilities(self.guest, self.ha.data[self.guest])
        self.ha.data[self.guest]["state"] = "off"
        self.ha.data[self.guest]["attributes"]["hvac_modes"] = ["off", "cool"]
        self.assertFalse(self.executor._supports(self.guest, "heat", self.ha.states()))
        self.assertEqual(self.executor.capability_blocks[self.guest]["previously_supported_modes"], [])


if __name__ == "__main__":
    unittest.main()
