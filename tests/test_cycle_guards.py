"""Head-mode timers reduce cycling without extending unsafe conditioning."""
import unittest
from dataclasses import replace
from datetime import timedelta

from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.policy import Policy
from tests.test_controller import FakeHA, NIGHT, config, fixture


class HeadCycleGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(config(), head_minimum_run_seconds=300,
                           head_minimum_off_seconds=0, off_settle_seconds=180)
        self.ha = FakeHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")
        # Isolate head cycling within an already established shared direction.
        self.executor.phase, self.executor.direction = "running", "cool"
        self.guest = self.cfg.heads["guest"]
        self.bonus = self.cfg.heads["bonus"]

    def request(self, now, mode="cool", heads=None):
        stamp = NIGHT + timedelta(seconds=now)
        for room in self.cfg.zones.values():
            for probe in room.probes:
                self.ha.data[probe.entity_id]["last_reported"] = stamp.isoformat()
        states = self.ha.states()
        plan = Policy(self.cfg).decide(states, stamp, Settings.read(states, self.cfg))
        plan.mode = mode
        plan.head_targets_f = dict({"guest": 72} if heads is None else heads)
        plan.heater_on = plan.auxiliary_fan_only = False
        plan.fans = {}
        return plan

    def step(self, now, mode="cool", heads=None):
        plan = self.request(now, mode, heads)
        self.executor.step(plan, self.ha.states(), now)
        return plan

    def start(self, heads=None):
        self.step(0, heads=heads)
        self.assertIsNone(self.executor.head_cycles[self.guest].run_since)
        self.step(15, heads=heads)
        self.ha.calls.clear()

    def test_acknowledged_run_continues_for_five_minutes_then_stops(self):
        self.start()
        self.assertEqual(self.executor.head_cycles[self.guest].run_since, 15)
        plan = self.step(30, "off", {})
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.head_targets_f, {"guest": 72})
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        hold = self.executor.recovery_status(30)["cycle_holds"][self.guest]
        self.assertEqual(hold["kind"], "minimum_run")
        self.assertEqual(hold["remaining_seconds"], 285)
        self.assertFalse(any(service == "set_hvac_mode" for _, service, _ in self.ha.calls))
        self.step(315, "off", {})
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertNotIn(self.guest, self.executor.cycle_holds)

    def test_confirmed_opposite_comfort_edge_stops_before_minimum_run(self):
        self.start()
        plan = self.request(30, "off", {})
        plan.rooms["guest"].update(temperature=71.5, low=71.5, high=71.5,
                                    confirmed_low=71.5, confirmed_high=71.5)
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertNotIn(self.guest, self.executor.cycle_holds)

    def test_unconfirmed_probe_outlier_does_not_defeat_run_hold_but_hard_limit_does(self):
        self.start()
        plan = self.request(30, "off", {})
        plan.rooms["guest"].update(temperature=72, low=71.5, high=73,
                                    confirmed_low=72, confirmed_high=72)
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.executor.cycle_holds[self.guest]["kind"], "minimum_run")
        plan = self.request(45, "off", {})
        plan.rooms["guest"].update(temperature=72, low=66.25, high=73,
                                    confirmed_low=72, confirmed_high=72)
        self.executor.step(plan, self.ha.states(), 45)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")

    def test_missing_or_stale_room_readings_bypass_run_hold(self):
        for missing in ("missing", "stale"):
            with self.subTest(missing=missing):
                self.setUp()
                self.start()
                plan = self.request(30, "off", {})
                if missing == "missing":
                    plan.rooms.pop("guest")
                else:
                    stamp = NIGHT + timedelta(seconds=30)
                    states = self.ha.states()
                    for probe in self.cfg.zones["guest"].probes:
                        states[probe.entity_id]["last_reported"] = (stamp - timedelta(hours=1)).isoformat()
                        states[probe.entity_id]["last_updated"] = states[probe.entity_id]["last_reported"]
                    plan = Policy(self.cfg).decide(states, stamp, Settings.read(states, self.cfg))
                    plan.mode, plan.head_targets_f = "off", {}
                    plan.heater_on = plan.auxiliary_fan_only = False
                self.executor.step(plan, self.ha.states(), 30)
                self.assertEqual(self.ha.data[self.guest]["state"], "off")

    def test_inhibited_head_is_never_retained(self):
        self.start()
        plan = self.request(30, "off", {})
        plan.inhibited_heads = {"guest": "Temperature safety check"}
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertNotIn(self.guest, self.executor.cycle_holds)

    def test_pending_activation_and_unavailable_state_are_never_retained(self):
        self.step(0)
        self.assertIn(self.guest, self.executor.pending)
        self.step(15, "off", {})
        self.assertNotIn(self.guest, self.executor.cycle_holds)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.setUp()
        self.start()
        self.ha.ignore.add(self.guest)
        self.ha.data[self.guest]["state"] = "unavailable"
        self.step(30, "off", {})
        self.assertNotIn(self.guest, self.executor.cycle_holds)
        self.assertIsNone(self.executor.head_cycles[self.guest].run_since)

    def test_shared_head_can_finish_run_with_a_limits_only_adjacent_room(self):
        self.start(heads={"bonus": 72})
        plan = self.step(30, "off", {})
        self.assertFalse(plan.room_targets["entertainment"]["comfort_enabled"])
        self.assertTrue(plan.room_targets["office"]["comfort_enabled"])
        self.assertEqual(plan.head_targets_f, {"bonus": 72})
        self.assertEqual(self.executor.cycle_holds[self.bonus]["kind"], "minimum_run")
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")

    def test_explicit_comfort_disable_or_target_change_bypasses_run_hold(self):
        for change in ("disable", "target"):
            with self.subTest(change=change):
                self.setUp()
                self.start()
                plan = self.request(30, "off", {})
                if change == "disable":
                    plan.room_targets["guest"]["comfort_enabled"] = False
                else:
                    plan.room_targets["guest"].update(low_f=73, high_f=75, target_f=74)
                self.executor.step(plan, self.ha.states(), 30)
                self.assertEqual(self.ha.data[self.guest]["state"], "off")

    def test_opposite_shared_direction_and_shutdown_bypass_run_hold(self):
        self.start()
        self.step(30, "heat", {"primary": 68})
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertNotIn(self.guest, self.executor.cycle_holds)
        self.setUp()
        self.start()
        self.assertEqual(self.executor.shutdown(), [])
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertEqual(self.executor.cycle_holds, {})
        self.assertIsNone(self.executor.head_cycles[self.guest].run_since)

    def test_setpoint_adjustment_does_not_restart_run_clock(self):
        self.start()
        self.step(30, heads={"guest": 73})
        self.step(45, heads={"guest": 73})
        self.assertEqual(self.executor.head_cycles[self.guest].run_since, 15)
        plan = self.step(90, "off", {})
        self.assertEqual(plan.head_targets_f["guest"], 73)
        self.assertEqual(self.executor.cycle_holds[self.guest]["remaining_seconds"], 225)

    def start_two_with_off_guard(self):
        self.cfg = replace(self.cfg, head_minimum_run_seconds=0, head_minimum_off_seconds=180)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"
        both = {"guest": 72, "bonus": 72}
        self.step(0, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(180, heads=both)
        self.step(195, heads=both)
        self.step(210, heads={"bonus": 72})
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertEqual(self.executor.head_cycles[self.guest].off_since, 210)
        self.ha.calls.clear()
        return both

    def test_individual_off_timer_keeps_other_heads_running_and_same_direction(self):
        both = self.start_two_with_off_guard()
        self.step(225, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.assertEqual(self.executor.phase, "running")
        hold = self.executor.cycle_holds[self.guest]
        self.assertEqual((hold["kind"], hold["remaining_seconds"]), ("minimum_off", 165))
        self.assertFalse(any(data["entity_id"] == self.bonus and data.get("hvac_mode") == "off"
                             for _, _, data in self.ha.calls))
        self.step(389, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(390, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.executor.phase, "running")

    def test_disconnected_time_never_counts_as_confirmed_off_time(self):
        both = self.start_two_with_off_guard()
        self.ha.ignore.add(self.guest)
        self.ha.data[self.guest]["state"] = "unavailable"
        self.step(225, heads=both)
        self.step(600, heads=both)
        self.assertIsNone(self.executor.head_cycles[self.guest].off_since)
        self.ha.ignore.remove(self.guest)
        self.ha.data[self.guest].update(state="off", last_reported="reconnected")
        self.step(601, heads=both)
        self.assertEqual(self.executor.head_cycles[self.guest].off_since, 601)
        self.step(780, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(781, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")

    def test_unacknowledged_off_never_starts_idle_timer_or_allows_reactivation(self):
        both = self.start_two_with_off_guard()
        # Re-activate, acknowledge, then simulate a head ignoring off commands.
        self.step(390, heads=both)
        self.step(405, heads=both)
        self.ha.ignore.add(self.guest)
        self.step(420, heads={"bonus": 72})
        self.ha.calls.clear()
        self.step(435, heads=both)
        self.assertIsNone(self.executor.head_cycles[self.guest].off_since)
        self.assertFalse(any(service == "set_temperature" and data["entity_id"] == self.guest
                             for _, service, data in self.ha.calls))

    def test_zero_guards_preserve_explicit_fast_cycle_configuration(self):
        self.cfg = replace(self.cfg, head_minimum_run_seconds=0, head_minimum_off_seconds=0)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"
        both = {"guest": 72, "bonus": 72}
        self.step(0, heads=both)
        self.step(15, heads=both)
        self.step(30, heads={"bonus": 72})
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(45, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")

    def test_global_all_off_settling_barrier_is_unchanged(self):
        self.cfg = replace(self.cfg, head_minimum_run_seconds=0, head_minimum_off_seconds=0)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.step(0)
        self.step(15)
        self.step(194)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(195)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")

    def test_off_mode_with_active_hvac_action_cannot_start_idle_time_or_restart(self):
        both = self.start_two_with_off_guard()
        self.ha.data[self.guest]["attributes"]["hvac_action"] = "cooling"
        self.step(225, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertIsNone(self.executor.head_cycles[self.guest].off_since)
        self.assertEqual(self.executor.cycle_holds[self.guest]["kind"], "minimum_off")
        self.step(600, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.ha.data[self.guest]["attributes"]["hvac_action"] = "idle"
        self.ha.data[self.guest]["last_reported"] = "confirmed_idle"
        self.step(601, heads=both)
        self.assertEqual(self.executor.head_cycles[self.guest].off_since, 601)
        self.step(780, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.step(781, heads=both)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
