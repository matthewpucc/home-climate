"""Confirmed openings and measured comfort deficits have separate jobs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Probe, Settings
from house_climate.head_guard import head_constraint
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import NOW, configuration, states


CONTACT = "binary_sensor.fixture_shared_window"
OUTDOOR = "sensor.fixture_shared_outdoor"


class ConfirmedOpenArbitrationTests(unittest.TestCase):
    def setUp(self):
        raw = configuration()
        for room in raw["rooms"].values():
            room["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
            room["schedules"] = []
            room["fans"] = []
        config = Config.from_dict(raw)
        self.config = replace(config,
            protection=ProtectionTemperatures(qualification_seconds=0),
            outdoor_probes=(Probe(OUTDOOR, "fixture_outside"),),
            zones={**config.zones,
                   "alpha": replace(config.zones["alpha"], windows=(CONTACT,))},
            head_heat_call_offset_f=5, head_cool_call_offset_f=5,
            mode_hold_seconds=0, switch_confirmation_seconds=1)
        self.cadence = DecisionCadence(self.config)
        self.settings = Settings.read({}, self.config)
        self.contact_value = "on"
        self.contact_changed = NOW.isoformat()

    def tick(self, seconds, contact="on", *, outdoor=52, contact_attributes=None,
             **temperatures):
        now = NOW + timedelta(seconds=seconds)
        snapshot = states(self.config,
            {"alpha": 71.1, "beta": 75.6, "gamma": 70.6, **temperatures}, now)
        if contact != self.contact_value:
            self.contact_value = contact
            self.contact_changed = now.isoformat()
        snapshot[CONTACT] = {"state": contact, "last_changed": self.contact_changed,
                             "attributes": contact_attributes or {}}
        snapshot[OUTDOOR] = {"state": str(outdoor), "last_reported": now.isoformat(),
                             "attributes": {"unit_of_measurement": "°F"}}
        return self.cadence.decide(snapshot, now, self.settings, monotonic_now=seconds)

    def start_open_cooling(self):
        self.tick(0)
        self.tick(120)
        return self.tick(210)

    def test_confirmed_open_restores_cooling_candidate_before_measured_effect(self):
        initial = self.tick(0)
        self.assertEqual(initial.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")
        opened = self.tick(120)
        self.assertEqual(opened.window_states["alpha"]["state"], "open")
        self.assertEqual(opened.protection_context["window_effects"], {})
        self.assertEqual(opened.room_targets["alpha"]["effective_comfort_priority"]["heat"], 1)
        self.assertGreater(opened.mode_scores["cool"][2], 0)
        selected = self.tick(210)
        self.assertEqual(selected.mode, "cool")
        self.assertIn("shared", selected.head_targets_f)

    def test_raw_open_candidate_still_obeys_normal_comfort_rearm(self):
        for seconds in (0, 119):
            with self.subTest(seconds=seconds):
                plan = self.tick(seconds)
                self.assertEqual(plan.window_states["alpha"]["state"], "unknown")
                self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")

    def test_unknown_contact_withdraws_relief_on_maintenance_tick(self):
        self.assertIn("shared", self.start_open_cooling().head_targets_f)
        plan = self.tick(211, "unavailable")
        self.assertEqual(plan.window_states["alpha"]["state"], "unknown")
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")

    def test_confirmed_close_restores_normal_rearm_without_waiting_for_effect(self):
        self.assertIn("shared", self.start_open_cooling().head_targets_f)
        self.tick(211, "off")
        closed = self.tick(331, "off")
        self.assertEqual(closed.window_states["alpha"]["state"], "closed")
        self.assertNotIn("shared", closed.head_targets_f)
        self.assertEqual(closed.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")

    def test_confirmed_open_never_overrides_opposing_hard_temperature_guard(self):
        self.tick(0, alpha=59, beta=81)
        plan = self.tick(120, alpha=59, beta=81)
        self.assertEqual(plan.window_states["alpha"]["state"], "open")
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "house_limit")

    def test_approved_emergency_floor_is_dynamic_with_existing_stop_reserve(self):
        for outside, floor in ((52, 60), (20, 65)):
            for offset, blocked in ((1, True), (1.01, False)):
                with self.subTest(outside=outside, floor=floor, offset=offset):
                    self.setUp()
                    temperature = floor + offset
                    self.tick(0, outdoor=outside, alpha=temperature, beta=77, gamma=72)
                    plan = self.tick(120, outdoor=outside, alpha=temperature, beta=77, gamma=72)
                    self.assertEqual(plan.protection_context["low_f"], floor)
                    self.assertIn("alpha", plan.protection_context["confirmed_open_rooms"])
                    if blocked:
                        guard = plan.head_constraints["shared"]["cool"]
                        self.assertEqual((guard["kind"], guard["boundary_f"]), ("house_limit", floor + 1))
                    else:
                        self.assertNotIn("cool", plan.head_constraints.get("shared", {}))
                        self.assertGreater(plan.mode_scores["cool"][2], 0)

    def test_no_unapproved_66_or_68_degree_routine_floor_is_added(self):
        self.tick(0, alpha=65.5, beta=77, gamma=72)
        self.tick(120, alpha=65.5, beta=77, gamma=72)
        plan = self.tick(210, alpha=65.5, beta=77, gamma=72)
        self.assertEqual(plan.protection_context["low_f"], 60)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("shared", plan.head_targets_f)

    def test_raw_extremes_and_worsening_forecast_still_precede_window_relief(self):
        for outside, floor in ((52, 60), (20, 65)):
            with self.subTest(outside=outside, floor=floor):
                self.setUp()
                self.tick(0, outdoor=outside, alpha=68, beta=77, gamma=72)
                plan = self.tick(120, outdoor=outside, alpha=68, beta=77, gamma=72)
                for raw_low, slope in ((floor + .9, 0), (floor + 2, -.6)):
                    rooms = deepcopy(plan.rooms)
                    rooms["alpha"].update(temperature=68, low=raw_low, confirmed_low=68,
                                          slope_f_per_minute=slope)
                    guard = head_constraint(self.config, "shared", "cool", rooms,
                        plan.room_targets, rearm=True, context=plan.protection_context)
                    self.assertEqual((guard["kind"], guard["boundary_f"]), ("house_limit", floor + 1))
                rooms["alpha"].update(low=floor + 2, slope_f_per_minute=.6)
                self.assertIsNone(head_constraint(self.config, "shared", "cool", rooms,
                    plan.room_targets, rearm=True, context=plan.protection_context))

    def test_colder_outdoor_floor_withdraws_a_previously_eligible_cooling_call(self):
        self.tick(0, alpha=64, beta=77, gamma=72)
        self.tick(120, alpha=64, beta=77, gamma=72)
        warm = self.tick(210, alpha=64, beta=77, gamma=72)
        self.assertEqual(warm.mode, "cool")
        self.assertIn("shared", warm.head_targets_f)
        cold = self.tick(211, outdoor=20, alpha=64, beta=77, gamma=72)
        self.assertEqual(cold.protection_context["low_f"], 65)
        self.assertEqual(cold.head_constraints["shared"]["cool"]["kind"], "house_limit")
        self.assertFalse(cold.mode == "cool" and "shared" in cold.head_targets_f)

    def test_new_cooling_eligibility_does_not_bypass_ordinary_mode_hold(self):
        self.config = replace(self.config, mode_hold_seconds=1200, switch_confirmation_seconds=120)
        self.cadence = DecisionCadence(self.config)
        self.settings = Settings.read({}, self.config)
        initial = self.tick(0)
        self.assertEqual(initial.mode, "heat")
        opened = self.tick(120)
        self.assertGreater(opened.mode_scores["cool"][2], 0)
        self.assertEqual(opened.mode, "heat")
        self.assertEqual(self.tick(210).mode, "heat")
        selected = self.tick(1200)
        self.assertEqual(selected.mode, "cool")
        self.assertIn("shared", selected.head_targets_f)

    def test_confirmed_open_alone_does_not_add_symmetric_heating_relief(self):
        self.tick(0, alpha=73.4, beta=69.5, gamma=72)
        self.tick(120, alpha=73.4, beta=69.5, gamma=72)
        plan = self.tick(420, alpha=73.4, beta=69.5, gamma=72)
        self.assertEqual(plan.window_states["alpha"]["state"], "open")
        self.assertEqual(plan.protection_context["window_effects"], {})
        self.assertEqual(plan.head_constraints["shared"]["heat"]["kind"], "comfort_limit")
        self.assertFalse(plan.mode == "heat" and "shared" in plan.head_targets_f)

    def test_assumed_contact_withdraws_active_cooling_relief_immediately(self):
        self.assertIn("shared", self.start_open_cooling().head_targets_f)
        unknown = self.tick(211, contact_attributes={"assumed_state": True})
        self.assertEqual(unknown.window_states["alpha"]["state"], "unknown")
        self.assertEqual(unknown.protection_context["confirmed_open_rooms"], ())
        self.assertNotIn("shared", unknown.head_targets_f)
        self.assertEqual(unknown.head_constraints["shared"]["cool"]["kind"], "comfort_rearm")

    def test_assumed_contact_also_withdraws_qualified_demand_effect(self):
        self.tick(0, alpha=69.5, beta=74, gamma=72)
        self.tick(120, alpha=69.5, beta=74, gamma=72)
        affected = self.tick(420, alpha=69.5, beta=74, gamma=72)
        self.assertEqual(affected.room_targets["alpha"]["effective_comfort_priority"]["heat"], .25)
        unknown = self.tick(421, contact_attributes={"assumed_state": True},
                            alpha=69.5, beta=74, gamma=72)
        self.assertEqual(unknown.protection_context["window_effects"], {})
        self.assertEqual(unknown.room_targets["alpha"]["effective_comfort_priority"]["heat"], 1)
        self.assertEqual(unknown.protection_context["confirmed_open_rooms"], ())
        self.assertNotIn("shared", unknown.head_targets_f)


if __name__ == "__main__":
    unittest.main()
