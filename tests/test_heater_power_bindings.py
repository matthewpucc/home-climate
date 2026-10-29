"""A dedicated watt meter is an explicit alternative to the legacy amp meter."""
from copy import deepcopy
from dataclasses import replace
import unittest

from house_climate.bindings import BindingsError, HouseBindings, example_bindings


class HeaterPowerBindingsTests(unittest.TestCase):
    def setUp(self):
        self.original = example_bindings()
        self.raw = self.original.to_dict()
        self.watt_sensor = "sensor.auxiliary_heater_watts"

    def watts(self):
        raw = deepcopy(self.raw)
        raw["power"].update(heater_current=None, heater_power=self.watt_sensor)
        return raw

    def test_legacy_amp_wiring_round_trip_is_unchanged(self):
        parsed = HouseBindings.from_dict(self.raw)
        self.assertIsNone(parsed.power.heater_power)
        self.assertEqual(parsed.power.heater_current, self.original.power.heater_current)
        self.assertNotIn("heater_power", parsed.to_dict()["power"])
        self.assertEqual(parsed.to_dict(), self.raw)
        self.assertEqual(parsed.power.entity_ids, self.original.power.entity_ids)

    def test_explicit_null_power_is_equivalent_to_absent_for_existing_paths(self):
        for path in ("amps", "switch_only", "direct"):
            original = deepcopy(self.raw)
            if path != "amps":
                original["power"]["heater_current"] = None
            if path == "direct":
                original["power"].update(heater_switch=None,
                                         emergency_ac_switch="switch.reserve_ac_output")
            with_null = deepcopy(original)
            with_null["power"]["heater_power"] = None
            with self.subTest(path=path):
                parsed = HouseBindings.from_dict(with_null)
                self.assertIsNone(parsed.power.heater_power)
                self.assertEqual(parsed.to_dict(), original)
                self.assertEqual(parsed.power.entity_ids,
                                 HouseBindings.from_dict(original).power.entity_ids)

    def test_dedicated_watts_round_trip_preserves_other_sections_and_fallback(self):
        raw = self.watts()
        raw["windows"] = {"office": ["binary_sensor.office_opening"]}
        raw["outdoor"] = [{"entity_id": "sensor.exterior_temperature",
                           "source": "exterior_probe", "role": "primary"}]
        parsed = HouseBindings.from_dict(raw)
        self.assertIsNone(parsed.power.heater_current)
        self.assertEqual(parsed.power.heater_power, self.watt_sensor)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertEqual(parsed.to_dict(), raw)
        self.assertIn(self.watt_sensor, parsed.power.entity_ids)
        self.assertNotIn(self.original.power.heater_current, parsed.power.entity_ids)
        self.assertNotIn(None, parsed.power.entity_ids)
        self.assertEqual(len(parsed.power.entity_ids), len(self.original.power.entity_ids))

    def test_direct_path_can_use_watts_without_arming_shared_cutoff(self):
        raw = self.watts()
        raw["power"].update(heater_switch=None,
                            emergency_ac_switch="switch.reserve_ac_output")
        parsed = HouseBindings.from_dict(raw)
        self.assertEqual(parsed.power.heater_power, self.watt_sensor)
        self.assertIsNone(parsed.power.heater_switch)
        self.assertFalse(parsed.power.emergency_ac_fallback_enabled)
        self.assertEqual(parsed.to_dict(), raw)

    def test_direct_constructor_accepts_the_explicit_alternative_meter(self):
        power = replace(self.original.power, heater_current=None,
                        heater_power=self.watt_sensor)
        self.assertEqual(power.heater_power, self.watt_sensor)
        self.assertIn(self.watt_sensor, power.entity_ids)
        self.assertNotIn(self.original.power.heater_current, power.entity_ids)
        self.assertEqual(power.to_dict()["heater_power"], self.watt_sensor)

    def test_two_load_meters_are_rejected_instead_of_silently_selecting_one(self):
        raw = deepcopy(self.raw)
        raw["power"]["heater_power"] = self.watt_sensor
        with self.assertRaisesRegex(BindingsError, "Choose one heater load meter"):
            HouseBindings.from_dict(raw)
        with self.assertRaisesRegex(BindingsError, "Choose one heater load meter"):
            replace(self.original.power, heater_power=self.watt_sensor)

    def test_new_meter_still_requires_a_sensor_entity_id(self):
        for value in ("switch.heater_meter", "climate.heater_meter", "sensor.",
                      "sensor.invalid.name", "sensor.Heater", "sensor.heater-meter",
                      "", 1, True, [], {}):
            raw = self.watts()
            raw["power"]["heater_power"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                    BindingsError, "power.heater_power must be a sensor entity ID"):
                HouseBindings.from_dict(raw)

    def test_individual_watts_cannot_alias_other_power_roles(self):
        for role in ("battery", "circuit_power", "input_power", "output_power",
                     "wall_phase_a_voltage", "wall_phase_b_voltage"):
            raw = self.watts()
            raw["power"]["heater_power"] = raw["power"][role]
            with self.subTest(role=role), self.assertRaisesRegex(
                    BindingsError, "distinct device entities"):
                HouseBindings.from_dict(raw)

    def test_watts_does_not_make_legacy_required_keys_or_unknown_fields_optional(self):
        raw = self.watts()
        del raw["power"]["heater_current"]
        with self.assertRaises(BindingsError):
            HouseBindings.from_dict(raw)
        raw = self.watts()
        raw["power"]["heater_power_timeout"] = 600
        with self.assertRaises(BindingsError):
            HouseBindings.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
