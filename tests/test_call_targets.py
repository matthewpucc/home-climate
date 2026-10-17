"""Room comfort and heat-pump call setpoints have different responsibilities."""
import unittest
from copy import deepcopy
from dataclasses import replace

from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.policy import Policy, call_target_f
from tests.test_controller import FakeHA, NIGHT, config, fixture


class HeatPumpCallTargetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(config(), head_heat_call_offset_f=5, head_cool_call_offset_f=5,
                           head_minimum_run_seconds=0, head_minimum_off_seconds=0)
        self.guest = self.cfg.heads["guest"]
        self.primary = self.cfg.heads["primary"]

    def decide(self, states, unit="°F", cfg=None):
        cfg = cfg or self.cfg
        return Policy(cfg).decide(states, NIGHT, Settings.read(states, cfg), unit)

    def executor(self, states, mode="cool", unit="°F", cfg=None):
        ha = FakeHA(states)
        executor = Executor(cfg or self.cfg, ha, unit)
        executor.phase, executor.direction = "running", mode
        return ha, executor

    def test_cooling_call_is_below_onboard_sensor_while_comfort_target_stays_72(self):
        states = fixture(self.cfg, guest=74)
        states[self.guest]["attributes"]["current_temperature"] = 70
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.head_targets_f["guest"], 72)
        self.assertEqual(plan.head_call_targets_f["guest"], 65)
        self.assertEqual(plan.rooms["guest"]["temperature"], 74)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 65)
        self.assertEqual(plan.head_targets_f["guest"], 72)

    def test_heating_call_exceeds_hot_onboard_sensor_while_room_target_stays_68(self):
        states = fixture(self.cfg, primary=66.9)
        states[self.primary]["attributes"]["current_temperature"] = 74
        plan = self.decide(states)
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(plan.head_targets_f["primary"], 68)
        self.assertEqual(plan.head_call_targets_f["primary"], 79)
        ha, executor = self.executor(states, "heat")
        executor.step(plan, ha.states(), 0)
        self.assertEqual(ha.data[self.primary]["attributes"]["temperature"], 79)

    def test_sensor_on_opposite_side_never_weakens_the_requested_call(self):
        for mode, comfort, board, expected in (("cool", 72, 80, 67), ("heat", 68, 60, 73)):
            with self.subTest(mode=mode):
                states = fixture(self.cfg)
                states[self.guest]["attributes"]["current_temperature"] = board
                self.assertEqual(call_target_f(self.cfg, self.guest, mode, comfort, states, "°F"), expected)

    def test_invalid_missing_or_implausible_onboard_readings_fall_back_to_room_offset(self):
        for board in (None, True, False, float("nan"), float("inf"), -100, 1000, "bad"):
            with self.subTest(board=board):
                states = fixture(self.cfg, guest=74)
                states[self.guest]["attributes"]["current_temperature"] = board
                plan = self.decide(states)
                self.assertEqual(plan.head_targets_f["guest"], 72)
                self.assertEqual(plan.head_call_targets_f["guest"], 67)
        states = fixture(self.cfg, guest=74)
        self.assertNotIn("current_temperature", states[self.guest]["attributes"])
        self.assertEqual(self.decide(states).head_call_targets_f["guest"], 67)

    def test_onboard_temperature_alone_cannot_create_a_room_demand(self):
        states = fixture(self.cfg)
        states[self.guest]["attributes"]["current_temperature"] = 100
        plan = self.decide(states)
        self.assertNotIn("guest", plan.head_targets_f)
        self.assertNotIn("guest", plan.head_call_targets_f)
        self.assertEqual(plan.rooms["guest"]["temperature"], 72)

    def test_celsius_onboard_input_and_equipment_quantization_are_converted_once(self):
        states = fixture(self.cfg, guest=74)
        for entity in self.cfg.climate_entities:
            states[entity]["attributes"].update(min_temp=16, max_temp=32, target_temp_step=.5, temperature=20)
        states[self.guest]["attributes"]["current_temperature"] = 20  # 68°F.
        plan = self.decide(states, "°C")
        self.assertEqual(plan.head_targets_f["guest"], 72)
        self.assertAlmostEqual(plan.head_call_targets_f["guest"], 62.6)  # Quantized 17°C.
        ha, executor = self.executor(states, unit="°C")
        executor.step(plan, ha.states(), 0)
        self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 17)
        self.assertAlmostEqual(plan.head_call_targets_f["guest"], 62.6)

    def test_planned_calls_match_native_limits_and_step(self):
        for mode, comfort, board, expected in (("cool", 72, 60, 64), ("heat", 68, 86, 80)):
            with self.subTest(mode=mode):
                states = fixture(self.cfg)
                states[self.guest]["attributes"].update(
                    min_temp=64, max_temp=80, target_temp_step=2, current_temperature=board)
                self.assertEqual(call_target_f(self.cfg, self.guest, mode, comfort, states, "°F"), expected)

    def test_zero_offsets_ignore_onboard_sensor_and_preserve_previous_commands(self):
        cfg = replace(self.cfg, head_heat_call_offset_f=0, head_cool_call_offset_f=0)
        for mode, comfort, board in (("cool", 72, 50), ("heat", 68, 90)):
            with self.subTest(mode=mode):
                states = fixture(cfg)
                states[self.guest]["attributes"]["current_temperature"] = board
                self.assertEqual(call_target_f(cfg, self.guest, mode, comfort, states, "°F"), comfort)

    def test_each_direction_offset_can_be_disabled_independently(self):
        cfg = replace(self.cfg, head_heat_call_offset_f=0)
        states = fixture(cfg)
        states[self.guest]["attributes"]["current_temperature"] = 70
        self.assertEqual(call_target_f(cfg, self.guest, "heat", 68, states, "°F"), 68)
        self.assertEqual(call_target_f(cfg, self.guest, "cool", 72, states, "°F"), 65)

    def test_capability_change_rebounds_cached_call_without_recalculating_sensor_bias(self):
        states = fixture(self.cfg, guest=74)
        states[self.guest]["attributes"]["current_temperature"] = 70
        plan = self.decide(states)
        self.assertEqual(plan.head_call_targets_f["guest"], 65)
        states[self.guest]["attributes"].update(min_temp=68, current_temperature=50)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(plan.head_targets_f["guest"], 72)
        self.assertEqual(plan.head_call_targets_f["guest"], 68)
        self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 68)

    def test_minimum_run_retains_comfort_and_acknowledged_call_separately(self):
        cfg = replace(self.cfg, head_minimum_run_seconds=300)
        states = fixture(cfg, guest=74)
        states[self.guest]["attributes"]["current_temperature"] = 70
        plan = self.decide(states, cfg=cfg)
        ha, executor = self.executor(states, cfg=cfg)
        executor.step(deepcopy(plan), ha.states(), 0)
        ha.data[self.guest]["attributes"]["current_temperature"] = 50
        executor.step(deepcopy(plan), ha.states(), 15)
        cycle = executor.head_cycles[self.guest]
        self.assertEqual((cycle.target_f, cycle.call_target_f, cycle.target_native), (72, 65, 65))
        ha.calls.clear()
        for now, board in ((30, 50), (45, 40)):
            for probe in cfg.zones["guest"].probes:
                ha.data[probe.entity_id]["state"] = "72"
            ha.data[self.guest]["attributes"]["current_temperature"] = board
            off_plan = self.decide(ha.states(), cfg=cfg)
            self.assertNotIn("guest", off_plan.head_targets_f)
            executor.step(off_plan, ha.states(), now)
            self.assertEqual(off_plan.head_targets_f["guest"], 72)
            self.assertEqual(off_plan.head_call_targets_f["guest"], 65)
            self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 65)
        self.assertFalse(any(service == "set_temperature" for _, service, _ in ha.calls))
        for probe in cfg.zones["guest"].probes:
            ha.data[probe.entity_id]["state"] = "71.5"
        off_plan = self.decide(ha.states(), cfg=cfg)
        executor.step(off_plan, ha.states(), 60)
        self.assertEqual(ha.data[self.guest]["state"], "off")
        self.assertNotIn("guest", off_plan.head_call_targets_f)

    def test_biased_call_still_obeys_per_head_minimum_off_time(self):
        cfg = replace(self.cfg, head_minimum_off_seconds=180)
        states = fixture(cfg, guest=74)
        states[self.guest]["attributes"]["current_temperature"] = 70
        plan = self.decide(states, cfg=cfg)
        ha, executor = self.executor(states, cfg=cfg)
        for now in (0, 179):
            executor.step(deepcopy(plan), ha.states(), now)
            self.assertEqual(ha.data[self.guest]["state"], "off")
        executor.step(deepcopy(plan), ha.states(), 180)
        self.assertEqual(ha.data[self.guest]["state"], "cool")
        self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 65)

    def test_auxiliary_heater_keeps_its_existing_exaggerated_setpoint(self):
        states = fixture(self.cfg, guest=74, living=67)
        states[self.guest]["attributes"]["current_temperature"] = 70
        states[self.cfg.heater_entity]["attributes"]["current_temperature"] = 99
        plan = self.decide(states)
        self.assertTrue(plan.heater_on)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(ha.data[self.cfg.heater_entity]["attributes"]["temperature"], 86)
        self.assertEqual(ha.data[self.guest]["attributes"]["temperature"], 65)
        self.assertNotIn(self.cfg.heater_entity, plan.head_call_targets_f)
