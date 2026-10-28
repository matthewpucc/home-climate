"""Outdoor qualification uses continuous valid input, not a count of reports."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, OutdoorProbe, Probe, Settings
from house_climate.operating_state import OperatingState
from house_climate.policy import Policy
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import NOW, configuration, states as room_states


FIRST = "sensor.fixture_outdoor_first"
SECOND = "sensor.fixture_outdoor_second"
WEATHER = "weather.fixture_outdoor_fallback"
OBSERVATION = "sensor.fixture_station_observation_time"


class OutdoorContinuityTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Config.from_dict(configuration()),
                              protection=ProtectionTemperatures(),
                              outdoor_probes=(Probe(FIRST, "first"), Probe(SECOND, "second")))
        self.operating = OperatingState(self.config)

    def snapshot(self, tick, first=None, second=None, *, wall_tick=None):
        """Reports are (wall-clock report tick, temperature) or unavailable."""
        wall_tick = tick if wall_tick is None else wall_tick
        states = {}
        for probe, report in zip(self.config.outdoor_probes, (first, second), strict=True):
            entity = probe.entity_id
            if report is None:
                states[entity] = {"state": "unavailable"}
                continue
            report_tick, temperature = report
            weather = entity.startswith("weather.")
            states[entity] = {
                "state": "sunny" if weather else str(temperature),
                "last_reported": (NOW + timedelta(seconds=report_tick)).isoformat(),
                "attributes": ({"temperature": temperature, "temperature_unit": "°F"}
                               if weather else {"unit_of_measurement": "°F"}),
            }
        return self.operating.environment(states, NOW + timedelta(seconds=wall_tick),
                                          monotonic_now=tick)

    def assert_context(self, context, regime, floor):
        self.assertEqual((context.outdoor_regime, context.low_f, context.high_f),
                         (regime, floor, 80))

    def test_pending_candidate_cannot_bridge_expiry_hidden_by_polling_stall(self):
        # The first report expires at tick100. No poll observes the invalid gap.
        self.assert_context(self.snapshot(0, (-1700, 50)), "unqualified", 65)
        fresh = self.snapshot(300, (300, 50))
        self.assert_context(fresh, "unqualified", 65)
        self.assertEqual(fresh.valid_until_monotonic, 600)
        self.assert_context(self.snapshot(599, (300, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(600, (300, 50)), "warm", 60)

    def test_qualified_warm_regime_requalifies_after_unseen_validity_gap(self):
        self.snapshot(0, (0, 50))
        self.assert_context(self.snapshot(300, (0, 50)), "warm", 60)
        self.assert_context(self.snapshot(1900, (1900, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(2199, (1900, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(2200, (1900, 50)), "warm", 60)

    def test_qualified_cold_regime_keeps_protective_floor_during_requalification(self):
        self.snapshot(0, (0, 20))
        self.assert_context(self.snapshot(300, (0, 20)), "cold", 65)
        recovered = self.snapshot(1900, (1900, 22))
        self.assert_context(recovered, "unqualified", 65)
        self.assertEqual(recovered.tie_mode, "heat")
        self.assert_context(self.snapshot(2200, (1900, 22)), "cold", 65)

    def test_one_unchanged_valid_report_qualifies_without_new_observations(self):
        self.snapshot(0, (0, 50))
        self.assert_context(self.snapshot(299, (0, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(300, (0, 50)), "warm", 60)

    def test_report_renewal_before_expiry_preserves_pending_qualification(self):
        self.snapshot(0, (-1700, 50))
        # A report at90 covers the old expiry100, even if read only at200.
        renewed = self.snapshot(200, (90, 50))
        self.assert_context(renewed, "unqualified", 65)
        self.assertEqual(renewed.valid_until_monotonic, 300)
        self.assert_context(self.snapshot(300, (90, 50)), "warm", 60)

    def test_reports_touching_exactly_at_expiry_have_no_validity_gap(self):
        self.snapshot(0, (-1700, 50))
        self.assert_context(self.snapshot(300, (100, 50)), "warm", 60)

    def test_any_positive_gap_requires_fresh_qualification(self):
        self.snapshot(0, (-1700, 50))
        self.assert_context(self.snapshot(300, (100.000001, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(600, (100.000001, 50)), "warm", 60)

    def test_independent_source_coverage_survives_earliest_alias_expiry(self):
        initial = self.snapshot(0, (-1700, 50), (0, 51))
        self.assertEqual(initial.valid_until_monotonic, 100)
        self.assert_context(self.snapshot(300, (-1700, 50), (0, 51)), "warm", 60)

    def test_same_source_alias_coverage_does_not_reset_when_colder_alias_expires(self):
        self.config = replace(self.config, outdoor_probes=(
            Probe(FIRST, "one_station"), Probe(SECOND, "one_station")))
        self.operating = OperatingState(self.config)
        self.snapshot(0, (-1700, 50), (0, 51))
        self.assert_context(self.snapshot(300, (-1700, 50), (0, 51)), "warm", 60)

    def test_all_independent_sources_expiring_is_a_gap(self):
        self.snapshot(0, (-1700, 50), (-1600, 51))
        self.assert_context(self.snapshot(300, (300, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(600, (300, 50)), "warm", 60)

    def test_failed_longer_lived_source_cannot_keep_extending_old_coverage(self):
        self.snapshot(0, (-1700, 50), (0, 51))
        # The long-lived second source is now explicitly unavailable. Only
        # the first source's remaining validity through100 may be retained.
        self.snapshot(50, (-1700, 50))
        self.assert_context(self.snapshot(300, (300, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(600, (300, 50)), "warm", 60)

    def test_newly_seen_overlapping_source_can_preserve_coverage(self):
        self.snapshot(0, (-1700, 50))
        self.assert_context(self.snapshot(300, second=(90, 50)), "warm", 60)

    def test_observed_unknown_always_resets_even_if_later_timestamp_overlaps(self):
        self.snapshot(0, (0, 50))
        self.assert_context(self.snapshot(200), "unknown", 65)
        self.assert_context(self.snapshot(300, (190, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(600, (190, 50)), "warm", 60)

    def test_exact_expiry_without_new_report_immediately_restores_unknown_floor(self):
        self.snapshot(0, (0, 50))
        self.assert_context(self.snapshot(300, (0, 50)), "warm", 60)
        unknown = self.snapshot(1800, (0, 50))
        self.assert_context(unknown, "unknown", 65)
        self.assertEqual(unknown.tie_mode, "heat")
        self.assert_context(self.snapshot(1800, (1800, 50)), "unqualified", 65)
        self.assert_context(self.snapshot(2100, (1800, 50)), "warm", 60)

    def test_monotonic_regression_cannot_retain_previously_qualified_regime(self):
        self.snapshot(0, (0, 50))
        self.snapshot(300, (0, 50))
        self.assert_context(self.snapshot(200, (400, 50), wall_tick=400), "unqualified", 65)
        self.assert_context(self.snapshot(499, (400, 50), wall_tick=699), "unqualified", 65)
        self.assert_context(self.snapshot(500, (400, 50), wall_tick=700), "warm", 60)

    def test_wall_clock_regression_cannot_retain_previously_qualified_regime(self):
        self.snapshot(0, (0, 50))
        self.snapshot(300, (0, 50))
        self.assert_context(self.snapshot(400, (200, 50), wall_tick=200), "unqualified", 65)
        self.assert_context(self.snapshot(699, (200, 50), wall_tick=499), "unqualified", 65)
        self.assert_context(self.snapshot(700, (200, 50), wall_tick=500), "warm", 60)

    def test_explicit_zero_qualification_remains_immediate_after_recovery(self):
        self.config = replace(self.config, protection=ProtectionTemperatures(qualification_seconds=0))
        self.operating = OperatingState(self.config)
        self.assert_context(self.snapshot(0, (-1700, 50)), "warm", 60)
        self.assert_context(self.snapshot(300, (300, 50)), "warm", 60)

    def test_slow_drift_in_existing_cold_regime_does_not_restart_or_change_limits(self):
        self.snapshot(0, (0, 20))
        self.assert_context(self.snapshot(300, (0, 20)), "cold", 65)
        for tick, temperature in ((600, 25), (900, 30), (1200, 32), (1500, 33)):
            context = self.snapshot(tick, (tick, temperature))
            self.assert_context(context, "cold", 65)
            self.assertIsNone(context.outdoor_candidate)
            self.assertEqual(context.tie_mode, "heat")
        self.assert_context(self.snapshot(1600, (1600, 34)), "cold", 65)
        self.assert_context(self.snapshot(1899, (1600, 34)), "cold", 65)
        self.assert_context(self.snapshot(1900, (1600, 34)), "warm", 60)

    def primary_and_weather(self):
        self.config = replace(self.config, outdoor_probes=(
            OutdoorProbe(FIRST, "local", role="primary"),
            OutdoorProbe(WEATHER, "regional")))
        self.operating = OperatingState(self.config)

    def test_ignored_weather_expiry_does_not_shorten_primary_decision_or_requalify(self):
        self.primary_and_weather()
        # The fallback expires at100, but a valid primary is the accepted input.
        first = self.snapshot(0, (0, 50), (-5300, 20))
        self.assertEqual((first.outdoor_selected_role, first.valid_until_monotonic),
                         ("primary", 300))
        qualified = self.snapshot(300, (0, 50), (-5300, 20))
        self.assert_context(qualified, "warm", 60)
        self.assertEqual((qualified.outdoor_selected_role, qualified.valid_until_monotonic),
                         ("primary", 1800))

    def test_observed_fallback_with_overlapping_coverage_can_keep_qualification(self):
        self.primary_and_weather()
        self.snapshot(0, (-1700, 50), (-5100, 50))
        fallback = self.snapshot(200, (-1700, 50), (-5100, 50))
        self.assert_context(fallback, "unqualified", 65)
        self.assertEqual(fallback.outdoor_selected_role, "fallback")
        self.assert_context(self.snapshot(300, second=(200, 50)), "warm", 60)

    def test_missing_poll_does_not_invent_historical_fallback_selection(self):
        self.primary_and_weather()
        self.snapshot(0, (-1700, 50), (0, 20))
        # At the next poll a primary is valid again. Its coverage has a gap;
        # an ignored fallback cannot prove the temperature selected in the gap.
        self.assert_context(self.snapshot(300, (300, 50), (0, 20)), "unqualified", 65)

    def test_normal_weather_renewal_uses_weather_lifetime_without_resetting_regime(self):
        self.config = replace(self.config, outdoor_probes=(
            Probe(WEATHER, "regional"), Probe(SECOND, "other")))
        self.operating = OperatingState(self.config)
        self.snapshot(0, (0, 20))
        self.assert_context(self.snapshot(300, (0, 20)), "cold", 65)
        renewed = self.snapshot(6000, (3300, 22))
        self.assert_context(renewed, "cold", 65)
        self.assertEqual(renewed.valid_until_monotonic, 8700)

    def test_actual_observation_expiry_limits_report_continuity(self):
        self.config = replace(self.config, outdoor_probes=(
            OutdoorProbe(WEATHER, "regional", observation_time_entity=OBSERVATION),))
        self.operating = OperatingState(self.config)

        def snapshot(tick, report_tick, observation_tick):
            data = {
                WEATHER: {"state": "sunny", "attributes": {
                    "temperature": 50, "temperature_unit": "°F"},
                    "last_reported": (NOW + timedelta(seconds=report_tick)).isoformat()},
                OBSERVATION: {"state": (NOW + timedelta(seconds=observation_tick)).isoformat()},
            }
            return self.operating.environment(data, NOW + timedelta(seconds=tick), monotonic_now=tick)

        # HA report remains fresh, but the observation expires at100.
        self.assertEqual(snapshot(0, 0, -5300).valid_until_monotonic, 100)
        self.assert_context(snapshot(300, 300, 300), "unqualified", 65)
        self.assert_context(snapshot(600, 300, 300), "warm", 60)

    def test_slow_cold_drift_does_not_advance_cached_comfort_decision(self):
        config = replace(self.config, decision_interval_seconds=90)
        tick = [0]
        clock = lambda: tick[0]
        policy = Policy(config, clock=clock)
        policy.decide = Mock(wraps=policy.decide)
        cadence = DecisionCadence(config, policy, clock)
        settings = Settings.read({}, config)

        def evaluate(seconds, outdoor):
            tick[0] = seconds
            now = NOW + timedelta(seconds=seconds)
            data = room_states(config, {"alpha": 72, "beta": 72, "gamma": 72}, now)
            data[FIRST] = {"state": str(outdoor), "last_reported": now.isoformat(),
                           "attributes": {"unit_of_measurement": "°F"}}
            return cadence.decide(data, now, settings)

        evaluate(0, 29)
        qualified = evaluate(300, 29)
        self.assertEqual(qualified.protection_context["outdoor_regime"], "cold")
        self.assertEqual(policy.decide.call_count, 2)
        self.assertEqual(cadence.next_decision, 390)
        for seconds, outdoor in ((315, 29.1), (330, 29.2), (345, 29.3),
                                 (360, 29.4), (375, 29.5), (389, 29.6)):
            with self.subTest(seconds=seconds):
                maintained = evaluate(seconds, outdoor)
                self.assertEqual(policy.decide.call_count, 2)
                self.assertEqual(cadence.next_decision, 390)
                self.assertEqual(maintained.protection_context["low_f"], 65)
                self.assertEqual(maintained.protection_context["outdoor_regime"], "cold")
                self.assertAlmostEqual(maintained.protection_context["outdoor_temperature_f"], outdoor)
        evaluate(390, 29.7)
        self.assertEqual(policy.decide.call_count, 3)
        self.assertEqual(cadence.next_decision, 480)


if __name__ == "__main__":
    unittest.main()
