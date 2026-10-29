"""Qualify sustained wall-power evidence without actuators or Home Assistant."""
from dataclasses import FrozenInstanceError
import math
import unittest

from house_climate.wall_power import PowerReport, WallPowerBudget


class WallPowerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.policy = WallPowerBudget()

    def frame(self, now, *, input_w=1600, circuit_w=100, budget=1476,
              input_at=None, circuit_at=None, input_source="sensor.fixture_input",
              circuit_source="sensor.fixture_circuit"):
        return self.policy.observe(now, budget, {
            "input": PowerReport(input_source, input_w, now if input_at is None else input_at),
            "circuit": PowerReport(circuit_source, circuit_w, now if circuit_at is None else circuit_at),
        })

    def test_requires_three_distinct_reports_and_full_span(self):
        for now in (1000, 1015):
            outcome = self.frame(now)
            self.assertEqual(outcome.pending_sources, ("input",))
            self.assertEqual(outcome.qualified_sources, ())
        outcome = self.frame(1030)
        self.assertEqual(outcome.qualified_sources, ("input",))
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 3)
        self.assertEqual(outcome.as_dict()["sources"]["input"]["evidence_seconds"], 30)

    def test_selected_input_role_does_not_require_circuit_reports(self):
        policy = WallPowerBudget(roles=("input",))
        for now in (1000, 1015, 1030):
            outcome = policy.observe(now, 1476, {
                "input": PowerReport("sensor.fixture_input", 1600, now)})
        self.assertEqual(outcome.qualified_sources, ("input",))
        self.assertEqual(outcome.unavailable_sources, ())
        self.assertEqual(tuple(outcome.as_dict()["sources"]), ("input",))

    def test_selected_input_role_cannot_use_or_invalidate_an_unselected_circuit(self):
        policy = WallPowerBudget(roles=("input",))
        with self.assertRaises(ValueError):
            policy.observe(1000, 1476, {"circuit": PowerReport("sensor.fixture_circuit", 5000, 1000)})
        with self.assertRaises(ValueError):
            policy.invalidate("circuit", 1000)
        for now in (1000, 1015, 1030):
            outcome = policy.observe(now, 1476, {"input": None})
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.unavailable_sources, ("input",))

    def test_role_selection_rejects_empty_unknown_and_duplicate_roles(self):
        for roles in ((), ("other",), ("input", "input"), "input", None):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                WallPowerBudget(roles=roles)

    def test_elapsed_polling_time_cannot_replace_report_span(self):
        for now in (1000, 1005, 1010):
            self.frame(now)
        before = self.frame(1010).as_dict()
        later = self.frame(1050, input_at=1010, circuit_at=1010)
        self.assertEqual(later.qualified_sources, ())
        self.assertEqual(later.as_dict(), before)
        self.assertEqual(self.frame(1051).qualified_sources, ("input",))

    def test_two_reports_over_long_span_are_still_not_three(self):
        self.frame(1000)
        self.assertEqual(self.frame(1030).qualified_sources, ())
        self.assertEqual(self.frame(1031).qualified_sources, ("input",))

    def test_exact_budget_is_high_and_just_below_resets(self):
        for now in (1000, 1015, 1030):
            outcome = self.frame(now, input_w=1476)
        self.assertEqual(outcome.qualified_sources, ("input",))
        self.assertEqual(self.frame(1031, input_w=1475.999).pending_sources, ())
        self.assertEqual(self.frame(1032).qualified_sources, ())

    def test_input_and_circuit_never_combine_reports(self):
        self.frame(1000, input_w=1600, circuit_w=100)
        self.frame(1015, input_w=100, circuit_w=1600)
        outcome = self.frame(1030, input_w=1600, circuit_w=100)
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)
        self.assertEqual(self.frame(1045, input_w=100, circuit_w=1600).qualified_sources, ())

    def test_both_sources_can_qualify_independently(self):
        for now in (1000, 1015, 1030):
            outcome = self.frame(now, circuit_w=1700)
        self.assertEqual(outcome.qualified_sources, ("input", "circuit"))

    def test_qualified_source_is_not_hidden_by_other_missing_source(self):
        for now in (1000, 1015, 1030):
            outcome = self.policy.observe(now, 1476, {
                "input": PowerReport("sensor.fixture_input", 1600, now), "circuit": None})
        self.assertEqual(outcome.qualified_sources, ("input",))
        self.assertEqual(outcome.unavailable_sources, ("circuit",))

    def test_voltage_only_updates_do_not_add_reports(self):
        self.frame(1000)
        for now, budget in ((1010, 1480), (1020, 1460), (1030, 1490)):
            outcome = self.frame(now, budget=budget, input_at=1000, circuit_at=1000)
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)

    def test_voltage_jitter_keeps_a_continuously_high_streak(self):
        self.frame(1000, budget=1476)
        self.frame(1015, budget=1480)
        outcome = self.frame(1030, budget=1470)
        self.assertEqual(outcome.qualified_sources, ("input",))

    def test_raised_budget_invalidates_earlier_lower_power(self):
        self.frame(1000, input_w=1480, budget=1470)
        self.frame(1015, input_w=1600, budget=1470)
        outcome = self.frame(1030, input_w=1600, budget=1500)
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)
        self.assertEqual(self.frame(1045, budget=1500).qualified_sources, ())
        self.assertEqual(self.frame(1060, budget=1500).qualified_sources, ("input",))

    def test_raised_budget_does_not_recount_cached_latest_report(self):
        self.frame(1000, input_w=1480, budget=1470)
        self.frame(1015, budget=1470)
        outcome = self.frame(1030, budget=1500, input_at=1015, circuit_at=1015)
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 0)
        self.assertEqual(outcome.qualified_sources, ())

    def test_lowered_budget_never_backdates_previously_normal_reports(self):
        for now in (1000, 1015, 1030):
            self.frame(now, input_w=1450, budget=1476)
        outcome = self.frame(1040, input_w=1450, budget=1440,
                             input_at=1030, circuit_at=1030)
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 0)
        for now in (1041, 1056):
            self.assertEqual(self.frame(now, input_w=1450, budget=1440).qualified_sources, ())
        self.assertEqual(self.frame(1071, input_w=1450, budget=1440).qualified_sources, ("input",))

    def test_source_identity_change_starts_new_streak(self):
        self.frame(1000)
        self.frame(1015)
        outcome = self.frame(1030, input_source="sensor.replacement_input")
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)

    def test_known_invalid_event_blocks_coalesced_cached_report(self):
        self.frame(1000)
        self.frame(1015)
        self.policy.invalidate("input", 1020)
        outcome = self.frame(1030, input_at=1019)
        self.assertIn("input", outcome.unavailable_sources)
        for now in (1031, 1046):
            self.assertEqual(self.frame(now).qualified_sources, ())
        self.assertEqual(self.frame(1061).qualified_sources, ("input",))

    def test_invalidation_barrier_is_strict_and_is_not_bypassed_by_source_change(self):
        self.frame(1000)
        self.policy.invalidate("input", 1010)
        outcome = self.frame(1015, input_at=1010, input_source="sensor.replacement_input")
        self.assertIn("input", outcome.unavailable_sources)
        self.assertEqual(outcome.qualified_sources, ())

    def test_missing_source_resets_only_that_source(self):
        self.frame(1000, circuit_w=1700)
        self.frame(1015, circuit_w=1700)
        outcome = self.policy.observe(1020, 1476, {
            "input": None, "circuit": PowerReport("sensor.fixture_circuit", 1700, 1015)})
        self.assertEqual(outcome.unavailable_sources, ("input",))
        outcome = self.frame(1030, circuit_w=1700)
        self.assertEqual(outcome.qualified_sources, ("circuit",))
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)

    def test_missing_voltage_resets_both_and_does_not_use_nominal_fallback(self):
        self.frame(1000, circuit_w=1700)
        self.frame(1015, circuit_w=1700)
        outcome = self.frame(1020, budget=None, circuit_w=1700)
        self.assertEqual(outcome.unavailable_sources, ("input", "circuit"))
        outcome = self.frame(1030, circuit_w=1700, input_at=1015, circuit_at=1015)
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.unavailable_sources, ("input", "circuit"))

    def test_invalid_budget_values_fail_closed(self):
        for budget in (None, 0, -1, math.nan, math.inf, True, "1476"):
            with self.subTest(budget=budget):
                self.policy = WallPowerBudget()
                self.frame(1000)
                self.assertEqual(self.frame(1015, budget=budget).unavailable_sources,
                                 ("input", "circuit"))

    def test_invalid_source_numbers_reset_qualification(self):
        for field, bad in (("watts", math.nan), ("watts", math.inf), ("watts", -1),
                           ("watts", True), ("watts", 10 ** 4000),
                           ("reported_at", math.nan), ("reported_at", 10 ** 4000),
                           ("reported_at", math.inf), ("reported_at", True),
                           ("source", ""), ("source", None)):
            with self.subTest(field=field, bad=bad):
                self.policy = WallPowerBudget()
                self.frame(1000)
                self.frame(1015)
                raw = {"source": "sensor.fixture_input", "watts": 1600, "reported_at": 1030}
                raw[field] = bad
                outcome = self.policy.observe(1030, 1476, {
                    "input": PowerReport(**raw),
                    "circuit": PowerReport("sensor.fixture_circuit", 100, 1030)})
                self.assertIn("input", outcome.unavailable_sources)
                self.assertEqual(outcome.qualified_sources, ())

    def test_future_report_is_not_qualification_evidence(self):
        self.frame(1000)
        self.frame(1015)
        outcome = self.frame(1030, input_at=1030.001)
        self.assertIn("input", outcome.unavailable_sources)
        self.assertEqual(self.frame(1031).as_dict()["sources"]["input"]["report_count"], 1)

    def test_out_of_order_and_same_timestamp_changed_power_reset(self):
        for report, watts in ((1009, 1600), (1015, 1601)):
            with self.subTest(report=report, watts=watts):
                self.policy = WallPowerBudget()
                self.frame(1000)
                self.frame(1015)
                outcome = self.frame(1030, input_w=watts, input_at=report)
                self.assertIn("input", outcome.unavailable_sources)
                self.assertEqual(outcome.qualified_sources, ())

    def test_expired_source_and_hidden_report_gap_require_new_streak(self):
        for stale_poll in (False, True):
            with self.subTest(stale_poll=stale_poll):
                self.policy = WallPowerBudget(max_age_seconds=40)
                self.frame(1000)
                self.frame(1015)
                if stale_poll:
                    outcome = self.frame(1056, input_at=1015)
                    self.assertIn("input", outcome.unavailable_sources)
                    now = 1057
                else:
                    now = 1056
                outcome = self.frame(now)
                self.assertEqual(outcome.qualified_sources, ())
                self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)

    def test_maximum_age_boundary_is_inclusive(self):
        self.policy = WallPowerBudget(max_age_seconds=40)
        self.frame(1000)
        self.frame(1015)
        self.frame(1030)
        self.assertEqual(self.frame(1070, input_at=1030).qualified_sources, ("input",))
        self.assertIn("input", self.frame(1070.001, input_at=1030).unavailable_sources)

    def test_qualification_does_not_expire_while_distinct_reports_continue(self):
        self.policy = WallPowerBudget(max_age_seconds=40)
        for now in range(1000, 1301, 15):
            outcome = self.frame(now)
        self.assertEqual(outcome.qualified_sources, ("input",))
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 3)
        self.assertEqual(outcome.as_dict()["sources"]["input"]["evidence_seconds"], 300)

    def test_clock_rollback_cannot_inherit_old_qualified_duration(self):
        for now in (1000, 1015, 1030):
            self.frame(now)
        self.assertEqual(self.frame(1005).qualified_sources, ())
        self.assertEqual(self.frame(1031).as_dict()["sources"]["input"]["report_count"], 1)

    def test_invalid_observation_clock_clears_all_sources(self):
        for bad in (math.nan, math.inf, True, "1030"):
            with self.subTest(now=bad):
                self.policy = WallPowerBudget()
                self.frame(1000, circuit_w=1700)
                self.frame(1015, circuit_w=1700)
                outcome = self.policy.observe(bad, 1476, {})
                self.assertEqual(outcome.qualified_sources, ())
                self.assertEqual(outcome.unavailable_sources, ("input", "circuit"))

    def test_restart_never_restores_qualification(self):
        for now in (1000, 1015, 1030):
            self.frame(now)
        self.policy = WallPowerBudget()
        self.assertEqual(self.frame(1045).qualified_sources, ())
        self.assertEqual(self.frame(1045).as_dict()["sources"]["input"]["report_count"], 1)

    def test_cached_first_report_cannot_backdate_a_new_runtime_hold(self):
        self.frame(1100, input_at=1000)
        self.frame(1101)
        outcome = self.frame(1102)
        self.assertEqual(outcome.qualified_sources, ())
        # Waiting on cached state alone cannot finish an observation span.
        outcome = self.frame(1130, input_at=1102, circuit_at=1102)
        self.assertEqual(outcome.qualified_sources, ())
        self.assertEqual(outcome.as_dict()["sources"]["input"]["evidence_seconds"], 2)
        self.assertEqual(self.frame(1131).qualified_sources, ("input",))

    def test_snapshot_is_immutable_and_has_no_unbounded_history(self):
        report = PowerReport("sensor.fixture_input", 1600, 1000)
        with self.assertRaises(FrozenInstanceError):
            report.watts = 0
        outcome = self.frame(1000)
        with self.assertRaises(FrozenInstanceError):
            outcome.budget_w = 0
        copy = outcome.as_dict()
        copy["sources"]["input"]["report_count"] = 999
        self.assertEqual(outcome.as_dict()["sources"]["input"]["report_count"], 1)

    def test_configuration_and_role_validation(self):
        for options in ({"max_age_seconds": 0}, {"hold_seconds": -1},
                        {"minimum_reports": 2}, {"minimum_reports": True},
                        {"minimum_reports": 3.5}, {"max_age_seconds": math.inf}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                WallPowerBudget(**options)
        with self.assertRaises(ValueError):
            self.policy.observe(1000, 1476, {"other": None})
        with self.assertRaises(ValueError):
            self.policy.invalidate("other", 1000)


if __name__ == "__main__":
    unittest.main()
