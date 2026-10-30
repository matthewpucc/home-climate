"""A cooling decision's window proof survives the entire dispatch pipeline."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Probe, Settings
from house_climate.executor import Executor
from house_climate.ha import HAActivationDeferred
from house_climate.protection import ProtectionTemperatures
from house_climate.window_authorization import check_window_authorization
from tests.test_controller import FakeHA
from tests.test_rooms import NOW, configuration, states


CONTACT = "binary_sensor.fixture_pipeline_window"
OUTDOOR = "sensor.fixture_pipeline_outside"


class GuardedFakeHA(FakeHA):
    """Separate dispatch routes, checking the ticket at the actual send boundary."""

    def __init__(self, snapshot, config, settings, clock):
        super().__init__(snapshot)
        self.config, self.settings, self.clock = config, settings, clock
        self.ordinary_calls = []
        self.guarded_attempts = []
        self.guarded_calls = []
        self.defer = False

    def service(self, domain, service, **data):
        self.ordinary_calls.append((domain, service, data))
        return super().service(domain, service, **data)

    def service_guarded(self, domain, service, *, ticket, **data):
        self.guarded_attempts.append((domain, service, ticket, data))
        tick = self.clock()
        result = check_window_authorization(ticket, self.config, data["entity_id"],
            data["hvac_mode"], self.states(), NOW + timedelta(seconds=tick), self.settings,
            monotonic_now=tick)
        if self.defer or not result:
            raise HAActivationDeferred(result.reason if not result else "Adapter deferred")
        self.guarded_calls.append((domain, service, ticket, data))
        # Deliberately bypass service(): an ordinary fallback would be visible.
        return FakeHA.service(self, domain, service, **data)


class WindowDispatchPipelineTests(unittest.TestCase):
    def setUp(self):
        raw = configuration()
        for room in raw["rooms"].values():
            room["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
            room["schedules"], room["fans"] = [], []
        config = Config.from_dict(raw)
        self.config = replace(config,
            protection=ProtectionTemperatures(qualification_seconds=0),
            outdoor_probes=(Probe(OUTDOOR, "fixture_outside"),),
            zones={**config.zones,
                   "alpha": replace(config.zones["alpha"], windows=(CONTACT,))},
            head_heat_call_offset_f=5, head_cool_call_offset_f=5,
            head_minimum_run_seconds=0, head_minimum_off_seconds=0,
            off_settle_seconds=.01, mode_hold_seconds=0, switch_confirmation_seconds=1)
        self.settings = Settings.read({}, self.config)
        self.cadence = DecisionCadence(self.config)
        self.seconds = 0
        self.contact, self.contact_changed = "on", NOW.isoformat()
        self.ha = None
        self.snapshot = None
        self.entity = self.config.heads["shared"]

    def tick(self, seconds, *, contact="on", outdoor=52, **temperatures):
        self.seconds = seconds
        now = NOW + timedelta(seconds=seconds)
        snapshot = states(self.config,
            {"alpha": 71.1, "beta": 75.6, "gamma": 72, **temperatures}, now)
        if self.ha is not None:
            for entity in self.config.heads.values():
                snapshot[entity] = deepcopy(self.ha.data[entity])
        if contact != self.contact:
            self.contact, self.contact_changed = contact, now.isoformat()
        snapshot[CONTACT] = {"state": contact, "last_changed": self.contact_changed,
                             "attributes": {}}
        snapshot[OUTDOOR] = {"state": str(outdoor), "last_reported": now.isoformat(),
                             "attributes": {"unit_of_measurement": "°F"}}
        self.snapshot = snapshot
        if self.ha is not None:
            self.ha.data = deepcopy(snapshot)
        return self.cadence.decide(snapshot, now, self.settings, monotonic_now=seconds)

    def open_cooling(self, **temperatures):
        self.tick(0, **temperatures)
        self.tick(120, **temperatures)
        plan = self.tick(210, **temperatures)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("shared", plan.head_targets_f)
        return plan

    def executor(self, *, guarded=True):
        self.ha = (GuardedFakeHA(self.snapshot, self.config, self.settings,
                                lambda: self.seconds) if guarded else FakeHA(self.snapshot))
        executor = Executor(self.config, self.ha, "°F")
        # All heads are already reconciled Off; focus on the running dispatch path.
        executor.phase, executor.direction = "running", "cool"
        return executor

    def step(self, executor, plan, seconds=None):
        if seconds is not None:
            self.seconds = seconds
        executor.step(plan, self.ha.states(), self.seconds)

    @staticmethod
    def positives(calls):
        return [call for call in calls if call[1] == "set_temperature"
                and call[-1].get("hvac_mode") in {"heat", "cool"}]

    def test_final_cadence_snapshot_ticket_is_the_actual_dispatch_ticket(self):
        plan = self.open_cooling()
        ticket = plan.window_authorizations["shared"]
        self.assertEqual(ticket.issued_at_monotonic, self.seconds)
        self.assertLessEqual(ticket.expires_at_monotonic, self.cadence.next_decision)
        executor = self.executor()
        self.step(executor, plan)
        self.assertEqual(len(self.ha.guarded_calls), 1)
        self.assertIs(self.ha.guarded_calls[0][2], ticket)
        self.assertEqual(self.ha.data[self.entity]["state"], "cool")
        self.assertFalse(self.positives(self.ha.ordinary_calls))

    def test_missing_adapter_defers_without_starting_an_acknowledgement_budget(self):
        plan = self.open_cooling()
        executor = self.executor(guarded=False)
        for seconds in (210, 211, 212):
            self.step(executor, plan, seconds)
            self.assertNotIn(self.entity, executor.pending)
        self.assertFalse(self.positives(self.ha.calls))
        self.assertEqual(self.ha.data[self.entity]["state"], "off")
        self.assertNotIn(self.entity, executor.device_issues)

    def test_missing_ticket_never_falls_back_to_the_ordinary_adapter(self):
        plan = self.open_cooling()
        plan.window_authorizations.clear()
        executor = self.executor()
        self.step(executor, plan)
        self.assertFalse(self.ha.guarded_attempts)
        self.assertFalse(self.positives(self.ha.ordinary_calls))
        self.assertNotIn(self.entity, executor.pending)

    def test_measured_effect_does_not_bypass_the_dispatch_proof(self):
        self.tick(0, alpha=69.5, beta=74)
        self.tick(120, alpha=69.5, beta=74)
        plan = self.tick(420, alpha=69.5, beta=74)
        self.assertEqual(plan.room_targets["alpha"]["effective_comfort_priority"]["heat"], .25)
        self.assertIn("shared", plan.head_targets_f)
        self.assertIn("shared", plan.window_authorizations)
        executor = self.executor(guarded=False)
        self.step(executor, plan)
        self.assertFalse(self.positives(self.ha.calls))
        self.assertNotIn(self.entity, executor.pending)

    def test_deferred_replacement_preserves_the_actual_outstanding_command_budget(self):
        plan = self.open_cooling()
        executor = self.executor()
        self.ha.ignore.add(self.entity)
        self.step(executor, plan)
        original = executor.pending[self.entity]
        self.assertEqual(original.activation_since, 210)
        self.ha.data[self.entity]["attributes"]["min_temp"] = 68
        self.ha.defer = True
        self.step(executor, plan, 211)
        self.assertEqual(len(self.ha.guarded_attempts), 2)
        self.assertIs(executor.pending[self.entity], original)
        self.assertEqual(executor.pending[self.entity].activation_since, 210)
        self.assertEqual(len(self.ha.guarded_calls), 1)
        self.assertFalse(self.positives(self.ha.ordinary_calls))

    def test_ordinary_cooling_still_uses_the_ordinary_adapter(self):
        self.tick(0, contact="off", alpha=74)
        self.tick(120, contact="off", alpha=74)
        plan = self.tick(210, contact="off", alpha=74)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("shared", plan.head_targets_f)
        self.assertFalse(plan.window_authorizations)
        executor = self.executor(guarded=False)
        self.step(executor, plan)
        self.assertTrue(self.positives(self.ha.calls))
        self.assertEqual(self.ha.data[self.entity]["state"], "cool")

    def test_off_dispatch_needs_no_positive_ticket_or_guarded_adapter(self):
        plan = self.open_cooling()
        executor = self.executor()
        self.step(executor, plan)
        self.step(executor, plan, 211)
        self.ha.calls.clear()
        self.ha.ordinary_calls.clear()
        off = self.tick(300, alpha=72, beta=72)
        self.assertEqual(off.mode, "off")
        self.assertFalse(off.window_authorizations)
        self.step(executor, off)
        self.assertEqual(self.ha.data[self.entity]["state"], "off")
        self.assertTrue(any(call[1] == "set_hvac_mode" and call[-1].get("hvac_mode") == "off"
                            for call in self.ha.ordinary_calls))
        self.assertEqual(len(self.ha.guarded_calls), 1)

    def test_retained_off_plan_cannot_mint_a_ticket_for_a_metadata_forced_resend(self):
        self.config = replace(self.config, head_minimum_run_seconds=300)
        self.settings = Settings.read({}, self.config)
        self.cadence = DecisionCadence(self.config)
        plan = self.open_cooling()
        executor = self.executor()
        self.step(executor, plan)
        self.step(executor, plan, 211)
        self.assertEqual(executor.head_cycles[self.entity].run_since, 211)
        self.assertEqual(self.ha.data[self.entity]["attributes"]["temperature"], 67)
        # The actual producer drops the demand; no ticket is present to inherit.
        off = self.tick(300, beta=72)
        self.assertEqual(off.mode, "off")
        self.assertEqual(off.head_targets_f, {})
        self.assertEqual(off.window_authorizations, {})
        self.assertEqual(off.window_authorization_heads, [])
        # Retention still accepts the observed old target, but fresh metadata
        # would turn that retained call into a new positive 68°F command.
        self.ha.data[self.entity]["attributes"]["min_temp"] = 68
        previous_calls = len(self.ha.calls)
        previous_attempts = len(self.ha.guarded_attempts)
        self.step(executor, off)
        self.assertEqual(off.mode, "cool")
        self.assertIn("shared", off.head_targets_f)
        self.assertEqual(off.head_call_targets_f["shared"], 68)
        self.assertIn(self.entity, executor.cycle_holds)
        self.assertEqual(off.window_authorizations, {})
        self.assertEqual(len(self.ha.guarded_attempts), previous_attempts)
        self.assertFalse(self.positives(self.ha.calls[previous_calls:]))
        self.assertEqual(self.ha.data[self.entity]["attributes"]["temperature"], 67)
        self.assertNotIn(self.entity, executor.pending)

    def test_changed_cadence_withdraws_eligibility_and_rejects_the_older_queued_start(self):
        for change in ("unknown", "closed", "floor", "freezing"):
            with self.subTest(change=change):
                self.setUp()
                original = self.open_cooling(alpha=64 if change == "freezing" else 71.1)
                executor = self.executor()
                if change == "unknown":
                    current = self.tick(211, contact="unavailable")
                elif change == "closed":
                    self.tick(211, contact="off")
                    current = self.tick(331, contact="off")
                elif change == "floor":
                    current = self.tick(211, alpha=60.5)
                else:
                    current = self.tick(211, outdoor=20, alpha=64)
                self.assertFalse(current.mode == "cool" and "shared" in current.head_targets_f)
                self.assertNotIn("shared", current.window_authorizations)
                # A queued old plan must be checked against that new snapshot.
                self.step(executor, original)
                self.assertFalse(self.ha.guarded_calls)
                self.assertFalse(self.positives(self.ha.ordinary_calls))
                self.assertNotIn(self.entity, executor.pending)

    def test_same_value_reports_after_a_gap_cannot_renew_an_old_dispatch_ticket(self):
        original = self.open_cooling()
        ticket = original.window_authorizations["shared"]
        executor = self.executor()
        expiry = ticket.expires_at_monotonic
        later = self.tick(expiry + self.config.sensor_stale_seconds + 1)
        self.assertIn("shared", later.head_targets_f)
        self.assertIsNot(later.window_authorizations["shared"], ticket)
        self.assertEqual(ticket.expires_at_monotonic, expiry)
        self.step(executor, original)
        self.assertEqual(len(self.ha.guarded_attempts), 1)
        self.assertFalse(self.ha.guarded_calls)
        self.assertFalse(self.positives(self.ha.ordinary_calls))
        self.assertNotIn(self.entity, executor.pending)


if __name__ == "__main__":
    unittest.main()
