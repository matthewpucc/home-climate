"""The dwell timer reports earliest policy eligibility without changing selection."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.policy import Policy
from tests.test_rooms import NOW, configuration, states


class ModeHoldCountdownTests(unittest.TestCase):
    def initialize(self, *, hold, confirmation, interval=90, equal_priority=False):
        raw = configuration()
        if equal_priority:
            raw["rooms"]["gamma"]["schedules"][0]["priority"] = 1
        self.config = replace(Config.from_dict(raw), mode_hold_seconds=hold,
                              switch_confirmation_seconds=confirmation, decision_interval_seconds=interval,
                              forecast_minutes=0)
        self.policy = Policy(self.config)
        self.settings = Settings.read({}, self.config)

    def decide(self, seconds, *, alpha=73.2, gamma=68, cadence=None):
        now = NOW + timedelta(seconds=seconds)
        data = states(self.config, dict(alpha=alpha, beta=72, gamma=gamma), now)
        if cadence is not None:
            return cadence.decide(data, now, self.settings, monotonic_now=seconds)
        return self.policy.decide(data, now, self.settings)

    def test_old_direction_dwell_is_larger_than_parallel_confirmation(self):
        self.initialize(hold=300, confirmation=120)
        self.assertEqual(self.decide(0, alpha=72, gamma=66.9).mode, "heat")
        first = self.decide(30)
        self.assertEqual((first.mode, first.mode_hold_remaining_seconds), ("off", 270))
        self.assertEqual(first.as_dict()["mode_hold_remaining_seconds"], 270)
        self.assertEqual(self.decide(299).mode_hold_remaining_seconds, 1)
        selected = self.decide(300)
        self.assertEqual(selected.mode, "cool")
        self.assertIsNone(selected.mode_hold_remaining_seconds)

    def test_confirmation_is_larger_and_restarts_after_demand_drops(self):
        self.initialize(hold=60, confirmation=120)
        self.decide(0, alpha=72, gamma=66.9)
        self.assertEqual(self.decide(30).mode_hold_remaining_seconds, 120)
        self.assertEqual(self.decide(70).mode_hold_remaining_seconds, 80)
        self.assertIsNone(self.decide(80, alpha=71.5).mode_hold_remaining_seconds)
        self.assertEqual(self.decide(100).mode_hold_remaining_seconds, 120)
        self.assertEqual(self.decide(219).mode_hold_remaining_seconds, 1)
        self.assertIsNone(self.decide(220).mode_hold_remaining_seconds)

    def test_no_finite_eta_for_active_opposing_call_or_zero_time_gates(self):
        self.initialize(hold=300, confirmation=120, equal_priority=True)
        self.decide(0, alpha=72, gamma=66.9)
        for alpha in (73.1, 73.2):  # tie, then marginal score advantage only
            competing = self.decide(420, alpha=alpha, gamma=66.9)
            self.assertEqual(competing.mode, "heat")
            self.assertIsNone(competing.mode_hold_remaining_seconds)
        self.initialize(hold=0, confirmation=0)
        self.decide(0, alpha=72, gamma=66.9)
        selected = self.decide(30)
        self.assertEqual(selected.mode, "cool")
        self.assertIsNone(selected.mode_hold_remaining_seconds)

    def test_cadence_ages_snapshot_without_forcing_or_assuming_a_new_decision(self):
        self.initialize(hold=60, confirmation=45, interval=90)
        cadence = DecisionCadence(self.config)
        self.assertEqual(self.decide(0, alpha=72, gamma=66.9, cadence=cadence).mode, "heat")
        self.assertEqual(self.decide(90, cadence=cadence).mode_hold_remaining_seconds, 45)
        self.assertEqual(self.decide(115, cadence=cadence).mode_hold_remaining_seconds, 20)
        expired = self.decide(135, cadence=cadence)
        self.assertEqual(expired.mode, "off")
        self.assertIsNone(expired.mode_hold_remaining_seconds)
        self.assertEqual(cadence.cached_plan.mode_hold_remaining_seconds, 45)
        self.assertEqual(self.decide(180, cadence=cadence).mode, "cool")
