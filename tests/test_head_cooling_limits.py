"""Per-head cooling floors describe equipment limits, not room comfort targets."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tomllib
import unittest

from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Plan, Policy
from tests.test_controller import FakeHA, NIGHT, config, fixture


class HeadCoolingLimitTests(unittest.TestCase):
    def setUp(self):
        self.base = replace(
            config(), head_cool_call_offset_f=5, head_heat_call_offset_f=5,
            head_minimum_run_seconds=0, head_minimum_off_seconds=0,
        )
        self.bonus = self.base.heads["bonus"]
        self.guest = self.base.heads["guest"]
        self.cfg = replace(self.base, head_cool_min_f={self.bonus: 65})

    def states(self, cfg=None):
        cfg = cfg or self.cfg
        states = fixture(cfg, office=74, entertainment=73, guest=74)
        states[self.bonus]["attributes"]["current_temperature"] = 69
        states[self.guest]["attributes"]["current_temperature"] = 69
        return states

    def decide(self, states, cfg=None, unit="°F"):
        cfg = cfg or self.cfg
        return Policy(cfg).decide(states, NIGHT, Settings.read(states, cfg), unit)

    def executor(self, states, cfg=None, unit="°F", mode="cool"):
        ha = FakeHA(states)
        executor = Executor(cfg or self.cfg, ha, unit)
        executor.phase, executor.direction = "running", mode
        return ha, executor

    def activation(self, ha, entity):
        return [data for _, service, data in ha.calls
                if service == "set_temperature" and data["entity_id"] == entity]

    def test_missing_override_keeps_previous_biased_cooling_behavior(self):
        raw = self.base.to_dict()
        raw.pop("head_cool_min_f", None)
        cfg = Config.from_dict(raw)
        self.assertEqual(cfg.head_cool_min_f, {})
        states = self.states(cfg)
        plan = self.decide(states, cfg)
        self.assertEqual(plan.head_targets_f["bonus"], 72)
        self.assertEqual(plan.head_call_targets_f["bonus"], 64)
        ha, executor = self.executor(states, cfg)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 64)

    def test_shared_head_floor_clamps_biased_call_without_changing_room_comfort(self):
        states = self.states()
        plan = self.decide(states)
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.head_targets_f["bonus"], 72)
        self.assertEqual(plan.head_call_targets_f["bonus"], 65)
        self.assertEqual(plan.room_targets["office"]["target_f"], 72)
        self.assertEqual(plan.rooms["office"]["temperature"], 74)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 65)

    def test_each_head_keeps_its_own_override_or_advertised_limit(self):
        for overrides, guest_target in (
            ({self.bonus: 65}, 64),
            ({self.bonus: 65, self.guest: 67}, 67),
        ):
            with self.subTest(overrides=overrides):
                cfg = replace(self.base, head_cool_min_f=overrides)
                states = self.states(cfg)
                plan = self.decide(states, cfg)
                self.assertEqual(plan.head_call_targets_f["bonus"], 65)
                self.assertEqual(plan.head_call_targets_f["guest"], guest_target)
                ha, executor = self.executor(states, cfg)
                executor.step(plan, ha.states(), 0)
                self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 65)
                self.assertEqual(self.activation(ha, self.guest)[0]["temperature"], guest_target)

    def test_celsius_floor_rounds_up_on_original_equipment_step_grid(self):
        for floor, minimum, expected in ((65, 16, 18.5), (65, 16.25, 18.75), (64.4, 16, 18)):
            with self.subTest(floor_f=floor, advertised_minimum=minimum):
                cfg = replace(self.cfg, head_cool_min_f={self.bonus: floor})
                states = self.states(cfg)
                for entity in cfg.climate_entities:
                    states[entity]["attributes"].update(
                        min_temp=minimum, max_temp=32, target_temp_step=.5, temperature=22,
                    )
                # 65°F is between supported steps, but 64.4°F is exactly 18°C.
                # Respect the original grid and do not jump an extra rung due
                # to floating-point conversion at an exact boundary.
                states[self.bonus]["attributes"]["current_temperature"] = (69 - 32) * 5 / 9
                plan = self.decide(states, cfg, unit="°C")
                self.assertAlmostEqual(plan.head_call_targets_f["bonus"], expected * 9 / 5 + 32)
                ha, executor = self.executor(states, cfg, unit="°C")
                executor.step(plan, ha.states(), 0)
                self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], expected)

    def test_higher_advertised_minimum_still_wins(self):
        states = self.states()
        states[self.bonus]["attributes"]["min_temp"] = 68
        plan = self.decide(states)
        self.assertEqual(plan.head_call_targets_f["bonus"], 68)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 68)

    def test_floor_does_not_weaken_a_warmer_valid_cooling_call(self):
        states = self.states()
        states[self.bonus]["attributes"]["current_temperature"] = 72
        plan = self.decide(states)
        self.assertEqual(plan.head_call_targets_f["bonus"], 67)
        ha, executor = self.executor(states)
        executor.step(plan, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 67)

    def test_executor_rebounds_stale_plan_below_configured_cooling_floor(self):
        states = self.states()
        stale = Plan(
            mode="cool", head_targets_f={"bonus": 72},
            head_call_targets_f={"bonus": 64},
        )
        ha, executor = self.executor(states)
        executor.step(deepcopy(stale), ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 65)
        executor.step(deepcopy(stale), ha.states(), 15)
        self.assertNotIn(self.bonus, executor.pending)
        self.assertNotIn(self.bonus, executor.recoveries)
        self.assertEqual(len(self.activation(ha, self.bonus)), 1)

    def test_direct_climate_command_also_enforces_cooling_floor(self):
        states = self.states()
        ha, executor = self.executor(states)
        executor.climate(self.bonus, "cool", 64, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 65)
        executor.climate(self.bonus, "cool", 64, ha.states(), 15)
        self.assertNotIn(self.bonus, executor.pending)
        self.assertNotIn(self.bonus, executor.device_issues)

    def test_impossible_floor_stops_only_affected_head_and_explains_configuration_block(self):
        states = self.states()
        states[self.bonus]["state"] = "cool"
        states[self.bonus]["attributes"].update(max_temp=64, temperature=64)
        plan = self.decide(states)
        ha, executor = self.executor(states)
        executor.step(deepcopy(plan), ha.states(), 0)
        executor.step(deepcopy(plan), ha.states(), 15)
        self.assertEqual(self.activation(ha, self.bonus), [])
        self.assertEqual(ha.data[self.bonus]["state"], "off")
        self.assertEqual(ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.activation(ha, self.guest)[0]["temperature"], 64)
        self.assertEqual(len(self.activation(ha, self.guest)), 1)
        self.assertEqual(executor.phase, "running")
        detail = executor.recovery_status(15)["devices"][self.bonus]
        self.assertEqual(detail["kind"], "configuration")
        self.assertIn("65", detail["reason"])
        self.assertIn("64", detail["reason"])
        # Configuration disagreement must not be mistaken for a Wi-Fi failure
        # and trigger a reload of the integration owning healthy heads.
        self.assertNotIn(self.bonus, executor.recoveries)

    def test_reported_floor_target_strictly_acknowledges_without_recovery_or_off(self):
        states = self.states()
        plan = self.decide(states)
        ha, executor = self.executor(states)
        executor.step(deepcopy(plan), ha.states(), 0)
        self.assertEqual(ha.data[self.bonus]["attributes"]["temperature"], 65)
        executor.step(deepcopy(plan), ha.states(), 15)
        executor.step(deepcopy(plan), ha.states(), 60)
        self.assertNotIn(self.bonus, executor.pending)
        self.assertNotIn(self.bonus, executor.device_issues)
        self.assertEqual(len(self.activation(ha, self.bonus)), 1)
        self.assertFalse(any(
            service == "set_hvac_mode" and data["entity_id"] == self.bonus
            and data["hvac_mode"] == "off" for _, service, data in ha.calls
        ))
        # Off commands remain available immediately; a hardware floor is not
        # a reason to retain a room call or postpone a safety stop.
        executor.step(Plan(), ha.states(), 75)
        self.assertEqual(ha.data[self.bonus]["state"], "off")

    def test_heating_auxiliary_heater_and_fans_ignore_cooling_floor(self):
        cfg = replace(
            self.base, head_cool_min_f={self.bonus: 74}, head_heat_call_offset_f=0,
        )
        states = fixture(cfg, office=68, entertainment=68, living=67)
        plan = self.decide(states, cfg)
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(plan.head_call_targets_f["bonus"], 72)
        self.assertTrue(plan.heater_on)
        fan = cfg.fans["office"]
        plan.fans[fan] = 60
        ha, executor = self.executor(states, cfg, mode="heat")
        executor.step(plan, ha.states(), 0)
        self.assertEqual(self.activation(ha, self.bonus)[0]["temperature"], 72)
        self.assertEqual(self.activation(ha, cfg.heater_entity)[0]["temperature"], 86)
        self.assertEqual(ha.data[fan]["attributes"]["percentage"], 60)

    def test_schema_two_roundtrip_preserves_entity_mapping_and_fahrenheit_value(self):
        raw = self.cfg.to_dict()
        self.assertEqual(raw["head_cool_min_f"], {self.bonus: 65})
        restored = Config.from_dict(json.loads(json.dumps(raw)))
        self.assertEqual(restored.head_cool_min_f, {self.bonus: 65})
        raw["head_cool_min_f"][self.bonus] = 70
        self.assertEqual(restored.head_cool_min_f[self.bonus], 65)

    def test_legacy_import_and_schema_two_export_preserve_hardware_floor(self):
        raw = tomllib.loads((Path(__file__).parent / "fixtures" / "legacy.toml").read_text())
        raw["head_cool_min_f"] = {raw["heads"]["bonus"]: 65}
        imported = Config.from_dict(raw)
        self.assertTrue(imported.legacy)
        self.assertEqual(imported.head_cool_min_f, {self.bonus: 65})
        restored = Config.from_dict(imported.to_dict())
        self.assertFalse(restored.legacy)
        self.assertEqual(restored.head_cool_min_f, {self.bonus: 65})

    def test_invalid_override_mappings_are_rejected_before_control(self):
        for overrides in (
            None, [], [65], "65", 65, True,
            {"bonus": 65},
            {"climate.not_configured": 65},
            {self.base.heater_entity: 65},
            {self.base.fans["office"]: 65},
            {self.bonus: None},
            {self.bonus: True},
            {self.bonus: False},
            {self.bonus: "65"},
            {self.bonus: float("nan")},
            {self.bonus: float("inf")},
            {self.bonus: float("-inf")},
            {self.bonus: -41},
            {self.bonus: 141},
            {self.bonus: []},
            {self.bonus: {}},
        ):
            with self.subTest(overrides=overrides):
                raw = self.base.to_dict()
                raw["head_cool_min_f"] = overrides
                with self.assertRaises(ValueError):
                    Config.from_dict(raw)
