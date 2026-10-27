"""Monotonic environmental evidence never delays a measured indoor emergency."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.config import Config, Probe, Profile, Settings
from house_climate.operating_state import OperatingState
from house_climate.protection import ProtectionTemperatures
from house_climate.sensors import Sensors
from tests.test_rooms import NOW, configuration, states

CONTACT = "binary_sensor.fixture_open_window"
OUTDOOR = "sensor.fixture_outdoor"


class OperatingContextTests(unittest.TestCase):
    def setUp(self):
        raw = configuration()
        raw["rooms"]["alpha"]["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
        base = Config.from_dict(raw)
        self.cfg = replace(base, protection=ProtectionTemperatures(),
                           outdoor_probes=(Probe(OUTDOOR, "fixture_station"),),
                           zones={**base.zones, "alpha": replace(base.zones["alpha"], windows=(CONTACT,))})
        self.operating = OperatingState(self.cfg)
        self.contact_state = "on"
        self.contact_changed = NOW.isoformat()

    def snapshot(self, seconds, *, outdoor=70, contact="on", temperatures=None, settings=None):
        now = NOW + timedelta(seconds=seconds)
        settings = settings or Settings.read({}, self.cfg)
        data = states(self.cfg, {"alpha": 69.5, "beta": 72, "gamma": 68, **(temperatures or {})}, now)
        data[OUTDOOR] = {"state": "unavailable" if outdoor is None else str(outdoor),
                         "last_reported": now.isoformat(), "attributes": {"unit_of_measurement": "°F"}}
        if contact != self.contact_state:
            self.contact_state, self.contact_changed = contact, now.isoformat()
        data[CONTACT] = {"state": contact, "last_changed": self.contact_changed}
        for name, value in (temperatures or {}).items():
            if value is None:
                for probe in self.cfg.zones[name].probes:
                    data[probe.entity_id]["state"] = "unavailable"
        context = self.operating.environment(data, now, monotonic_now=seconds)
        readings, _warnings = Sensors(self.cfg).read(data, now)
        profiles = {name: settings.profile(room, now, self.cfg.timezone, name)
                    for name, room in self.cfg.zones.items()}
        bands = {name: ((settings.value(profile.target_f) - settings.value(profile.half_band_f),
                         settings.value(profile.target_f) + settings.value(profile.half_band_f),
                         settings.value(profile.target_f)) if profile.comfort_enabled else
                        (context.low_f, context.high_f, (context.low_f + context.high_f) / 2))
                 for name, profile in profiles.items()}
        return self.operating.rooms(context, data, readings, bands, profiles, settings, now,
                                    monotonic_now=seconds)

    def test_warm_regime_needs_sustained_valid_time_but_indoor_emergency_does_not(self):
        first = self.snapshot(0, temperatures={"alpha": 64})
        self.assertEqual((first.low_f, first.outdoor_regime, first.tie_mode), (65, "unqualified", "heat"))
        self.assertTrue(first.emergencies["alpha"]["heat"]["measured"])
        self.assertEqual(self.snapshot(299).low_f, 65)
        qualified = self.snapshot(300)
        self.assertEqual((qualified.low_f, qualified.high_f, qualified.outdoor_regime, qualified.tie_mode),
                         (60, 80, "warm", "cool"))

    def test_qualified_regime_survives_deadband_and_changes_only_after_qualification(self):
        self.snapshot(0)
        self.snapshot(300)
        boundary = self.snapshot(301, outdoor=32)
        self.assertEqual((boundary.low_f, boundary.tie_mode), (60, "cool"))
        self.snapshot(400, outdoor=29.9)
        self.assertEqual(self.snapshot(699, outdoor=29.9).low_f, 60)
        cold = self.snapshot(700, outdoor=29.9)
        self.assertEqual((cold.low_f, cold.outdoor_regime, cold.tie_mode), (65, "cold", "heat"))
        self.assertEqual(self.snapshot(701, outdoor=32).low_f, 65)
        self.snapshot(800, outdoor=34.1)
        self.assertEqual(self.snapshot(1099, outdoor=34.1).low_f, 65)
        self.assertEqual(self.snapshot(1100, outdoor=34.1).low_f, 60)

    def test_unknown_outdoor_restores_conservative_floor_without_claiming_freezing(self):
        self.snapshot(0)
        self.snapshot(300)
        unknown = self.snapshot(301, outdoor=None, temperatures={"alpha": 64.9})
        self.assertEqual((unknown.low_f, unknown.outdoor_regime, unknown.tie_mode), (65, "unknown", "heat"))
        self.assertEqual(unknown.outdoor_status, "unknown")
        self.assertTrue(unknown.emergencies["alpha"]["heat"]["measured"])

    def test_literal_emergency_trips_and_release_are_separate_from_comfort_target(self):
        self.cfg = replace(self.cfg, protection=ProtectionTemperatures(qualification_seconds=0))
        self.operating = OperatingState(self.cfg)
        at_floor = self.snapshot(0, temperatures={"alpha": 60, "beta": 80})
        self.assertNotIn("heat", at_floor.emergencies.get("alpha", {}))
        self.assertTrue(at_floor.emergencies["beta"]["cool"]["measured"])
        cold = self.snapshot(1, temperatures={"alpha": 59.9, "beta": 79.5})
        self.assertTrue(cold.emergencies["alpha"]["heat"]["measured"])
        self.assertTrue(cold.emergencies["beta"]["cool"]["recovering"])
        recovering = self.snapshot(2, temperatures={"alpha": 60.5, "beta": 79})
        self.assertTrue(recovering.emergencies["alpha"]["heat"]["recovering"])
        self.assertNotIn("cool", recovering.emergencies.get("beta", {}))
        released = self.snapshot(3, temperatures={"alpha": 61, "beta": 78.9})
        self.assertNotIn("heat", released.emergencies.get("alpha", {}))

    def test_already_cold_steady_room_qualifies_without_new_drift(self):
        self.snapshot(0)
        opening = self.snapshot(120)
        self.assertFalse(opening.window_effects["alpha"]["heat"]["active"])
        self.assertFalse(self.snapshot(419).window_effects["alpha"]["heat"]["active"])
        affected = self.snapshot(420)
        self.assertTrue(affected.window_effects["alpha"]["heat"]["active"])
        self.assertEqual(affected.window_effects["alpha"]["heat"]["comfort_priority_factor"], .25)
        self.assertNotIn("cool", affected.window_effects["alpha"])

    def test_invalid_contact_or_temperature_restores_baseline_and_confirmed_close_restores(self):
        self.snapshot(0)
        self.snapshot(120)
        self.snapshot(420)
        self.assertNotIn("alpha", self.snapshot(421, contact="unavailable").window_effects)
        self.snapshot(422)
        self.snapshot(542)
        self.assertTrue(self.snapshot(842).window_effects["alpha"]["heat"]["active"])
        self.assertNotIn("alpha", self.snapshot(843, temperatures={"alpha": None}).window_effects)
        self.snapshot(844)
        self.assertTrue(self.snapshot(1144).window_effects["alpha"]["heat"]["active"])
        self.assertTrue(self.snapshot(1145, contact="off").window_effects["alpha"]["heat"]["active"])
        self.assertNotIn("alpha", self.snapshot(1265, contact="off").window_effects)

    def test_hold_context_changes_reset_window_qualification(self):
        self.snapshot(0)
        self.snapshot(120)
        self.snapshot(420)
        held = replace(Settings.read({}, self.cfg), room_profiles={"alpha": self.cfg.zones["alpha"].profile})
        self.assertFalse(self.snapshot(421, settings=held).window_effects["alpha"]["heat"]["active"])
        self.assertTrue(self.snapshot(721, settings=held).window_effects["alpha"]["heat"]["active"])
        self.assertFalse(self.snapshot(722).window_effects["alpha"]["heat"]["active"])

    def test_deadlines_cover_contact_weather_and_impact_boundaries_without_raw_changes(self):
        self.assertEqual(self.snapshot(0).valid_until_monotonic, 120)
        self.assertEqual(self.snapshot(120).valid_until_monotonic, 300)
        self.assertEqual(self.snapshot(300).valid_until_monotonic, 420)
        final = self.snapshot(420)
        self.assertEqual(final.valid_until_monotonic, 2220)


if __name__ == "__main__":
    unittest.main()
