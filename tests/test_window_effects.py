"""Open-window comfort yields without discarding attached-room protection."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Probe, Profile, Settings
from house_climate.policy import Policy
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import NOW, configuration, states

CONTACT = "binary_sensor.fixture_window_effect"
OUTDOOR = "sensor.fixture_window_outdoor"


class WindowEffectTests(unittest.TestCase):
    def setup(self, *, beta_comfort=True):
        raw = configuration()
        for name in raw["rooms"]:
            raw["rooms"][name]["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
            raw["rooms"][name]["schedules"] = []
            raw["rooms"][name]["fans"] = []
        raw["rooms"]["beta"]["profile"]["comfort_enabled"] = beta_comfort
        cfg = Config.from_dict(raw)
        self.cfg = replace(cfg, protection=ProtectionTemperatures(qualification_seconds=0),
                           outdoor_probes=(Probe(OUTDOOR, "fixture_weather"),),
                           zones={**cfg.zones, "alpha": replace(cfg.zones["alpha"], windows=(CONTACT,))},
                           head_heat_call_offset_f=5, head_cool_call_offset_f=5,
                           mode_hold_seconds=0, switch_confirmation_seconds=1)
        self.cadence = DecisionCadence(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        self.raw_contact = "on"
        self.contact_changed = NOW.isoformat()

    def tick(self, seconds, *, contact="on", settings=None, **temperatures):
        now = NOW + timedelta(seconds=seconds)
        data = states(self.cfg, {"alpha": 69.5, "beta": 74, "gamma": 72, **temperatures}, now)
        if contact != self.raw_contact:
            self.raw_contact, self.contact_changed = contact, now.isoformat()
        data[CONTACT] = {"state": contact, "last_changed": self.contact_changed}
        data[OUTDOOR] = {"state": "70", "last_reported": now.isoformat(),
                         "attributes": {"unit_of_measurement": "°F"}}
        for name, value in temperatures.items():
            if value is None:
                for probe in self.cfg.zones[name].probes:
                    data[probe.entity_id]["state"] = "unavailable"
        return self.cadence.decide(data, now, settings or self.settings, monotonic_now=seconds)

    def affected(self, **temperatures):
        self.tick(0, **temperatures)
        self.tick(120, **temperatures)
        return self.tick(420, **temperatures)

    def test_steady_cold_open_room_yields_only_its_opposing_soft_boundary(self):
        self.setup()
        before = self.tick(0)
        self.assertEqual(before.mode, "off")
        self.assertEqual(before.head_constraints["shared"]["cool"]["kind"], "comfort_limit")
        self.tick(120)
        affected = self.tick(420)
        self.assertEqual(affected.mode, "cool")
        self.assertIn("shared", affected.head_targets_f)
        self.assertEqual(affected.room_targets["alpha"]["priority"], 1)
        self.assertEqual(affected.room_targets["alpha"]["effective_comfort_priority"], {"heat": .25, "cool": 1})
        self.assertEqual(affected.head_constraints["shared"]["heat"]["room"], "beta")

    def test_warm_open_room_yields_its_soft_ceiling_for_shared_heating(self):
        self.setup()
        plan = self.affected(alpha=74.5, beta=69.5)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("shared", plan.head_targets_f)
        self.assertEqual(plan.room_targets["alpha"]["effective_comfort_priority"], {"heat": 1, "cool": .25})

    def test_nonzero_window_priority_can_still_call_when_nothing_competes(self):
        self.setup(beta_comfort=False)
        plan = self.affected(beta=72)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("shared", plan.head_targets_f)
        self.assertEqual(plan.mode_scores["heat"][2], .25)

    def test_window_discount_changes_comfort_arbitration_without_changing_other_rooms(self):
        self.setup(beta_comfort=False)
        self.tick(0, beta=72, gamma=74)
        self.tick(120, beta=72, gamma=74)
        candidate = self.tick(420, beta=72, gamma=74)
        self.assertEqual(candidate.room_targets["gamma"]["effective_comfort_priority"]["cool"], 1)
        # An ordinary comfort direction change still waits its confirmation
        # and the next normal 90-second comfort decision.
        plan = self.tick(510, beta=72, gamma=74)
        self.assertEqual(plan.mode, "cool")
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertIn("second", plan.head_targets_f)

    def test_unknown_contact_and_invalid_temperature_restore_baseline_constraints(self):
        for failure in ("contact", "temperature"):
            with self.subTest(failure=failure):
                self.setup()
                self.affected()
                plan = self.tick(421, contact="unavailable") if failure == "contact" else self.tick(421, alpha=None)
                self.assertNotIn("alpha", plan.protection_context["window_effects"])
                self.assertEqual(plan.room_targets["alpha"]["effective_comfort_priority"]["heat"], 1)
                self.assertNotIn("shared", plan.head_targets_f)
                self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"],
                                 "comfort_limit" if failure == "contact" else "missing")

    def test_confirmed_close_restores_soft_constraints_after_120_seconds(self):
        self.setup()
        self.affected()
        closing = self.tick(421, contact="off")
        self.assertIn("shared", closing.head_targets_f)
        self.assertEqual(closing.decision_valid_until_monotonic, 541)
        closed = self.tick(541, contact="off")
        self.assertNotIn("shared", closed.head_targets_f)
        self.assertEqual(closed.head_constraints["shared"]["cool"]["kind"], "comfort_limit")

    def test_hold_and_target_changes_reset_effect_evidence(self):
        self.setup()
        self.affected()
        held = replace(self.settings, room_profiles={"alpha": self.cfg.zones["alpha"].profile})
        plan = self.tick(421, settings=held)
        self.assertFalse(plan.protection_context["window_effects"]["alpha"]["heat"]["active"])
        self.assertEqual(plan.room_targets["alpha"]["effective_comfort_priority"]["heat"], 1)
        # Resetting demand weighting does not discard independently confirmed
        # open-contact cooling relief.
        self.assertIn("shared", plan.head_targets_f)
        changed = replace(held, room_profiles={"alpha": Profile(target_f=70, half_band_f=1, priority=1)})
        plan = self.tick(422, settings=changed)
        self.assertNotIn("alpha", plan.protection_context["window_effects"])

    def test_window_does_not_reduce_protection_priority_or_bypass_raw_hard_conflict(self):
        self.setup()
        plan = self.affected(alpha=59, beta=81)
        self.assertTrue(plan.protection_context["window_effects"]["alpha"]["heat"]["active"])
        self.assertTrue(plan.protection_context["emergencies"]["alpha"]["heat"]["active"])
        self.assertTrue(plan.protection_context["emergencies"]["beta"]["cool"]["active"])
        self.assertEqual(plan.mode, "off")
        for mode in ("heat", "cool"):
            self.assertEqual(plan.head_constraints["shared"][mode]["kind"], "house_limit")
        self.assertTrue(any("not currently feasible" in warning for warning in plan.warnings))
        request = self.cadence.policy.demand("alpha", self.cadence.policy.sensors.read(
            states(self.cfg, {"alpha": 59}, NOW), NOW)[0]["alpha"], (71, 73, 72), 1, update=False)
        self.assertEqual(request["heat"]["score"][2], 1)

    def test_raw_cold_guard_remains_active_after_soft_floor_yields(self):
        self.setup()
        self.affected()
        plan = self.tick(421, alpha=61, beta=80)
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "house_limit")
        self.assertEqual(plan.protection_context["low_f"], 60)

    def test_unavailable_reserve_keeps_its_priority_even_at_exact_guard_boundary(self):
        self.setup(beta_comfort=False)
        plan = self.affected(beta=61, gamma=74)
        now = NOW + timedelta(seconds=420)
        data = states(self.cfg, {"alpha": 69.5, "beta": 61, "gamma": 74}, now)
        data[self.cfg.heads["shared"]]["attributes"]["hvac_modes"] = ["off", "cool"]
        rooms, _warnings = self.cadence.policy.sensors.read(data, now)
        request = self.cadence.policy.unavailable_request("shared", "heat", rooms, plan.room_targets, data)
        self.assertIsNotNone(request)
        self.assertGreater(request["score"][1], 0)
        self.assertEqual(request["score"][2], 1)


if __name__ == "__main__":
    unittest.main()
