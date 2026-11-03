"""Explicit purifier wiring and durable house-policy minimums use synthetic devices."""
from copy import deepcopy
from dataclasses import replace
import unittest

from house_climate.bindings import BindingsError, HouseBindings, example_bindings
from house_climate.config import Config
from house_climate.house_policy import editable_policy_settings, house_configuration
from tests.test_rooms import configuration


class PurifierConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.bindings = example_bindings()

    def with_living(self):
        raw = self.bindings.to_dict()
        raw["fans"]["living"] = "fan.fixture_living_purifier"
        return HouseBindings.from_dict(raw)

    def test_existing_four_role_bindings_remain_valid_and_no_living_is_inferred(self):
        cfg = house_configuration(bindings=self.bindings)
        self.assertEqual(set(cfg.fan_controls), set(self.bindings.fans.values()))
        self.assertNotIn("living", self.bindings.fans)
        self.assertEqual(cfg.purifiers.minimum_percentage, 20)
        self.assertEqual(cfg.purifiers.living_percentage, 100)
        for fan in cfg.fan_controls.values():
            self.assertEqual(fan.minimum_percentage, 20)

    def test_optional_living_is_a_distinct_always_control_and_roundtrips(self):
        bindings = self.with_living()
        self.assertEqual(HouseBindings.from_dict(bindings.to_dict()), bindings)
        self.assertEqual(replace(self.bindings, fans=dict(bindings.fans)), bindings)
        cfg = house_configuration(bindings=bindings)
        living = cfg.fan_controls[bindings.fans["living"]]
        self.assertEqual((living.room, living.strategy, living.percentage,
                          living.minimum_percentage), ("living", "always", 100, 100))
        self.assertEqual(Config.from_dict(cfg.to_dict()).fan_controls, cfg.fan_controls)
        self.assertEqual(cfg.fallback.fan_percentage, 40)

    def test_optional_living_does_not_make_required_roles_optional(self):
        for role in self.bindings.fans:
            raw = self.with_living().to_dict()
            del raw["fans"][role]
            with self.subTest(role=role), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_unknown_role_duplicate_device_and_wrong_domain_are_rejected(self):
        for role, entity in (("unknown", "fan.fixture_extra"),
                             ("living", self.bindings.fans["primary"]),
                             ("living", "climate.fixture_heater")):
            raw = self.bindings.to_dict()
            raw["fans"][role] = entity
            with self.subTest(role=role, entity=entity), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)
            with self.subTest(direct=True, role=role, entity=entity), self.assertRaises(BindingsError):
                replace(self.bindings, fans=raw["fans"])

    def test_explicit_minimum_preserves_existing_schedules_strategies_and_boosts(self):
        before = house_configuration(bindings=self.bindings)
        after = house_configuration(bindings=self.bindings,
                                    policy_settings={"purifiers": {"minimum_percentage": 35}})
        self.assertEqual(after.zones, before.zones)
        for entity, original in before.fan_controls.items():
            self.assertEqual(after.fan_controls[entity], replace(original, minimum_percentage=35))
        self.assertEqual(after.zones["primary"].schedules[0].profile.fan_percentage, 60)
        self.assertEqual(after.zones["primary"].schedules[1].profile.fan_percentage, 60)
        self.assertEqual(after.auxiliary_policy, before.auxiliary_policy)
        self.assertEqual(after.fallback, before.fallback)

    def test_living_preference_survives_export_without_a_living_binding(self):
        cfg = house_configuration(bindings=self.bindings,
                                  policy_settings={"purifiers": {"living_percentage": 75}})
        exported = editable_policy_settings(cfg, self.bindings)
        self.assertEqual(exported["purifiers"], {"minimum_percentage": 20, "living_percentage": 75})
        restored = house_configuration(bindings=self.bindings, policy_settings=exported)
        self.assertEqual(restored.purifiers, cfg.purifiers)
        bound = self.with_living()
        later = house_configuration(bindings=bound, policy_settings=exported)
        self.assertEqual(later.fan_controls[bound.fans["living"]].percentage, 75)
        self.assertEqual(later.fan_controls[bound.fans["living"]].minimum_percentage, 75)

    def test_percentage_numbers_and_ordering_are_strict(self):
        for field in ("minimum_percentage", "living_percentage"):
            for value in (True, False, "20", None, -1, 101, float("nan"), float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    house_configuration(bindings=self.bindings,
                                        policy_settings={"purifiers": {field: value}})
        for value in (None, [], "20", {"unexpected": 20},
                      {"minimum_percentage": 40, "living_percentage": 30}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                house_configuration(bindings=self.bindings, policy_settings={"purifiers": value})

    def test_zero_and_full_percentage_boundaries_are_valid(self):
        for value in (0, 20.5, 100):
            cfg = house_configuration(bindings=self.with_living(), policy_settings={
                "purifiers": {"minimum_percentage": value, "living_percentage": value}})
            self.assertTrue(all(fan.minimum_percentage == value for fan in cfg.fan_controls.values()))

    def test_old_generic_room_configuration_retains_zero_minimum(self):
        cfg = Config.from_dict(deepcopy(configuration()))
        self.assertEqual(cfg.purifiers.minimum_percentage, 0)
        self.assertTrue(all(fan.minimum_percentage == 0 for fan in cfg.fan_controls.values()))
        restored = Config.from_dict(cfg.to_dict())
        self.assertEqual(restored.fan_controls, cfg.fan_controls)


if __name__ == "__main__":
    unittest.main()
