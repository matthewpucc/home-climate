"""An observe-only candidate cannot displace actual reserve arbitration."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Settings
from house_climate.policy import Policy, observe_reserve_split
from house_climate.sensors import Reading

from test_protection_arbitration import Clock, configuration, states


class ReserveSplitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = replace(configuration(),
                           auxiliary_room="north", heater_entity="climate.fixture_aux")
        self.policy = Policy(self.cfg, clock=self.clock)

    def snapshot(self, *, north=60.75, south=74):
        now = self.clock.now
        result = states(self.cfg, self.clock, {"north": north, "south": south},
                        modes={"north_head": "heat"})
        for entity in self.cfg.heads.values():
            result[entity]["integration_repair"] = {"epoch": 0, "held": False, "off_since": None}
        result[self.cfg.heater_entity] = {
            "state": "heat", "last_reported": now.isoformat(),
            "attributes": {"home_climate_auxiliary_blocked": False,
                           "home_climate_auxiliary_soc_grace_active": False,
                           "hvac_action": "heating", "hvac_modes": ["off", "heat"]},
        }
        result[self.cfg.entity("auxiliary_probe_safe", "binary_sensor")] = {"state": "on"}
        result[self.cfg.entity("auxiliary_authorization", "input_datetime")] = {
            "attributes": {"timestamp": now.timestamp()}}
        return result

    def decide(self, snapshot):
        return self.policy.decide(snapshot, self.clock.now, Settings.read({}, self.cfg))

    def test_low_margin_never_spends_preventive_reserve(self):
        snapshot = self.snapshot()
        plan = self.decide(snapshot)
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(set(plan.head_targets_f), {"north_head"})
        split = plan.reserve_split
        self.assertEqual(split["cooling_heads"], ["south_head"])
        self.assertEqual(split["shared_priority"], "retain_heat")
        self.assertEqual(split["independent_capacity"], "unproven")
        self.assertEqual(split["blocking_reason"], "insufficient_floor_margin")
        self.assertEqual(split["heating_floor_f"], 60)
        self.assertEqual(split["shared_heat_stop_f"], plan.head_targets_f["north_head"])
        self.assertNotEqual(split["shared_heat_stop_f"], split["heating_floor_f"])

    def test_never_marks_unavailable_or_non_independent_cooling(self):
        snapshot = self.snapshot()
        snapshot[self.cfg.heads["south_head"]]["attributes"]["hvac_modes"] = ["off", "heat"]
        self.assertEqual(self.decide(snapshot).reserve_split, {})
        self.assertEqual(self.decide(self.snapshot(south=72)).reserve_split, {})

    def test_prototype_never_changes_base_plan(self):
        reference = Policy(self.cfg, clock=self.clock)
        snapshot = self.snapshot()
        with patch("house_climate.policy.observe_reserve_split", return_value={}):
            original = reference.decide(snapshot, self.clock.now, Settings.read({}, self.cfg)).as_dict()
        observed = self.decide(snapshot).as_dict()
        self.assertTrue(observed.pop("reserve_split"))
        original.pop("reserve_split")
        self.assertEqual(original, observed)

    def test_limits_only_real_cooling_competes_but_protection_only_does_not(self):
        settings = Settings.read({}, self.cfg)
        settings.room_profiles = {
            "south": replace(self.cfg.zones["south"].profile, comfort_enabled=False)}
        plan = self.policy.decide(self.snapshot(), self.clock.now, settings)
        self.assertFalse(plan.room_targets["south"]["comfort_enabled"])
        self.assertTrue(plan.room_targets["south"]["cool_requested"])
        self.assertEqual(plan.reserve_split["cooling_heads"], ["south_head"])
        settings.protection_only_rooms = frozenset({"south"})
        safe_only = self.policy.decide(self.snapshot(), self.clock.now, settings)
        self.assertFalse(safe_only.reserve_split)

    def test_emergency_is_not_a_split_candidate(self):
        plan = self.decide(self.snapshot(north=59.5))
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(plan.head_protections["north_head"]["kind"], "emergency")
        self.assertFalse(plan.reserve_split)

    def test_manual_power_or_stale_readback_never_qualifies(self):
        for case, reason in (("manual", "auxiliary_manual"),
                             ("grace", "auxiliary_supply_or_probe_not_ready"),
                             ("power", "auxiliary_supply_or_probe_not_ready"),
                             ("stale_head", "shared_head_feedback_unqualified")):
            with self.subTest(case=case):
                snapshot = self.snapshot()
                attrs = snapshot[self.cfg.heater_entity]["attributes"]
                if case == "manual":
                    attrs["home_climate_manual"] = True
                elif case == "grace":
                    attrs["home_climate_auxiliary_soc_grace_active"] = True
                elif case == "power":
                    del attrs["home_climate_auxiliary_blocked"]
                else:
                    snapshot[self.cfg.heads["south_head"]]["last_reported"] = (
                        self.clock.now - timedelta(minutes=10)).isoformat()
                plan = self.decide(snapshot)
                self.assertEqual(plan.mode, "heat")
                self.assertEqual(plan.reserve_split["blocking_reason"], reason)

    def test_maintenance_retracts_cooling_candidate_before_next_comfort_choice(self):
        cadence = DecisionCadence(self.cfg, self.policy)
        snapshot = self.snapshot()
        settings = Settings.read({}, self.cfg)
        first = cadence.decide(snapshot, self.clock.now, settings)
        self.assertTrue(first.reserve_split)
        self.clock.tick = 15
        snapshot = self.snapshot(south=72)
        second = cadence.decide(snapshot, self.clock.now, settings)
        self.assertFalse(second.reserve_split)
        self.assertEqual(second.mode, "heat")

    def test_heating_report_and_improving_fused_trend_still_do_not_prove_capacity(self):
        snapshot = self.snapshot()
        plan = self.decide(snapshot)
        # A hypothetical large near-term margin isolates the capacity veto
        # from the normal colder forecast that first qualified the reserve.
        # In production only live qualified readings are passed to the preview.
        now = self.clock.now
        room_reports = {
            name: Reading(value, value, value, value, value, 0, 3,
                          observed_at=now.timestamp(),
                          observation_id=(("fixture", now.timestamp()),))
            for name, value in (("north", 62), ("south", 74))
        }
        plan.heater_on = True
        plan.auxiliary_policy.update(progress_known=True, progress_f=.25)
        preview = observe_reserve_split(self.policy, plan, snapshot, room_reports, now, ["south_head"])
        self.assertEqual(preview["blocking_reason"], "independent_capacity_not_proven")
        self.assertEqual(preview["fused_room_progress_f"], .25)
        self.assertEqual(plan.mode, "heat")
        heater = snapshot[self.cfg.heater_entity]
        heater["last_reported"] = (now - timedelta(minutes=3)).isoformat()
        preview = observe_reserve_split(self.policy, plan, snapshot, room_reports, now, ["south_head"])
        self.assertEqual(preview["blocking_reason"], "auxiliary_heating_not_observed_under_lease")


class RollingAttemptTests(unittest.TestCase):
    def setUp(self):
        from house_climate.reserve_split import RollingAttempt
        self.trial = RollingAttempt(acknowledgement_timeout=45, evidence_maximum_age=30, isolation_timeout=90)

    def tick(self, now, *, ack=None, reading=(10, 66.7), ready=True, competing=True, isolated=None):
        return self.trial.advance(now=now, competing=competing, ready=ready,
                                  heat_ack_at=ack, reading=reading, isolated_at=isolated)

    def test_needs_new_command_ack_and_observation_not_cached_heat(self):
        self.assertEqual(self.tick(10, ack=9), "offered")
        self.assertEqual(self.tick(20, ack=20), "offered")  # same reading
        self.assertEqual(self.tick(25, ack=20, reading=(19, 66.8)), "offered")
        self.assertEqual(self.tick(30, ack=20, reading=(25, 66.8)), "isolating")
        self.assertEqual(self.tick(35, ack=32, reading=(35, 66.8), isolated=36), "isolating")
        self.assertEqual(self.tick(40, ack=38, reading=(40, 66.8), isolated=36), "isolating")
        self.assertEqual(self.tick(45, ack=43, reading=(40, 66.8), isolated=36), "isolating")
        self.assertEqual(self.tick(50, ack=48, reading=(50, 66.8), isolated=36), "active")

    def test_loss_of_guard_decline_and_timeout_latch_until_conflict_clears(self):
        for failure in ("guard", "declining", "heat", "timeout", "missing"):
            with self.subTest(failure=failure):
                self.trial.reset()
                self.assertEqual(self.tick(10), "offered")
                if failure != "timeout":
                    self.assertEqual(self.tick(20, ack=15, reading=(20, 66.8)), "isolating")
                if failure == "guard":
                    result = self.tick(25, ack=15, reading=(25, 66.8), ready=False)
                elif failure == "declining":
                    result = self.tick(25, ack=15, reading=(25, 66.7))
                elif failure == "heat":
                    result = self.tick(25, reading=(25, 66.8))
                elif failure == "missing":
                    result = self.tick(25, ack=15, reading=None)
                else:
                    result = self.tick(55, ack=50, reading=(25, 66.8))
                self.assertEqual(result, "fallback")
                self.assertEqual(self.tick(60, ack=59, reading=(60, 67)), "fallback")
                self.assertEqual(self.tick(65, competing=False), "idle")
                self.assertEqual(self.tick(70, ack=69, reading=(70, 66.7)), "offered")

    def test_switch_confirmation_uses_sustained_demand_not_late_heat_ack(self):
        from house_climate.reserve_split import RollingAttempt
        self.trial = RollingAttempt(acknowledgement_timeout=25, evidence_maximum_age=30,
                                    isolation_timeout=60, confirmation=45)
        self.assertEqual(self.tick(10), "offered")
        self.assertEqual(self.tick(20, ack=18, reading=(20, 66.8)), "offered")
        self.assertEqual(self.tick(40, ack=38, reading=(40, 66.8)), "offered")
        self.assertEqual(self.tick(55, ack=53, reading=(55, 66.8)), "isolating")

    def test_bad_clock_and_conflicting_duplicate_veto_activation(self):
        self.assertEqual(self.tick(20), "offered")
        self.assertEqual(self.tick(30, ack=25, reading=(10, 66.8)), "fallback")
        self.trial.reset()
        self.assertEqual(self.tick(20), "offered")
        self.assertEqual(self.tick(15, ack=25, reading=(20, 66.8)), "fallback")


class GuardedControllerSplitTests(ReserveSplitTests):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, decision_interval_seconds=90, poll_seconds=15,
                           zones=configuration(priorities=(.1, 10)).zones,
                           command_timeout_seconds=30, off_settle_seconds=0, switch_confirmation_seconds=15,
                           head_minimum_off_seconds=0)
        self.policy = Policy(self.cfg, clock=self.clock)

    def test_missing_reserve_room_cannot_escape_via_ordinary_cooling(self):
        from house_climate.policy import Plan
        self.policy.reserve_attempt.stage = "active"
        plan = Plan(mode="cool", head_targets_f={"south_head": 72})
        self.policy._apply_reserve_split(plan, {}, {}, self.clock.now, 0,
                                         {"cool": {"south_head": 72}}, None, None, "°F")
        self.assertEqual(plan.mode, "off")
        self.assertFalse(plan.head_targets_f)
        self.assertTrue(plan.reserve_split_stop_cooling)
        self.assertEqual(plan.reserve_split.get("mode"), "fallback")

    def test_guarded_attempt_then_maintenance_loss_immediately_reselects_heat(self):
        from house_climate.sensors import Sensors
        cadence = DecisionCadence(self.cfg, self.policy, clock=self.clock)
        settings = Settings.read({}, self.cfg)

        def feed(_sensor_reader, *_args, **_kwargs):
            stamp = self.clock.now.timestamp()
            rooms = {
                room: Reading(value, value, value, value, value, slope, 3,
                              observed_at=stamp, observation_id=((room, stamp),))
                for room, value, slope in (("north", 62, -.11), ("south", 74, 0))
            }
            return rooms, []

        def tick(offset, *, proof=True, isolated_at=None, stop_revision=0):
            self.clock.tick = offset
            snapshot = self.snapshot(north=62)
            if stop_revision:
                attrs = snapshot[self.cfg.heater_entity]["attributes"]
                attrs["home_climate_auxiliary_stop_revision"] = stop_revision
                attrs["home_climate_auxiliary_blocked"] = True
            return cadence.decide(snapshot, self.clock.now, settings, monotonic_now=offset,
                reserve_proof_supported=True,
                reserve_proof=({"ready": True, "generation": 1, "auxiliary_generation": 1,
                    "heat_reported_at": self.clock.now.timestamp() - 1, "stop_revision": 0, "stop_epoch": 0, "repair_epochs": ()}
                    if proof else None), reserve_isolated_at=isolated_at)

        with patch.object(Sensors, "read", feed):
            first = tick(0, proof=False)
            self.assertEqual((first.reserve_split["mode"], first.mode), ("offered", "heat"))
            second = tick(15)
            self.assertEqual((second.reserve_split["mode"], second.mode), ("isolating", "off"))
            self.assertTrue(second.reserve_split_stop_shared)
            third = tick(30, isolated_at=20)
            self.assertEqual((third.reserve_split["mode"], third.mode), ("isolating", "off"))
            fourth = tick(45, isolated_at=20)
            self.assertEqual((fourth.reserve_split["mode"], fourth.mode), ("active", "cool"))
            self.assertEqual(fourth.reserve_split_ticket["heads"], (self.cfg.heads["south_head"],))
            self.assertNotIn("reserve_split_ticket", fourth.as_dict())
            fault = tick(60, proof=False, stop_revision=1)  # Inside the standard 90s interval
            self.assertEqual((fault.reserve_split["mode"], fault.mode), ("fallback", "heat"))
            self.assertIsNone(fault.reserve_split_ticket)
            self.assertTrue(fault.reserve_split_stop_cooling)
            self.assertFalse(fault.heater_on)
            retry = tick(75)
            self.assertEqual((retry.reserve_split["mode"], retry.mode), ("fallback", "heat"))


class ReserveSplitExecutorTests(unittest.TestCase):
    def setUp(self):
        import time
        from house_climate.executor import Executor
        from test_controller import FakeHA

        self.clock = Clock()
        self.cfg = replace(configuration(), auxiliary_room="north", heater_entity="climate.fixture_aux")
        self.states = states(self.cfg, self.clock, {"north": 62, "south": 74})
        self.ha = FakeHA(self.states)
        self.ha.service_reserve_guarded = self.guarded
        self.ha.reserve_split_proof = lambda: self.proof
        self.ha.set_reserve_split_authorization = self.set_ticket
        self.proof = {"ready": True, "generation": 1, "auxiliary_generation": 2, "stop_revision": 3, "stop_epoch": 0, "repair_epochs": ()}
        self.tickets = []
        self.guarded_calls = []
        self.now = time.monotonic()
        self.executor = Executor(self.cfg, self.ha, "°F")

    def set_ticket(self, ticket):
        self.tickets.append(ticket)

    def guarded(self, domain, service, *, ticket, **data):
        self.guarded_calls.append((domain, service, ticket, data))
        return self.ha.service(domain, service, **data)

    def plan(self, *, isolating=False):
        from house_climate.policy import Plan
        head = self.cfg.heads["south_head"]
        ticket = {"heads": () if isolating else (head,),
                  "generation": 1, "auxiliary_generation": 2, "stop_revision": 3, "stop_epoch": 0, "repair_epochs": (),
                  "attempt_id": self.now - 45, "expires_at_monotonic": self.now + 15}
        return Plan(mode="off" if isolating else "cool", heater_on=True,
                    head_targets_f={} if isolating else {"south_head": 72},
                    reserve_split_ticket=ticket, reserve_split_stop_shared=isolating)

    def test_same_tick_authorization_veto_prevents_isolation_or_cooling(self):
        for isolating in (False, True):
            with self.subTest(isolating=isolating):
                plan = self.plan(isolating=isolating)
                self.executor.reserve_revoked_attempt = None
                self.executor.phase, self.executor.direction = ("running", "heat")
                plan.heater_on = False  # Native authorize_auxiliary_plan rejected the request.
                with patch.object(self.executor, "begin_stop") as stop:
                    self.executor.shared(plan, self.states, self.now)
                    stop.assert_not_called()  # Shared heat stays on pending ordinary arbitration.
                self.assertEqual(self.executor.reserve_revoked_attempt, plan.reserve_split_ticket["attempt_id"])
                self.assertEqual(self.tickets[-1], None)
        self.assertFalse(self.ha.calls)

    def test_same_tick_cooling_guard_loss_stops_even_inside_minimum_run(self):
        plan = self.plan()
        self.executor.phase, self.executor.direction = ("running", "cool")
        self.proof = None
        with patch.object(self.executor, "begin_stop") as stop:
            self.executor.shared(plan, self.states, self.now)
            stop.assert_called_once()
        self.assertFalse(self.guarded_calls)
        self.assertIsNone(self.tickets[-1])

    def test_split_never_falls_back_to_unguarded_cooling(self):
        from house_climate.ha import HAActivationDeferred
        head = self.cfg.heads["south_head"]
        self.executor._diagnostic_plan = self.plan()
        self.ha.service_reserve_guarded = None
        with self.assertRaises(HAActivationDeferred):
            self.executor.ensure(head, "set_temperature", {"hvac_mode": "cool", "temperature": 68},
                                 lambda _: False, self.states, self.now)
        self.assertFalse(self.ha.calls)
        self.assertNotIn(head, self.executor.pending)
