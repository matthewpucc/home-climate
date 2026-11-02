"""Approved supplementary policy uses real observation progression, not polls."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from house_climate.auxiliary_dispatch import AuxiliaryDispatch
from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.policy import Plan, Policy
from tests.test_controller import FakeHA, NIGHT, config, fixture


PEAK = datetime(2026, 11, 3, 2, tzinfo=timezone.utc)  # 18:00 PST


def reading(temperature, at, source="ambient"):
    return SimpleNamespace(temperature=temperature, observed_at=at.timestamp(),
                           observation_id=((source, at.timestamp(), temperature),))


class AuxiliaryDispatchTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.dispatch = AuxiliaryDispatch(self.config)
        self.band = (69, 73, 71)

    def tick(self, seconds, temperature=68, *, stamp=None, **kwargs):
        now = PEAK + timedelta(seconds=seconds)
        observed = now if stamp is None else PEAK + timedelta(seconds=stamp)
        return self.dispatch.decide(reading(temperature, observed), now, self.band, **kwargs)

    def test_peak_sustained_one_degree_deficit_needs_qualified_progress(self):
        for seconds in (0, 300, 600, 899):
            self.assertFalse(self.tick(seconds).requested)
        decision = self.tick(900)
        self.assertTrue(decision.requested)
        self.assertTrue(decision.progress_known)
        self.assertEqual(decision.progress_f, 0)
        self.assertEqual(decision.stop_target_f, 69.5)

    def test_cached_poll_does_not_advance_deficit_or_progress(self):
        self.tick(0)
        decision = self.tick(900, stamp=0)
        self.assertFalse(decision.requested)
        self.assertFalse(decision.progress_known)
        self.assertEqual(decision.outside_observed_seconds, 0)

    def test_unknown_history_does_not_mean_no_recovery(self):
        item = SimpleNamespace(temperature=68, observed_at=None, observation_id=())
        decision = self.dispatch.decide(item, PEAK, self.band)
        self.assertFalse(decision.requested)
        self.assertIn("unknown", decision.reason)
        maintained = self.dispatch.decide(item, PEAK, self.band, allow_start=False)
        self.assertIn("unknown", maintained.reason)

    def test_maintenance_can_remove_but_cannot_create_an_auxiliary_request(self):
        self.assertFalse(self.tick(0, 67, allow_start=False).requested)
        self.assertTrue(self.tick(15, 67).requested)
        self.assertFalse(self.tick(30, 69.5, allow_start=False).requested)

    def test_two_degree_peak_deficit_does_not_wait_for_history(self):
        decision = self.tick(0, temperature=67)
        self.assertTrue(decision.requested)
        self.assertFalse(decision.progress_known)

    def test_meaningful_recovery_prevents_peak_assistance(self):
        self.band = (70, 74, 72)
        for seconds, temp in ((0, 68.5), (300, 68.5), (600, 68.8), (900, 69.0)):
            decision = self.tick(seconds, temp)
        self.assertFalse(decision.requested)
        self.assertAlmostEqual(decision.progress_f, .5)
        self.assertIn("meaningful recovery", decision.reason)

    def test_peak_stop_inside_band_and_no_immediate_restart(self):
        self.assertTrue(self.tick(0, 67).requested)
        self.assertTrue(self.tick(15, 69.4).requested)
        self.assertFalse(self.tick(30, 69.5).requested)
        self.assertFalse(self.tick(45, 68.9).requested)

    def test_source_change_invalidates_accumulated_progress(self):
        self.tick(0)
        self.tick(600)
        now = PEAK + timedelta(seconds=900)
        result = self.dispatch.decide(reading(68, now, source="replacement"), now, self.band)
        self.assertFalse(result.requested)
        self.assertFalse(result.progress_known)

    def test_sparse_old_endpoint_is_not_presented_as_ten_minute_progress(self):
        self.tick(0)
        result = self.tick(900)
        self.assertFalse(result.progress_known)
        self.assertFalse(result.requested)

    def test_off_peak_has_no_one_degree_or_fifteen_minute_gate(self):
        now = PEAK + timedelta(hours=4)
        result = self.dispatch.decide(reading(68.9, now), now, self.band,
                                     heat_pump_selected=False)
        self.assertTrue(result.requested)
        self.assertFalse(result.peak)
        self.assertEqual(result.stop_target_f, 71)

    def test_off_peak_prefers_selected_heat_pump_but_can_help_cooling_queue(self):
        now = PEAK + timedelta(hours=4)
        observed = reading(68.9, now)
        self.assertFalse(self.dispatch.decide(observed, now, self.band,
                                             heat_pump_selected=True).requested)
        self.assertTrue(self.dispatch.decide(observed, now, self.band,
                                            heat_pump_selected=True, cooling_waiting=True).requested)

    def test_manual_or_missing_supply_never_requests_heat(self):
        self.assertFalse(self.tick(0, 65, manual=True).requested)
        self.assertFalse(self.tick(15, 65, available=False).requested)


class LivingParticipationTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.settings = Settings.read({}, self.config)

    def test_available_auxiliary_does_not_replace_living_heat_pump(self):
        result = Policy(self.config).decide(fixture(self.config, living=67, entertainment=70), NIGHT, self.settings)
        self.assertEqual(result.mode, "heat")
        self.assertIn("living", result.head_targets_f)
        self.assertFalse(result.heater_on)

    def test_auxiliary_can_heat_while_shared_system_cools(self):
        result = Policy(self.config).decide(fixture(self.config, living=67, office=76), NIGHT, self.settings)
        self.assertEqual(result.mode, "cool")
        self.assertTrue(result.heater_on)

    def test_auxiliary_manual_takeover_keeps_heat_pump_eligible(self):
        states = fixture(self.config, living=67)
        states[self.config.heater_entity]["attributes"]["home_climate_manual"] = True
        result = Policy(self.config).decide(states, NIGHT, self.settings)
        self.assertIn("living", result.head_targets_f)
        self.assertFalse(result.heater_on)
        self.assertFalse(result.auxiliary_fan_only)

    def test_circulation_inhibit_is_distinct_from_a_heat_only_inhibit(self):
        states = fixture(self.config, living=73)
        attrs = states[self.config.heater_entity]["attributes"]
        attrs["home_climate_auxiliary_blocked"] = True
        settings = replace(self.settings, toggles={**self.settings.toggles, "auxiliary_fan_enabled": True})
        self.assertTrue(Policy(self.config).decide(states, NIGHT, settings).auxiliary_fan_only)
        attrs["home_climate_auxiliary_fan_blocked"] = True
        self.assertFalse(Policy(self.config).decide(states, NIGHT, settings).auxiliary_fan_only)

    def test_manual_heater_receives_no_routine_executor_or_shutdown_command(self):
        states = fixture(self.config)
        states[self.config.heater_entity]["state"] = "heat"
        states[self.config.heater_entity]["attributes"]["home_climate_manual"] = True
        ha = FakeHA(states)
        executor = Executor(self.config, ha, "°F")
        executor.auxiliary(Plan(), ha.states(), 0)
        executor.climate(self.config.heater_entity, "heat", 80, ha.states(), 1, heater=True)
        executor.shutdown(ha.states())
        self.assertFalse(any(data["entity_id"] == self.config.heater_entity for _, _, data in ha.calls))

    def test_independent_stop_revision_invalidates_cached_heat_without_a_hot_worker_sample(self):
        from house_climate.cadence import DecisionCadence

        states = fixture(self.config, living=67.2, guest=74.8)
        cadence = DecisionCadence(replace(self.config, decision_interval_seconds=90))
        first = cadence.decide(states, NIGHT, self.settings, monotonic_now=0)
        self.assertTrue(first.heater_on)
        states[self.config.heater_entity]["attributes"]["home_climate_auxiliary_stop_revision"] = 1
        stopped = cadence.decide(states, NIGHT + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assertFalse(stopped.heater_on)
        self.assertIn("Independent auxiliary stop", stopped.auxiliary_policy["reason"])
        self.assertFalse(cadence.decide(states, NIGHT + timedelta(seconds=30),
                                       self.settings, monotonic_now=30).heater_on)
        self.assertTrue(cadence.decide(states, NIGHT + timedelta(seconds=90),
                                      self.settings, monotonic_now=90).heater_on)


class SharedOffAttemptTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), off_settle_seconds=60)
        self.entity = self.config.heads["living"]
        states = fixture(self.config)
        states[self.entity]["state"] = "heat"
        self.ha = FakeHA(states, ignore={self.entity})
        self.executor = Executor(self.config, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "heat"
        self.plan = Plan(mode="cool", head_targets_f={"living": 70})

    def count(self):
        return len([c for c in self.ha.calls if c[2]["entity_id"] == self.entity])

    def tick(self, seconds):
        self.executor.step(self.plan, self.ha.states(), seconds)

    def test_three_initial_attempts_and_45_second_failure_then_separate_cleanup(self):
        for seconds, expected in ((0, 1), (14, 1), (15, 2), (29, 2), (30, 3), (44, 3), (45, 3), (59, 3)):
            self.tick(seconds)
            self.assertEqual(self.count(), expected, seconds)
        status = self.executor.recovery_status(59)["shared_stop"][self.entity]
        self.assertTrue(status["failed"])
        self.assertEqual(status["attempts"], 3)
        self.assertEqual(self.executor.phase, "stopping")

        self.tick(60)
        self.assertEqual(self.count(), 4)

    def test_fresh_off_after_second_attempt_stops_retrying(self):
        self.tick(0)
        self.tick(15)
        self.ha.data[self.entity]["state"] = "off"
        self.ha.data[self.entity]["last_reported"] = (NIGHT + timedelta(seconds=20)).isoformat()
        self.tick(20)
        self.tick(30)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.executor.phase, "settling")

    def test_unavailable_shared_head_has_an_immediate_visible_issue(self):
        self.ha.data[self.entity]["state"] = "unavailable"
        self.tick(0)
        self.assertIn(self.entity, self.executor.device_issues)
        self.assertIn("unavailable", self.executor.device_issues[self.entity])
        self.assertEqual(self.executor.phase, "stopping")
        self.assertFalse(self.executor.recovery_status(0)["shared_stop"][self.entity]["failed"])

    def test_late_scheduler_ticks_do_not_compress_retry_spacing_or_extend_deadline(self):
        self.tick(0)
        self.tick(40)
        self.tick(40)
        self.tick(44)
        self.assertEqual(self.count(), 2)
        self.tick(45)
        self.assertEqual(self.count(), 2)
        status = self.executor.recovery_status(45)["shared_stop"][self.entity]
        self.assertTrue(status["failed"])
        self.assertIn("2 attempts", status["reason"])
        self.assertEqual(self.executor.phase, "stopping")



class SeasonalPlannerTests(unittest.TestCase):
    def test_seasonal_status_is_reported_without_an_auxiliary_device(self):
        from house_climate.cadence import DecisionCadence
        from house_climate.config import Config, SeasonalPolicy
        from tests.test_rooms import configuration, states

        cfg = replace(Config.from_dict(configuration()), seasonal_policy=SeasonalPolicy(enabled=True))
        settings = replace(Settings.read({}, cfg), seasonal_profile="warm")
        cadence = DecisionCadence(cfg)
        first = cadence.decide(states(cfg, now=PEAK), PEAK, settings, monotonic_now=0)
        self.assertEqual(first.seasonal["profile"], "warm")
        self.assertTrue(first.seasonal["degraded"])
        # Missing outdoor evidence preserves the saved profile and still
        # publishes its reason on maintenance ticks without any heater.
        later = cadence.decide(states(cfg, now=PEAK + timedelta(seconds=1)),
                               PEAK + timedelta(seconds=1), settings, monotonic_now=1)
        self.assertEqual(later.seasonal["profile"], "warm")
        self.assertIn("retaining", later.seasonal["reason"])
