"""Optional physical stages are explicit and never borrow absent-device evidence."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest

from house_climate.aggregate_stop import AggregateStopConfig
from house_climate.bindings import BindingsError, HouseBindings, example_bindings


class OptionalPowerBindingsTests(unittest.TestCase):
    def setUp(self):
        self.original = example_bindings()
        self.raw = self.original.to_dict()

    def direct(self, *, meter=None):
        raw = deepcopy(self.raw)
        raw["power"].update(heater_switch=None, heater_current=meter,
                            emergency_ac_switch="switch.shared_ac_output")
        return raw

    def test_old_wiring_retains_identical_round_trip_and_disabled_fallback(self):
        parsed = HouseBindings.from_dict(self.raw)
        self.assertIsNone(parsed.power.emergency_ac_switch)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertIsNone(parsed.power.aggregate_stop_confirmation)
        self.assertIsNone(parsed.power.emergency_notify_service)
        self.assertEqual(parsed.to_dict(), self.raw)
        self.assertEqual(parsed.power.entity_ids, self.original.power.entity_ids)

    def test_direct_path_explicitly_retires_cutoff_and_meter(self):
        raw = self.direct()
        power = HouseBindings.from_dict(raw).power
        self.assertIsNone(power.heater_switch)
        self.assertIsNone(power.heater_current)
        self.assertEqual(power.emergency_ac_switch, "switch.shared_ac_output")
        self.assertNotIn(self.original.power.heater_switch, power.entity_ids)
        self.assertNotIn(self.original.power.heater_current, power.entity_ids)
        self.assertNotIn(None, power.entity_ids)
        self.assertEqual(len(power.entity_ids), 8)
        self.assertEqual(HouseBindings.from_dict(raw).to_dict(), raw)

    def test_binding_the_shared_switch_does_not_arm_fallback(self):
        raw = self.direct()
        power = HouseBindings.from_dict(raw).power
        self.assertFalse(power.emergency_ac_fallback_enabled)
        raw["power"]["emergency_ac_fallback_enabled"] = True
        armed = HouseBindings.from_dict(raw)
        self.assertTrue(armed.power.emergency_ac_fallback_enabled)
        self.assertEqual(armed.to_dict(), raw)
        self.assertNotIn(True, armed.power.entity_ids)

    def test_fallback_arming_requires_boolean_and_a_separate_target(self):
        for value in (0, 1, None, "true", []):
            raw = self.direct()
            raw["power"]["emergency_ac_fallback_enabled"] = value
            with self.subTest(value=value), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)
        raw = deepcopy(self.raw)
        raw["power"]["emergency_ac_fallback_enabled"] = True
        with self.assertRaisesRegex(BindingsError, "requires power.emergency_ac_switch"):
            HouseBindings.from_dict(raw)

    def test_explicit_false_arming_is_canonicalized_to_absent(self):
        raw = self.direct()
        raw["power"]["emergency_ac_fallback_enabled"] = False
        parsed = HouseBindings.from_dict(raw)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertNotIn("emergency_ac_fallback_enabled", parsed.to_dict()["power"])

    def test_notify_group_is_a_service_not_a_tracked_device_or_arming_flag(self):
        raw = self.direct()
        raw["windows"] = {"office": ["binary_sensor.office_opening"]}
        raw["outdoor"] = [{"entity_id": "sensor.exterior_temperature",
                           "source": "exterior_probe", "role": "primary"}]
        before = HouseBindings.from_dict(raw)
        raw["power"]["emergency_notify_service"] = "notify.household_alerts"
        parsed = HouseBindings.from_dict(raw)
        self.assertEqual(parsed.power.emergency_notify_service, "notify.household_alerts")
        self.assertEqual(parsed.power.entity_ids, before.power.entity_ids)
        self.assertNotIn("notify.household_alerts", parsed.power.entity_ids)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertEqual(parsed.to_dict(), raw)

    def test_explicit_null_notify_service_keeps_old_round_trip(self):
        raw = deepcopy(self.raw)
        raw["power"]["emergency_notify_service"] = None
        parsed = HouseBindings.from_dict(raw)
        self.assertIsNone(parsed.power.emergency_notify_service)
        self.assertEqual(parsed.to_dict(), self.raw)

    def test_notify_service_rejects_entities_and_malformed_names(self):
        for value in ("switch.household_alerts", "sensor.household_alerts", "notify",
                      "notify.", "notify.group.recipient", "notify.HouseholdAlerts",
                      "notify.household-alerts", " notify.household_alerts", "", True, 1, [], {}):
            raw = deepcopy(self.raw)
            raw["power"]["emergency_notify_service"] = value
            with self.subTest(value=value), self.assertRaisesRegex(BindingsError, "notify service"):
                HouseBindings.from_dict(raw)
            with self.subTest(constructor=value), self.assertRaisesRegex(BindingsError, "notify service"):
                replace(self.original.power, emergency_notify_service=value)

    def test_meter_and_individual_cutoff_are_independently_optional(self):
        direct_metered = HouseBindings.from_dict(self.direct(meter="sensor.heater_only_current"))
        self.assertIn("sensor.heater_only_current", direct_metered.power.entity_ids)
        self.assertEqual(len(direct_metered.power.entity_ids), 9)
        dedicated_without_meter = deepcopy(self.raw)
        dedicated_without_meter["power"]["heater_current"] = None
        parsed = HouseBindings.from_dict(dedicated_without_meter)
        self.assertEqual(parsed.power.heater_switch, self.original.power.heater_switch)
        self.assertIsNone(parsed.power.heater_current)
        self.assertEqual(parsed.to_dict(), dedicated_without_meter)

    def test_direct_path_without_last_resort_cutoff_fails_closed(self):
        for supplied in (False, True):
            raw = self.direct()
            if supplied:
                raw["power"]["emergency_ac_switch"] = None
            else:
                del raw["power"]["emergency_ac_switch"]
            with self.subTest(explicit_null=supplied), self.assertRaisesRegex(
                BindingsError, "direct heater path"):
                HouseBindings.from_dict(raw)

    def test_missing_roles_do_not_silently_become_optional(self):
        for key in ("heater_switch", "heater_current"):
            raw = self.direct()
            del raw["power"][key]
            with self.subTest(key=key), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_wrong_domains_and_aliasing_of_stages_are_rejected(self):
        for key, value in (
            ("heater_switch", "climate.not_a_cutoff"),
            ("heater_current", "switch.not_a_measurement"),
            ("emergency_ac_switch", "sensor.not_a_cutoff"),
            ("emergency_ac_switch", True),
            ("emergency_ac_switch", ""),
            ("emergency_ac_switch", self.original.power.heater_switch),
            ("heater_current", self.original.power.input_power),
        ):
            raw = deepcopy(self.raw)
            raw["power"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_none_only_has_meaning_for_optional_cutoff_and_meter_roles(self):
        for key in ("battery", "circuit_power", "input_power", "output_power",
                    "wall_phase_a_voltage", "wall_phase_b_voltage"):
            raw = self.direct()
            raw["power"][key] = None
            with self.subTest(key=key), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_aggregate_calibration_parses_and_preserves_other_sections(self):
        raw = self.direct()
        raw["windows"] = {"office": ["binary_sensor.office_opening"]}
        raw["outdoor"] = [{"entity_id": "sensor.exterior_temperature",
                           "source": "exterior_probe", "role": "primary"}]
        raw["power"]["aggregate_stop_confirmation"] = {
            "minimum_drop_w": 700, "residual_ceiling_w": 350, "noise_margin_w": 30}
        parsed = HouseBindings.from_dict(raw)
        policy = parsed.power.aggregate_stop_confirmation
        self.assertIsInstance(policy, AggregateStopConfig)
        self.assertEqual(parsed.to_dict()["power"]["aggregate_stop_confirmation"],
                         policy.to_dict())
        for section in ("heads", "rooms", "fans", "heater", "windows", "outdoor"):
            self.assertEqual(parsed.to_dict()[section], raw[section])
        self.assertTrue(all(isinstance(value, str) for value in parsed.power.entity_ids))
        self.assertEqual(len(parsed.power.entity_ids), 8)

    def test_invalid_or_incomplete_calibration_is_not_accepted_as_safe_policy(self):
        for calibration in ({}, {"minimum_drop_w": 700}, [], True,
                            {"minimum_drop_w": 700, "residual_ceiling_w": 350,
                             "noise_margin_w": 30, "disable_safety": True}):
            raw = self.direct()
            raw["power"]["aggregate_stop_confirmation"] = calibration
            with self.subTest(calibration=calibration), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_relative_calibration_round_trip_does_not_arm_or_rebind_devices(self):
        raw = self.direct()
        before = HouseBindings.from_dict(raw)
        raw["power"]["aggregate_stop_confirmation"] = {
            "minimum_drop_w": 1000, "residual_ceiling_w": 160, "noise_margin_w": 20,
            "minimum_samples": 3, "settle_seconds": 30, "max_age_seconds": 90,
            "timeout_seconds": 180, "relative_drop_enabled": True}
        parsed = HouseBindings.from_dict(raw)
        self.assertTrue(parsed.power.aggregate_stop_confirmation.relative_drop_enabled)
        self.assertEqual(parsed.to_dict(), raw)
        self.assertEqual(parsed.power.entity_ids, before.power.entity_ids)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertIsNone(parsed.power.heater_switch)
        self.assertIsNone(parsed.power.heater_current)

    def test_relative_calibration_requires_explicit_boolean_and_feasible_baseline(self):
        policy = {"minimum_drop_w": 1000, "residual_ceiling_w": 160, "noise_margin_w": 20,
                  "minimum_samples": 3, "settle_seconds": 30, "max_age_seconds": 90,
                  "timeout_seconds": 180, "relative_drop_enabled": True}
        for key, value in (("relative_drop_enabled", 1), ("relative_drop_enabled", "true"),
                           ("relative_drop_enabled", None), ("minimum_samples", 2),
                           ("minimum_samples", 129),
                           ("settle_seconds", 91)):
            raw = self.direct()
            raw["power"]["aggregate_stop_confirmation"] = dict(policy, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(BindingsError):
                HouseBindings.from_dict(raw)

    def test_direct_constructors_require_validated_calibration_and_are_immutable(self):
        with self.assertRaises(BindingsError):
            replace(self.original.power, aggregate_stop_confirmation={})
        policy = AggregateStopConfig.from_dict({
            "minimum_drop_w": 700, "residual_ceiling_w": 350, "noise_margin_w": 30})
        power = replace(self.original.power, emergency_ac_switch="switch.shared_ac_output",
                        aggregate_stop_confirmation=policy)
        self.assertEqual(power.to_dict()["aggregate_stop_confirmation"], policy.to_dict())
        with self.assertRaises(FrozenInstanceError):
            power.emergency_ac_switch = "switch.changed_output"


if __name__ == "__main__":
    unittest.main()
