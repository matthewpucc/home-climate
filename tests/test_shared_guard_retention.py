"""Shared-room protection keeps useful calls without bypassing real cutoffs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from tests.test_controller import FakeHA, NIGHT


def configuration():
    return Config.from_dict({
        "schema_version": 2, "timezone": "Etc/UTC",
        "heads": {"shared": "climate.shared_test", "healthy": "climate.healthy_test"},
        "rooms": {
            name: {"head": "healthy" if name == "healthy" else "shared",
                   "sensors": [{"entity_id": f"sensor.{name}_{i}"} for i in range(3)],
                   "profile": {"target_f": 72, "half_band_f": 1}}
            for name in ("office", "entertainment", "healthy")},
        "decision_interval_seconds": 90, "forecast_minutes": 0,
        "head_minimum_run_seconds": 300, "head_minimum_off_seconds": 0,
        "head_cool_call_offset_f": 5, "head_heat_call_offset_f": 5,
    })


class SharedGuardRetentionTests(unittest.TestCase):
    def setup_controller(self, *, mode="cool", minimum_off=0):
        self.cfg = replace(configuration(), off_settle_seconds=0,
                           head_minimum_off_seconds=minimum_off)
        self.settings = Settings.read({}, self.cfg)
        self.cadence = DecisionCadence(self.cfg)
        self.values = ({"office": 70.9, "entertainment": 74.8, "healthy": 76}
                       if mode == "cool" else
                       {"office": 73.1, "entertainment": 66.4, "healthy": 65.5})
        data = {}
        for room in self.cfg.zones.values():
            for probe in room.probes:
                data[probe.entity_id] = {"state": "72", "last_reported": NIGHT.isoformat(),
                                         "attributes": {"unit_of_measurement": "°F"}}
        for entity in self.cfg.heads.values():
            data[entity] = {"state": "off", "attributes": {
                "hvac_modes": ["off", "heat", "cool"], "temperature": 72,
                "current_temperature": 72, "min_temp": 65, "max_temp": 90,
                "target_temp_step": 1}}
        self.ha = FakeHA(data)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", mode
        self.shared = self.cfg.heads["shared"]
        self.healthy = self.cfg.heads["healthy"]

    def plan(self, elapsed, **temperatures):
        self.values.update(temperatures)
        stamp = NIGHT + timedelta(seconds=elapsed)
        for name, room in self.cfg.zones.items():
            for probe in room.probes:
                self.ha.data[probe.entity_id].update(
                    state=str(self.values[name]), last_reported=stamp.isoformat())
        return self.cadence.decide(self.ha.states(), stamp, self.settings,
                                   monotonic_now=elapsed)

    def tick(self, elapsed, **temperatures):
        plan = self.plan(elapsed, **temperatures)
        self.executor.step(plan, self.ha.states(), elapsed)
        return plan

    def acknowledge_start(self):
        self.tick(0)
        self.tick(15)
        self.assertEqual(self.executor.head_cycles[self.shared].run_since, 15)
        self.ha.calls.clear()

    def test_hot_shared_area_keeps_cooling_beyond_other_areas_soft_floor(self):
        self.setup_controller()
        self.acknowledge_start()
        for elapsed, warm in ((30, 73.6), (90, 73.2), (120, 72.8)):
            plan = self.tick(elapsed, office=70.7, entertainment=warm)
            self.assertEqual(self.ha.data[self.shared]["state"], "cool")
            self.assertEqual(plan.head_protections["shared"]["mode"], "cool")
            self.assertEqual(self.executor.head_cycles[self.shared].run_since, 15)
        self.assertFalse(any(data["entity_id"] == self.shared and data.get("hvac_mode") == "off"
                             for _, _, data in self.ha.calls))

    def test_house_cutoff_still_stops_only_the_affected_head(self):
        self.setup_controller()
        self.acknowledge_start()
        plan = self.tick(30, office=66.9, entertainment=75.2)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertEqual(self.ha.data[self.healthy]["state"], "cool")
        self.assertNotIn(self.shared, self.executor.cycle_holds)
        self.assertNotIn("shared", plan.head_targets_f)

    def test_partial_probe_loss_runs_and_total_attached_room_loss_stops_only_shared(self):
        self.setup_controller()
        self.acknowledge_start()
        # Fresh healthy probes must not mask a completely unmeasured attached
        # area, even while the other area still requires house protection.
        self.plan(30)
        probes = self.cfg.zones["office"].probes
        self.ha.data[probes[0].entity_id]["state"] = "unavailable"
        now = NIGHT + timedelta(seconds=30)
        partial = self.cadence.decide(self.ha.states(), now, self.settings, monotonic_now=30)
        self.executor.step(partial, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.shared]["state"], "cool")
        for probe in probes:
            self.ha.data[probe.entity_id]["state"] = "unavailable"
        missing = self.cadence.decide(self.ha.states(), now + timedelta(seconds=15),
                                      self.settings, monotonic_now=45)
        self.executor.step(missing, self.ha.states(), 45)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertEqual(self.ha.data[self.healthy]["state"], "cool")
        self.assertNotIn(self.shared, self.executor.cycle_holds)

    def test_symmetric_cold_area_protection_yields_to_actual_hot_house_guard(self):
        self.setup_controller(mode="heat")
        self.acknowledge_start()
        retained = self.tick(30, office=73.2, entertainment=68.1)
        self.assertEqual(self.ha.data[self.shared]["state"], "heat")
        self.assertEqual(retained.head_protections["shared"]["mode"], "heat")
        self.tick(45, office=74.1, entertainment=66.5)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertEqual(self.ha.data[self.healthy]["state"], "heat")
        self.assertNotIn(self.shared, self.executor.cycle_holds)

    def test_minimum_run_uses_valid_house_protection_instead_of_soft_floor_veto(self):
        self.setup_controller()
        self.acknowledge_start()
        plan = self.plan(30, office=70.7, entertainment=73.4)
        self.assertIn("shared", plan.head_protections)
        self.assertIn("heat", plan.head_constraints["shared"])
        # Exercise retention rather than ordinary selected-head dispatch: an
        # otherwise safe call cannot be cut by its neighbour's soft preference.
        plan.mode, plan.head_targets_f, plan.head_call_targets_f = "off", {}, {}
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.shared]["state"], "cool")
        self.assertEqual(self.executor.cycle_holds[self.shared]["kind"], "minimum_run")
        self.assertEqual(self.executor.cycle_holds[self.shared]["remaining_seconds"], 285)

    def test_pure_policy_rearm_constraint_cannot_be_undone_by_run_retention(self):
        self.setup_controller()
        self.values.update(office=71.8, entertainment=73.4, healthy=72)
        self.acknowledge_start()
        # Observe a comfort cutoff without dispatching its Off yet. The head's
        # last confirmed Cool report and minimum-run clock remain intact.
        self.plan(30, office=70.88, entertainment=73.42)
        self.plan(90, office=71.42)
        stamp = NIGHT + timedelta(seconds=90)
        pure = self.cadence.policy.decide(self.ha.states(), stamp, self.settings)
        self.assertEqual(pure.mode, "off")
        self.assertEqual(pure.inhibited_heads, {})
        self.assertEqual(pure.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")
        self.assertEqual(self.ha.data[self.shared]["state"], "cool")
        self.executor.step(pure, self.ha.states(), 90)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertNotIn("shared", pure.head_targets_f)
        self.assertNotIn(self.shared, self.executor.cycle_holds)
        stop = self.executor.head_stop_reasons[self.shared]
        self.assertEqual(stop["kind"], "comfort_rearm")
        self.assertIn("office", stop["reason"])
        self.assertIn("71.5", stop["reason"])

    def test_expired_protection_cannot_retain_a_call_past_soft_floor(self):
        self.setup_controller()
        self.acknowledge_start()
        old = deepcopy(self.cadence.cached_plan.head_protections["shared"])
        plan = self.plan(30, office=70.7, entertainment=72)
        plan.mode = "cool"
        plan.head_targets_f.pop("shared", None)
        plan.head_call_targets_f.pop("shared", None)
        plan.inhibited_heads.pop("shared", None)
        # An earlier protection annotation is not authority to keep cooling
        # once the originating area's fresh reading has reached its target.
        plan.head_protections["shared"] = old
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertNotIn(self.shared, self.executor.cycle_holds)

    def test_house_protection_never_bypasses_acknowledged_minimum_off_time(self):
        self.setup_controller(minimum_off=180)
        self.tick(0)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.tick(180)
        self.tick(195)
        self.assertEqual(self.ha.data[self.shared]["state"], "cool")
        self.tick(210, office=66.9, entertainment=75.2)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.tick(225, office=70.9, entertainment=75.2)
        since = self.executor.head_cycles[self.shared].off_since
        self.assertIsNotNone(since)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.assertEqual(self.ha.data[self.healthy]["state"], "cool")
        # The maintenance hard-stop inhibition first survives to the next
        # normal decision. A renewed eligible request still cannot skip Off time.
        self.tick(300)
        self.assertEqual(self.executor.cycle_holds[self.shared]["kind"], "minimum_off")
        self.tick(since + 179)
        self.assertEqual(self.ha.data[self.shared]["state"], "off")
        self.tick(since + 180)
        self.assertEqual(self.ha.data[self.shared]["state"], "cool")


if __name__ == "__main__":
    unittest.main()
