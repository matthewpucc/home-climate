"""Whole-degree equipment commands for recognized Serin or explicitly configured heads."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from house_climate.bindings import BINDINGS_FILENAME, BindingsError, example_bindings, load_bindings
from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.house_policy import house_configuration
from house_climate.policy import Plan, Policy, head_native_target
from tests.test_controller import FakeHA, NIGHT, config, fixture
from tests.test_delayed_climate_ack import DelayedTargetHA


class WholeFahrenheitHeadTests(unittest.TestCase):
    def setUp(self):
        self.base = replace(config(), head_activation_timeout_seconds=120)
        self.bonus = self.base.heads["bonus"]
        self.guest = self.base.heads["guest"]
        self.cfg = replace(self.base, whole_fahrenheit_heads=[self.bonus],
                           head_cool_min_f={self.bonus: 65})
        self.states = fixture(self.cfg)
        for entity in (self.bonus, self.guest):
            self.states[entity]["attributes"].update(target_temp_step=.5, temperature=71)

    def plan(self, target=67.5):
        return Plan(mode="cool", head_targets_f={"bonus": 71.5, "guest": 71.5},
                    head_call_targets_f={"bonus": target, "guest": target})

    def commands(self, ha, entity, service="set_temperature"):
        return [data for _, action, data in ha.calls if action == service and data["entity_id"] == entity]

    def executor(self, cfg, ha):
        controller = Executor(cfg, ha, "°F")
        controller.phase, controller.direction = "running", "cool"
        return controller

    def test_dispatch_and_feedback_exact_at_six_seconds_no_off_after_original_deadline(self):
        ha = DelayedTargetHA(self.states, self.bonus)
        controller = self.executor(self.cfg, ha)
        plan = self.plan()
        controller.step(plan, ha.states(), 0)
        self.assertEqual(plan.head_targets_f, {"bonus": 71.5, "guest": 71.5})
        self.assertEqual(plan.head_call_targets_f, {"bonus": 68, "guest": 67.5})
        self.assertEqual(self.commands(ha, self.bonus)[0]["temperature"], 68)
        self.assertEqual(self.commands(ha, self.guest)[0]["temperature"], 67.5)
        self.assertIn(self.bonus, controller.pending)
        ha.publish(self.bonus, mode="cool", target=68, at=6)
        for now in (6, 120, 135):
            controller.step(self.plan(), ha.states(), now)
        self.assertEqual(controller.head_cycles[self.bonus].target_f, 71.5)
        self.assertEqual(controller.head_cycles[self.bonus].target_native, 68)
        self.assertNotIn(self.bonus, controller.pending)
        self.assertNotIn(self.bonus, controller.recoveries)
        self.assertEqual(len(self.commands(ha, self.bonus)), 1)
        self.assertEqual(self.commands(ha, self.bonus, "set_hvac_mode"), [])

    def test_fractional_echo_is_not_accepted_as_whole_target(self):
        ha = DelayedTargetHA(self.states, self.bonus)
        controller = self.executor(self.cfg, ha)
        controller.step(self.plan(), ha.states(), 0)
        ha.publish(self.bonus, mode="cool", target=67.5, at=6)
        for now in (6, 119.9):
            controller.step(self.plan(), ha.states(), now)
            self.assertIn(self.bonus, controller.pending)
            self.assertNotIn(self.bonus, controller.recoveries)
        controller.step(self.plan(), ha.states(), 120)
        self.assertIn(self.bonus, controller.recoveries)
        self.assertIn("timed out", controller.device_issues[self.bonus])
        self.assertEqual(len(self.commands(ha, self.bonus)), 1)

    def test_profile_preserves_fractional_comfort_and_respects_range_and_floor(self):
        attrs = {"min_temp": 61.5, "max_temp": 68.5, "target_temp_step": .5}
        scoped = replace(self.cfg, head_cool_min_f={self.bonus: 65.5})
        for mode, requested, expected in (
            ("cool", 64.5, 66), ("cool", 70, 68), ("heat", 61.5, 62), ("heat", 67.5, 68),
        ):
            with self.subTest(mode=mode, request=requested):
                actual = head_native_target(scoped, self.bonus, mode, requested, attrs, "°F")
                self.assertEqual(actual, expected)
                self.assertEqual(head_native_target(scoped, self.bonus, mode, actual, attrs, "°F"), actual)
        self.assertEqual(head_native_target(scoped, self.guest, "cool", 67.5, attrs, "°F"), 67.5)
        self.assertEqual(head_native_target(scoped, self.bonus, "heat", 67.1,
                                           {"min_temp": 16, "max_temp": 29, "target_temp_step": .5},
                                           "°C"), 19.5)

    def test_policy_advertises_the_same_quantized_call_as_executor(self):
        cfg = replace(self.cfg, head_cool_call_offset_f=4.5)
        states = fixture(cfg, office=74, entertainment=73, guest=74)
        for entity in (self.bonus, self.guest):
            states[entity]["attributes"].update(target_temp_step=.5, current_temperature=73)
        plan = Policy(cfg).decide(states, NIGHT, Settings.read(states, cfg), "°F")
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.head_targets_f["bonus"], 72)
        self.assertEqual(plan.head_call_targets_f["bonus"], 68)
        self.assertEqual(plan.head_call_targets_f["guest"], 67.5)
        ha = FakeHA(states)
        self.executor(cfg, ha).step(deepcopy(plan), ha.states(), 0)
        self.assertEqual(self.commands(ha, self.bonus)[0]["temperature"], 68)

    def test_impossible_integer_range_blocks_only_the_affected_head(self):
        self.states[self.bonus]["attributes"].update(min_temp=67.2, max_temp=67.8)
        ha = FakeHA(self.states)
        controller = self.executor(self.cfg, ha)
        controller.step(self.plan(), ha.states(), 0)
        self.assertEqual(self.commands(ha, self.bonus), [])
        self.assertEqual(self.commands(ha, self.guest)[0]["temperature"], 67.5)
        self.assertEqual(controller.recovery_status(0)["devices"][self.bonus]["kind"], "configuration")

    def test_private_wiring_validates_explicit_distinct_bound_head_ids(self):
        raw = example_bindings().to_dict()
        entity = raw["heads"]["bonus"]
        raw["whole_fahrenheit_heads"] = [entity]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / BINDINGS_FILENAME
            path.write_text(json.dumps(raw))
            loaded = load_bindings(directory)
            self.assertEqual(loaded.to_dict()["whole_fahrenheit_heads"], [entity])
            self.assertEqual(house_configuration(bindings=loaded).whole_fahrenheit_heads, [entity])
            for invalid in ([raw["heater"]], [entity, entity], [None], entity, ["bonus"], None):
                with self.subTest(invalid=invalid), self.assertRaises(BindingsError):
                    path.write_text(json.dumps(dict(raw, whole_fahrenheit_heads=invalid)))
                    load_bindings(directory)
            replacement = deepcopy(raw)
            replacement["heads"]["bonus"] = "climate.replacement"
            path.write_text(json.dumps(replacement))
            with self.assertRaises(BindingsError):
                load_bindings(directory)

    def test_private_bindings_default_applies_to_all_four_serin_heads(self):
        raw = example_bindings().to_dict()
        raw["heads"] = {
            "bonus": "climate.media_room_serin_bonus_heat_pump",
            "guest": "climate.guest_room_serin_guest_heat_pump",
            "primary": "climate.serin_suite_heat_pump",
            "living": "climate.common_area_serin_living_heat_pump",
        }
        attrs = {"min_temp": 60, "max_temp": 85, "target_temp_step": .5}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / BINDINGS_FILENAME
            path.write_text(json.dumps(raw))
            loaded = load_bindings(directory)
            self.assertNotIn("whole_fahrenheit_heads", loaded.to_dict())
            cfg = house_configuration(bindings=loaded)
            self.assertCountEqual(cfg.whole_fahrenheit_heads, raw["heads"].values())
            for entity in raw["heads"].values():
                for mode in ("heat", "cool"):
                    with self.subTest(entity=entity, mode=mode):
                        self.assertEqual(head_native_target(cfg, entity, mode, 67.5, attrs, "°F"), 68)
                self.assertEqual(head_native_target(cfg, entity, "heat", 67.1,
                                                  {"min_temp": 16, "max_temp": 29, "target_temp_step": .5},
                                                  "°C"), 19.5)

    def test_mixed_bindings_and_explicit_overrides_round_trip(self):
        raw = example_bindings().to_dict()
        raw["heads"].update(bonus="climate.serin_bonus", primary="climate.notserin_primary",
                            guest="climate.guest_serin", living="climate.kumo_living")
        for override, expected in (
            ({}, ["climate.serin_bonus", "climate.guest_serin"]),
            ({"whole_fahrenheit_heads": []}, []),
            ({"whole_fahrenheit_heads": ["climate.kumo_living"]}, ["climate.kumo_living"]),
        ):
            with self.subTest(override=override):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / BINDINGS_FILENAME
                    path.write_text(json.dumps(dict(raw, **override)))
                    loaded = load_bindings(directory)
                    cfg = house_configuration(bindings=loaded)
                    self.assertCountEqual(cfg.whole_fahrenheit_heads, expected)
                    attrs = {"min_temp": 60, "max_temp": 85, "target_temp_step": .5}
                    for entity in raw["heads"].values():
                        target = head_native_target(cfg, entity, "cool", 67.5, attrs, "°F")
                        self.assertEqual(target, 68 if entity in expected else 67.5)
                    emitted = loaded.to_dict()
                    self.assertEqual("whole_fahrenheit_heads" in emitted, bool(override))
                    path.write_text(json.dumps(emitted))
                    self.assertEqual(load_bindings(directory), loaded)
