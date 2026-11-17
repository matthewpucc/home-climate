"""Protection Only is room intent, never a shared equipment Off command."""
from dataclasses import replace
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Settings
from house_climate.policy import Policy
from house_climate.recovery_inputs import signature
from tests.test_protection_arbitration import Clock, configuration, states


class ProtectionOnlyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.config = configuration(shared=True)
        self.settings = Settings.read({}, self.config)
        self.settings.protection_only_rooms = frozenset({"north"})
        self.policy = Policy(self.config, clock=self.clock)

    def decide(self, north, south=72):
        return self.policy.decide(states(self.config, self.clock, {"north": north, "south": south}),
                                  self.clock.now, self.settings)

    def test_no_ordinary_demand_or_old_target_hysteresis(self):
        for temperature in (67, 70, 74, 77):
            with self.subTest(temperature=temperature):
                self.policy.calls["north", "heat"] = True
                self.policy.calls["north", "cool"] = True
                self.policy.forecast_pending.update({("north", "heat"), ("north", "cool")})
                plan = self.decide(temperature)
                self.assertFalse(plan.room_targets["north"]["heat_requested"])
                self.assertFalse(plan.room_targets["north"]["cool_requested"])
                self.assertEqual(plan.head_targets_f, {})
                self.assertNotIn(("north", "heat"), self.policy.forecast_pending)
                self.assertNotIn(("north", "cool"), self.policy.forecast_pending)
                self.assertTrue(plan.room_targets["north"]["protection_only"])
                self.assertFalse(plan.room_targets["north"]["comfort_enabled"])

    def test_native_floor_and_ceiling_protection_remain(self):
        for temperature, mode in ((59, "heat"), (81, "cool")):
            with self.subTest(mode=mode):
                self.policy = Policy(self.config, clock=self.clock)
                plan = self.decide(temperature)
                self.assertTrue(plan.room_targets["north"][mode + "_requested"])
                self.assertEqual(plan.mode, mode)
                self.assertIn("north_head", plan.head_targets_f)
                self.assertIn("north", plan.head_protections["north_head"]["rooms"])

    def test_sibling_can_call_without_old_soft_veto_but_hard_guard_remains(self):
        plan = self.decide(74, 70)
        self.assertEqual(plan.mode, "heat")
        self.assertFalse(plan.room_targets["north"]["heat_requested"])
        self.assertTrue(plan.room_targets["south"]["heat_requested"])
        plan = self.decide(81, 70)
        self.assertNotEqual(plan.mode, "heat")
        self.assertEqual(plan.head_constraints["north_head"]["heat"]["kind"], "house_limit")

    def test_missing_protected_room_still_blocks_shared_head(self):
        snapshot = states(self.config, self.clock, {"north": 72, "south": 70})
        for probe in self.config.zones["north"].probes:
            snapshot.pop(probe.entity_id)
        plan = self.policy.decide(snapshot, self.clock.now, self.settings)
        self.assertNotIn("north_head", plan.head_targets_f)

    def test_intent_invalidates_cadence_and_recovery_freshness(self):
        normal = replace(self.settings, protection_only_rooms=frozenset())
        snapshot = states(self.config, self.clock, {"north": 70, "south": 72})
        cadence = DecisionCadence(self.config, clock=self.clock)
        first = cadence.decide(snapshot, self.clock.now, normal)
        self.assertTrue(first.room_targets["north"]["heat_requested"])
        before = signature(self.config, snapshot, self.clock.now, normal)
        second = cadence.decide(snapshot, self.clock.now, self.settings)
        self.assertFalse(second.room_targets["north"]["heat_requested"])
        self.assertNotEqual(before, signature(self.config, snapshot, self.clock.now, self.settings))
        resumed = cadence.decide(snapshot, self.clock.now, normal)
        self.assertTrue(resumed.room_targets["north"]["heat_requested"])

    def test_auxiliary_ordinary_latch_is_removed_but_protection_still_calls(self):
        from house_climate.auxiliary_dispatch import AuxiliaryDispatch
        from tests.test_auxiliary_dispatch_policy import reading
        from tests.test_controller import config
        dispatch = AuxiliaryDispatch(config())
        dispatch.active = True
        ordinary = dispatch.decide(reading(67, self.clock.now), self.clock.now, (69, 73, 71),
                                   protection_only=True)
        self.assertFalse(ordinary.requested)
        protective = dispatch.decide(reading(59, self.clock.now), self.clock.now, (60, 80, 70),
                                     protection_only=True, comfort_enabled=False, emergency=True)
        self.assertTrue(protective.requested)

    def test_ambient_and_head_assist_transfer_leave_purifier_floor(self):
        from house_climate.config import Config
        from tests.test_rooms import configuration as rooms_config, states as room_states, NOW
        raw = rooms_config()
        raw["rooms"]["alpha"]["fans"][0]["minimum_percentage"] = 20
        cfg = Config.from_dict(raw)
        settings = Settings.read({}, cfg)
        settings.protection_only_rooms = frozenset({"alpha"})
        # Alpha would ordinarily benefit from warm beta; beta keeps the head
        # active in heat. Neither ambient nor selected-head transfer may boost alpha.
        snapshot = room_states(cfg, {"alpha": 68, "beta": 69, "gamma": 68})
        plan = Policy(cfg).decide(snapshot, NOW, settings)
        self.assertEqual(plan.fans["fan.test_airflow"], 20)
        self.assertNotIn("fan.test_airflow", plan.fan_head_assists)
        settings.room_fan_overrides = {"alpha": 40}
        plan = Policy(cfg).decide(snapshot, NOW, settings)
        self.assertEqual(plan.fans["fan.test_airflow"], 40)

    def test_directed_airflow_does_not_aim_or_boost_for_off_room(self):
        from house_climate.config import Config
        from tests.test_directed_airflow import fixed_config, FAN
        from tests.test_rooms import states as room_states, NOW
        cfg = Config.from_dict(fixed_config())
        settings = Settings.read({}, cfg)
        settings.protection_only_rooms = frozenset({"alpha"})
        plan = Policy(cfg).decide(room_states(cfg, {"alpha": 73, "beta": 72, "gamma": 68}),
                                  NOW, settings)
        self.assertEqual(plan.fans[FAN], 20)
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])

    def test_executor_profile_signature_invalidates_existing_minimum_run(self):
        from house_climate.executor import Executor
        from tests.test_controller import FakeHA
        normal = replace(self.settings, protection_only_rooms=frozenset())
        snapshot = states(self.config, self.clock, {"north": 70, "south": 72})
        policy = Policy(self.config, clock=self.clock)
        before = policy.decide(snapshot, self.clock.now, normal)
        after = policy.decide(snapshot, self.clock.now, self.settings)
        executor = Executor(self.config, FakeHA(snapshot), "°F")
        self.assertNotEqual(executor._head_profile_signature("north_head", before),
                            executor._head_profile_signature("north_head", after))

    def test_executor_does_not_resurrect_old_call_for_minimum_run(self):
        from tests.test_cycle_guards import HeadCycleGuardTests
        scenario = HeadCycleGuardTests()
        scenario.setUp()
        scenario.start()
        plan = scenario.request(30, "off", {})
        plan.room_targets["guest"].update(protection_only=True, comfort_enabled=False,
                                          low_f=66, high_f=75, target_f=70.5)
        scenario.executor.step(plan, scenario.ha.states(), 30)
        self.assertEqual(scenario.ha.data[scenario.guest]["state"], "off")
        self.assertNotIn(scenario.guest, scenario.executor.cycle_holds)
        self.assertNotIn("guest", plan.head_targets_f)
