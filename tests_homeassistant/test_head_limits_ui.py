"""Known Mitsubishi limits are part of the house policy, not an editor toggle."""
import unittest

from custom_components.house_climate.config_flow import HouseClimateConfigFlow
from custom_components.house_climate.configuration import entry_configuration
from custom_components.house_climate.house_policy import house_configuration
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.policy import call_target_f
from tests.test_rooms import states


class FixedHeadLimitsTests(unittest.TestCase):
    def setUp(self):
        self.bindings = example_bindings()

    def test_all_four_heads_always_have_the_confirmed_65f_cooling_floor(self):
        config = house_configuration(bindings=self.bindings)
        self.assertEqual(config.head_cool_min_f, {entity: 65 for entity in config.heads.values()})
        self.assertFalse(hasattr(HouseClimateConfigFlow(), "async_step_head_limit"))
        self.assertFalse(hasattr(HouseClimateConfigFlow(), "async_step_head_limits"))

    def test_saved_disabled_or_too_low_overrides_cannot_bring_back_unreachable_calls(self):
        previous = house_configuration(bindings=self.bindings).to_dict()
        previous["head_cool_min_f"] = {}
        config = entry_configuration("/unused", {"policy": "house", "configuration": previous}, self.bindings)
        data = states(config)
        for entity in config.heads.values():
            data[entity]["attributes"].update(current_temperature=62, min_temp=60, max_temp=90)
            self.assertEqual(call_target_f(config, entity, "cool", 68, data, "°F"), 65)

    def test_real_device_grid_and_higher_floor_still_take_precedence(self):
        config = house_configuration(bindings=self.bindings)
        data = states(config)
        entity = config.heads["primary"]
        data[entity]["attributes"].update(current_temperature=62, min_temp=66, max_temp=90,
                                          target_temp_step=1)
        self.assertEqual(call_target_f(config, entity, "cool", 68, data, "°F"), 66)

    def test_equipment_temperature_still_compensates_actuator_setpoint(self):
        config = house_configuration(bindings=self.bindings)
        data = states(config)
        entity = config.heads["guest"]
        data[entity]["attributes"].update(current_temperature=74, min_temp=60, max_temp=90)
        self.assertEqual(call_target_f(config, entity, "heat", 68, data, "°F"), 79)
        self.assertTrue(all("heat_pump" not in probe.entity_id
                            for room in config.zones.values() for probe in room.probes))


if __name__ == "__main__":
    unittest.main()
