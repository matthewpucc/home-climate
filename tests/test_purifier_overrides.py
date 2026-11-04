"""Room overrides survive decisions without changing other rooms or HVAC intent."""
from copy import deepcopy
from datetime import timedelta
import unittest

from house_climate.bindings import example_bindings
from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.house_policy import editable_policy_settings, house_configuration
from house_climate.policy import Policy
from tests.test_controller import FakeHA
from tests.test_directed_airflow import directed_config, FAN
from tests.test_rooms import configuration, states, NOW


class PurifierOverrideTests(unittest.TestCase):
    def test_house_room_isolation_across_decisions_and_maintenance(self):
        bindings = example_bindings()
        baseline = house_configuration(bindings=bindings)
        cfg = house_configuration(bindings=bindings, policy_settings={
            "purifiers": {"room_overrides": {"office": 70, "entertainment": 70}}})
        self.assertEqual(cfg.zones, baseline.zones)
        for role in ("primary", "guest"):
            self.assertEqual(cfg.fan_controls[bindings.fans[role]], baseline.fan_controls[bindings.fans[role]])
        cadence = DecisionCadence(cfg, Policy(cfg), clock=lambda: 0)
        ordinary = DecisionCadence(baseline, Policy(baseline), clock=lambda: 0)
        for seconds in (0, 15, 90, 105, 180):
            now = NOW + timedelta(seconds=seconds)
            data = states(cfg, now=now)
            if seconds >= 105:
                for probe in cfg.zones["office"].probes:
                    data.pop(probe.entity_id)
            plan = cadence.decide(data, now, Settings.read(data, cfg), monotonic_now=seconds)
            before = ordinary.decide(data, now, Settings.read(data, baseline), monotonic_now=seconds)
            self.assertEqual(plan.head_targets_f, before.head_targets_f)
            self.assertEqual(plan.mode, before.mode)
            for role in ("office", "entertainment"):
                self.assertEqual(plan.fans[bindings.fans[role]], 70)
                self.assertEqual(plan.fan_airflow[bindings.fans[role]]["profile"],
                                 before.fan_airflow[bindings.fans[role]]["profile"])
            for role in ("primary", "guest"):
                self.assertEqual(plan.fans[bindings.fans[role]], before.fans[bindings.fans[role]])

    def test_policy_roundtrip_clear_and_migrated_room_identity(self):
        bindings = example_bindings()
        previous = house_configuration(bindings=bindings).to_dict()
        previous["rooms"]["saved_office_id"] = previous["rooms"].pop("office")
        cfg = house_configuration(previous, bindings, policy_settings={
            "purifiers": {"room_overrides": {"office": 70, "living": 50}}})
        fan = cfg.fan_controls[bindings.fans["office"]]
        self.assertEqual(fan.room, "saved_office_id")
        self.assertEqual(fan.speed_override, 70)
        self.assertEqual(Config.from_dict(cfg.to_dict()).fan_controls, cfg.fan_controls)
        saved = editable_policy_settings(cfg, bindings)
        self.assertEqual(saved["purifiers"]["room_overrides"], {"office": 70, "living": 50})
        restored = house_configuration(previous, bindings, policy_settings=saved)
        self.assertEqual(restored.fan_controls, cfg.fan_controls)
        saved["purifiers"]["room_overrides"] = {}
        cleared = house_configuration(previous, bindings, policy_settings=saved)
        default = house_configuration(previous, bindings)
        self.assertEqual(cleared.fan_controls, default.fan_controls)

    def test_invalid_room_and_percentages_rejected(self):
        for overrides in (None, [], 70, {"unknown": 70}, {"Office": 70},
                          *({"office": value} for value in
                            (True, False, "70", None, -1, 101, float("nan"), float("inf")))):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                house_configuration(bindings=example_bindings(),
                                    policy_settings={"purifiers": {"room_overrides": overrides}})
        for value in (True, "70", -1, 101, float("nan"), float("inf")):
            raw = configuration()
            raw["rooms"]["alpha"]["fans"][0]["speed_override"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_override_replaces_minimum_and_scheduled_or_thermal_speed(self):
        for strategy in ("schedule", "cooling", "conditioning", "always", "transfer"):
            for value in (0, 10, 70, 100):
                raw = configuration()
                fan = raw["rooms"]["alpha"]["fans"][0]
                fan.update(strategy=strategy, percentage=90, minimum_percentage=20, speed_override=value)
                if strategy != "transfer":
                    fan.pop("source_room")
                raw["rooms"]["alpha"]["profile"]["fan_percentage"] = 95
                cfg = Config.from_dict(raw)
                for temperatures in ({"alpha": 72, "beta": 72}, {"alpha": 74, "beta": 70}):
                    with self.subTest(strategy=strategy, value=value, temperatures=temperatures):
                        plan = Policy(cfg).decide(states(cfg, temperatures), NOW, Settings.read({}, cfg))
                        self.assertEqual(plan.fans[FAN], value)

    def test_directed_override_keeps_profiles_and_survives_missing_adapter(self):
        for value in (0, 10, 70, 100):
            raw = directed_config()
            raw["rooms"]["alpha"]["fans"][0]["speed_override"] = value
            cfg = Config.from_dict(raw)
            data = states(cfg, {"alpha": 73, "beta": 72, "gamma": 68})
            plan = Policy(cfg).decide(data, NOW, Settings.read({}, cfg))
            self.assertEqual(plan.fans[FAN], value)
            self.assertEqual(plan.fan_airflow[FAN]["profile"], "toward_alpha" if value else None)
            ha = FakeHA(data)
            executor = Executor(cfg, ha, "°F")
            for tick in (0, 15, 30):
                executor.step(plan, ha.states(), tick)
            self.assertEqual(ha.data[FAN]["state"], "on" if value else "off")
            self.assertEqual(ha.data[FAN]["attributes"]["percentage"], value)
            self.assertFalse(any(call[1] in ("set_direction", "set_oscillation_angles") for call in ha.calls))

    def test_fractional_override_uses_existing_supported_rung_rounding(self):
        raw = configuration()
        raw["rooms"]["alpha"]["fans"][0]["speed_override"] = 65.5
        cfg = Config.from_dict(raw)
        data = states(cfg)
        data[FAN]["attributes"]["percentage_step"] = 10
        plan = Policy(cfg).decide(data, NOW, Settings.read({}, cfg))
        ha = FakeHA(deepcopy(data))
        Executor(cfg, ha, "°F").step(plan, data, 0)
        self.assertEqual(ha.data[FAN]["attributes"]["percentage"], 70)


if __name__ == "__main__":
    unittest.main()
