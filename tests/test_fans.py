"""Circulation fans degrade independently from shared climate control."""
from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from house_climate.__main__ import preflight, run_loop
from house_climate.executor import ControlFault, Executor
from house_climate.ha import HAError
from house_climate.policy import Plan
from tests.test_controller import FakeHA, add_helpers, config, fixture
from tests.test_runtime import BoundedEvent


class FailingFanHA(FakeHA):
    """A Dyson transport failure does not affect unrelated HA services."""

    def __init__(self, states, fan):
        super().__init__(states)
        self.failed_fan = fan

    def service(self, domain, service, **data):
        if data["entity_id"] == self.failed_fan:
            self.calls.append((domain, service, data))
            raise HAError("Dyson operation timed out")
        return super().service(domain, service, **data)


class CirculationFanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.ha = FakeHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.fan = self.cfg.fans["office"]
        self.other_fan = self.cfg.fans["primary"]

    def plan(self, percentage=60, **kwargs):
        return Plan(fans={self.fan: percentage}, **kwargs)

    def step(self, now, plan=None):
        plan = plan if plan is not None else self.plan()
        self.executor.step(plan, self.ha.states(), now)
        return plan

    def fan_calls(self, entity=None):
        return [call for call in self.ha.calls
                if call[0] == "fan" and call[2]["entity_id"] == (entity or self.fan)]

    def assert_fan_warning(self, plan):
        prefix = f"Circulation fan {self.fan}:"
        self.assertTrue(any(item.startswith(prefix) and "retrying independently" in item
                            for item in plan.warnings), plan.warnings)

    def start_cooling(self):
        for now in (0, 15, 195, 210):
            self.step(now, Plan(mode="cool", head_targets_f={"bonus": 72}))

    def test_unresponsive_fan_does_not_block_heads_or_other_fans(self):
        self.ha.ignore.add(self.fan)
        for now in (0, 15, 45, 195, 210, 240):
            plan = Plan(mode="cool", head_targets_f={"bonus": 72},
                        fans={self.fan: 60, self.other_fan: 60})
            self.step(now, plan)
        self.assertEqual(self.executor.phase, "running")
        self.assertEqual(self.ha.data[self.cfg.heads["bonus"]]["state"], "cool")
        self.assertEqual(self.ha.data[self.other_fan]["attributes"]["percentage"], 60)
        self.assert_fan_warning(plan)
        activations = [call for call in self.ha.calls if call[1] == "set_temperature"
                       and call[2]["entity_id"] == self.cfg.heads["bonus"]]
        self.assertEqual(len(activations), 1)

    def test_shared_off_barrier_runs_before_expired_fan_acknowledgement(self):
        self.start_cooling()
        self.ha.ignore.add(self.fan)
        self.step(255, self.plan(mode="cool", head_targets_f={"bonus": 72}))
        self.ha.calls.clear()
        plan = self.step(300, self.plan(mode="heat", head_targets_f={"primary": 68}))
        self.assertEqual(self.executor.phase, "stopping")
        self.assertEqual(self.ha.data[self.cfg.heads["bonus"]]["state"], "off")
        self.assert_fan_warning(plan)
        self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))

    def test_timeout_backoff_and_actual_acknowledgement_recovery(self):
        self.ha.ignore.add(self.fan)
        self.step(0)
        plan = self.step(45)
        self.assert_fan_warning(plan)
        self.assertNotIn(self.fan, self.executor.pending)
        failure = self.executor.fan_failures[self.fan]
        self.assertEqual(failure.attempts, 1)
        self.assertGreaterEqual(failure.retry_at, 90)
        sent = len(self.fan_calls())
        plan = self.step(failure.retry_at - 1)
        self.assertEqual(len(self.fan_calls()), sent)
        self.assert_fan_warning(plan)
        self.ha.ignore.remove(self.fan)
        plan = self.step(failure.retry_at)
        self.assertEqual(len(self.fan_calls()), sent + 1)
        self.assert_fan_warning(plan)
        self.assertIn(self.fan, self.executor.fan_failures)
        plan = self.step(failure.retry_at + 15)
        self.assertNotIn(self.fan, self.executor.fan_failures)
        self.assertNotIn(self.fan, self.executor.pending)
        self.assertFalse(any(self.fan in item for item in plan.warnings))
        sent = len(self.fan_calls())
        self.step(failure.retry_at + 30)
        self.assertEqual(len(self.fan_calls()), sent)

    def test_off_supersedes_speed_retry_without_waiting_for_backoff(self):
        self.ha.data[self.fan].update(state="on")
        self.ha.data[self.fan]["attributes"]["percentage"] = 20
        self.ha.ignore.add(self.fan)
        self.step(0)
        self.step(45)
        self.assertIn(self.fan, self.executor.fan_failures)
        self.ha.ignore.remove(self.fan)
        self.step(46, self.plan(0))
        self.assertEqual(self.fan_calls()[-1][1], "turn_off")
        self.assertEqual(self.ha.data[self.fan]["state"], "off")
        self.step(61, self.plan(0))
        self.assertNotIn(self.fan, self.executor.fan_failures)
        sent = len(self.fan_calls())
        self.step(400, self.plan(0))
        self.assertEqual(len(self.fan_calls()), sent)

    def test_off_supersedes_timed_out_activation_even_when_snapshot_still_off(self):
        self.ha.ignore.add(self.fan)
        self.step(0)
        self.step(45)
        self.assertEqual(self.ha.data[self.fan]["state"], "off")
        self.ha.ignore.remove(self.fan)
        self.step(46, self.plan(0))
        self.assertEqual(self.fan_calls()[-1][1], "turn_off")
        self.assertIn(self.fan, self.executor.pending)
        self.step(61, self.plan(0))
        self.assertNotIn(self.fan, self.executor.fan_failures)
        self.assertNotIn(self.fan, self.executor.pending)

    def test_new_off_attempts_even_if_failed_fan_has_become_unavailable(self):
        self.ha.ignore.add(self.fan)
        self.step(0)
        self.step(45)
        self.ha.data[self.fan]["state"] = "unavailable"
        self.ha.ignore.remove(self.fan)
        self.step(46, self.plan(0))
        self.assertEqual(self.fan_calls()[-1][1], "turn_off")
        self.assertEqual(self.ha.data[self.fan]["state"], "off")
        self.step(61, self.plan(0))
        self.assertNotIn(self.fan, self.executor.fan_failures)

    def test_service_failures_back_off_and_do_not_cycle_operating_head(self):
        self.ha = FailingFanHA(fixture(self.cfg), self.fan)
        self.executor = Executor(self.cfg, self.ha, "°F")
        for now in range(0, 901, 15):
            plan = self.step(now, self.plan(mode="cool", head_targets_f={"bonus": 72}))
        self.assert_fan_warning(plan)
        self.assertLessEqual(len(self.fan_calls()), 7)
        self.assertGreaterEqual(len(self.fan_calls()), 3)
        self.assertEqual(self.ha.data[self.cfg.heads["bonus"]]["state"], "cool")
        activations = [call for call in self.ha.calls if call[1] == "set_temperature"
                       and call[2]["entity_id"] == self.cfg.heads["bonus"]]
        self.assertEqual(len(activations), 1)
        self.assertLessEqual(self.executor.fan_failures[self.fan].retry_at - 900, 300)

    def test_invalid_metadata_and_unavailable_fans_only_raise_fan_warnings(self):
        variants = [
            {"state": "unavailable"},
            {"state": "unknown"},
            {"attributes": {"supported_features": 0}},
            *({"attributes": {"percentage_step": value}}
              for value in (None, "bad", math.nan, math.inf, -1, 0, 101)),
        ]
        for change in variants:
            with self.subTest(change=change):
                ha = FakeHA(fixture(self.cfg))
                if "state" in change:
                    ha.data[self.fan]["state"] = change["state"]
                ha.data[self.fan]["attributes"].update(change.get("attributes", {}))
                executor = Executor(self.cfg, ha, "°F")
                plan = Plan(mode="cool", head_targets_f={"bonus": 72},
                            fans={self.fan: 60, self.other_fan: 60})
                executor.step(plan, ha.states(), 0)
                self.assert_fan_warning(plan)
                self.assertEqual(ha.data[self.other_fan]["state"], "on")
                self.assertEqual(executor.phase, "stopping")

    def test_fractional_steps_send_integer_floor_and_accept_integer_reports(self):
        self.ha.data[self.fan]["attributes"]["percentage_step"] = 100 / 3
        self.step(0)
        command = self.fan_calls()[0][2]["percentage"]
        self.assertIs(type(command), int)
        self.assertEqual(command, 66)
        for observed in (66, 67):
            with self.subTest(observed=observed):
                self.ha.data[self.fan]["attributes"]["percentage"] = observed
                self.ha.calls.clear()
                self.step(15)
                self.assertEqual(self.fan_calls(), [])
                self.assertNotIn(self.fan, self.executor.fan_failures)

    def test_adjacent_speeds_are_not_acknowledged(self):
        for step, observed in ((100 / 3, 33), (100 / 3, 100),
                               (10, 50), (10, 70), (1, 59), (1, 61)):
            with self.subTest(step=step, observed=observed):
                ha = FakeHA(fixture(self.cfg), ignore={self.fan})
                ha.data[self.fan]["state"] = "on"
                ha.data[self.fan]["attributes"].update(
                    percentage_step=step, percentage=observed)
                executor = Executor(self.cfg, ha, "°F")
                executor.step(self.plan(), ha.states(), 0)
                plan = self.plan()
                executor.step(plan, ha.states(), 45)
                self.assert_fan_warning(plan)
                self.assertIn(self.fan, executor.fan_failures)

    def test_invalid_observed_percentages_do_not_acknowledge(self):
        for value in (True, False, math.nan, math.inf, None, "1"):
            with self.subTest(value=value):
                ha = FakeHA(fixture(self.cfg), ignore={self.fan})
                ha.data[self.fan]["state"] = "on"
                ha.data[self.fan]["attributes"].update(percentage_step=1, percentage=value)
                executor = Executor(self.cfg, ha, "°F")
                executor.step(self.plan(1), ha.states(), 0)
                plan = self.plan(1)
                executor.step(plan, ha.states(), 45)
                self.assert_fan_warning(plan)

    def test_preflight_accepts_missing_circulation_fan(self):
        states = fixture(self.cfg)
        add_helpers(states)
        states.pop(self.fan)
        preflight(self.cfg, states, self.executor)

    def test_shared_climate_acknowledgement_failures_remain_fatal(self):
        self.ha.ignore.add(self.cfg.heads["bonus"])
        self.start_cooling()
        with self.assertRaises(ControlFault):
            self.step(240, Plan(mode="cool", head_targets_f={"bonus": 72}))

    def test_auxiliary_heater_acknowledgement_failures_remain_fatal(self):
        self.ha.ignore.add(self.cfg.heater_entity)
        self.step(0, Plan(heater_on=True))
        with self.assertRaises(ControlFault):
            self.step(45, Plan(heater_on=True))

    def test_startup_fan_transport_failure_does_not_abort_live_control(self):
        cfg = replace(self.cfg, equipment_timing_confirmed=True,
                      heater_setpoint_confirmed=True)
        states = fixture(cfg, datetime.now(timezone.utc), guest=76, living=67)
        add_helpers(states)
        states[cfg.entity("enabled")] = {"state": "on"}
        states[cfg.entity("heater_owned")] = {"state": "off"}
        ha = FailingFanHA(states, self.fan)
        ha.request = Mock(return_value={
            "time_zone": "America/Los_Angeles",
            "unit_system": {"temperature": "°F"},
        })
        reports = []
        args = SimpleNamespace(live=True, once=False, snapshot=None, at=None)
        run_loop(args, cfg, ha_client=ha, stop_event=BoundedEvent(3),
                 event_callback=lambda event, **data: reports.append((event, data)))
        self.assertTrue(any(event == "arming" for event, _ in reports), reports)
        self.assertTrue(any(event == "live" for event, _ in reports), reports)
        self.assertFalse(any(event == "fault" for event, _ in reports), reports)
        self.assertTrue(any(call[1] == "set_datetime" for call in ha.calls))


if __name__ == "__main__":
    unittest.main()
