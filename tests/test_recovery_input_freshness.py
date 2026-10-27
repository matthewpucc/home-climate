"""Only material decision input changes should delay integration repair."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.config import Config, Settings
from house_climate.recovery_inputs import signature
from tests.test_rooms import NOW, configuration, states


class RecoveryInputFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(configuration())
        self.data = states(self.cfg)
        self.settings = Settings.read({}, self.cfg)

    def stamp(self, data=None, now=NOW, settings=None):
        return signature(self.cfg, self.data if data is None else data, now,
                         self.settings if settings is None else settings)

    def test_identical_reports_heartbeat_status_and_unrelated_noise_are_ignored(self):
        before = self.stamp()
        for state in self.data.values():
            state.update(last_updated=(NOW + timedelta(seconds=1)).isoformat(),
                         last_reported=(NOW + timedelta(seconds=1)).isoformat())
        self.data["input_datetime.test_heartbeat"] = {"state": "new heartbeat"}
        self.data["sensor.test_unrelated"] = {"state": "999"}
        self.data["input_text.test_status"] = {"state": "running"}
        self.assertEqual(self.stamp(now=NOW + timedelta(seconds=2)), before)

    def test_changed_probe_temperature_unit_or_availability_changes_the_gate(self):
        before = self.stamp()
        probe = self.cfg.zones["alpha"].probes[0].entity_id
        for change in ({"state": "70"}, {"state": "unavailable"},
                       {"attributes": {"unit_of_measurement": "°C"}}):
            data = deepcopy(self.data)
            data[probe].update(change)
            self.assertNotEqual(self.stamp(data), before)

    def test_probe_staleness_boundary_expires_without_a_new_report(self):
        boundary = NOW + timedelta(seconds=self.cfg.sensor_stale_seconds)
        self.assertEqual(self.stamp(now=boundary), self.stamp())
        self.assertNotEqual(self.stamp(now=boundary + timedelta(seconds=1)), self.stamp())

    def test_report_on_change_probe_does_not_expire_from_unchanged_timestamp(self):
        self.cfg = replace(self.cfg, zones={key: replace(room, probes=tuple(
            replace(probe, report_on_change=True) for probe in room.probes))
            for key, room in self.cfg.zones.items()})
        self.assertEqual(self.stamp(now=NOW + timedelta(seconds=1000)), self.stamp())

    def test_head_mode_capability_and_preset_are_material_but_mode_order_is_not(self):
        before = self.stamp()
        entity = self.cfg.heads["shared"]
        reordered = deepcopy(self.data)
        reordered[entity]["attributes"]["hvac_modes"].reverse()
        self.assertEqual(self.stamp(reordered), before)
        for change in ({"hvac_modes": ["off", "cool"]}, {"preset_mode": "eco"},
                       {"operation_mode": "summer"}):
            data = deepcopy(self.data)
            data[entity]["attributes"].update(change)
            self.assertNotEqual(self.stamp(data), before)
        self.data[entity]["state"] = "cool"
        self.assertNotEqual(self.stamp(), before)

    def test_schedule_transition_and_hold_are_material_even_with_same_sensor_values(self):
        before = self.stamp()
        self.assertNotEqual(self.stamp(now=NOW.replace(hour=5)), before)
        held = replace(self.settings, room_profiles={"alpha": self.cfg.zones["alpha"].profile})
        self.assertNotEqual(self.stamp(settings=held), before)


if __name__ == "__main__":
    unittest.main()
