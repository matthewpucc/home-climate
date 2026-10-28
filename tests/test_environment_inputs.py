"""Protection freshness follows current valid outdoor data, not forecast churn."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.config import Config, Probe, Settings
from house_climate.protection import ProtectionTemperatures
from house_climate.recovery_inputs import signature
from house_climate.outdoor import OUTDOOR_MAX_AGE_SECONDS, WEATHER_MAX_AGE_SECONDS
from tests.test_rooms import NOW, configuration, states


class EnvironmentInputTests(unittest.TestCase):
    def setUp(self):
        self.weather = "weather.fixture_outdoor_input"
        self.cfg = replace(Config.from_dict(configuration()), protection=ProtectionTemperatures(),
                           outdoor_probes=(Probe(self.weather, "fixture_provider"),))
        self.data = states(self.cfg)
        self.data[self.weather] = {"state": "sunny", "last_reported": NOW.isoformat(),
                                   "attributes": {"temperature": 32, "temperature_unit": "°F"}}
        self.settings = Settings.read({}, self.cfg)

    def stamp(self, now=NOW, data=None):
        return signature(self.cfg, self.data if data is None else data, now, self.settings)

    def test_outdoor_expiry_changes_validity_without_a_new_entity_report(self):
        # Room probes remain deliberately report-on-change for this test so
        # the only expiring material input is the outdoor source.
        self.cfg = replace(self.cfg, zones={name: replace(room, probes=tuple(
            replace(probe, report_on_change=True) for probe in room.probes))
            for name, room in self.cfg.zones.items()})
        before = self.stamp()
        self.assertEqual(self.stamp(NOW + timedelta(seconds=WEATHER_MAX_AGE_SECONDS - 1)), before)
        self.assertNotEqual(self.stamp(NOW + timedelta(seconds=WEATHER_MAX_AGE_SECONDS)), before)

    def test_physical_sensor_material_inputs_still_expire_at_thirty_minutes(self):
        entity = "sensor.fixture_external_temperature"
        self.cfg = replace(self.cfg, outdoor_probes=(Probe(entity, "local"),), zones={
            name: replace(room, probes=tuple(replace(probe, report_on_change=True) for probe in room.probes))
            for name, room in self.cfg.zones.items()})
        self.data[entity] = {"state": "32", "last_reported": NOW.isoformat(),
                             "attributes": {"unit_of_measurement": "°F"}}
        before = self.stamp()
        self.assertEqual(self.stamp(NOW + timedelta(seconds=OUTDOOR_MAX_AGE_SECONDS - 1)), before)
        self.assertNotEqual(self.stamp(NOW + timedelta(seconds=OUTDOOR_MAX_AGE_SECONDS)), before)

    def test_forecasts_and_identical_current_report_refresh_do_not_change_material_inputs(self):
        before = self.stamp()
        self.data[self.weather]["attributes"]["forecast"] = [{"temperature": -40}]
        self.data[self.weather]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(self.stamp(NOW + timedelta(seconds=1)), before)

    def test_current_unit_normalization_and_actual_temperature_changes(self):
        before = self.stamp()
        self.data[self.weather]["attributes"].update(temperature=0, temperature_unit="°C")
        self.assertEqual(self.stamp(), before)
        changed = deepcopy(self.data)
        changed[self.weather]["attributes"]["temperature"] = -1
        self.assertNotEqual(self.stamp(data=changed), before)


if __name__ == "__main__":
    unittest.main()
