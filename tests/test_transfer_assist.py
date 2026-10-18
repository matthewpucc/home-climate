"""Transfer fans distribute selected shared-head output before ambient gradients."""
import unittest
from datetime import timedelta
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence

from house_climate.config import Config, Settings
from house_climate.policy import Policy
from tests.test_rooms import NOW, configuration, states


class SharedHeadTransferTests(unittest.TestCase):
    def setup_policy(self, raw=None):
        raw = raw or configuration()
        self.cfg = Config.from_dict(raw)
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)

    def decide(self, temperatures=None, *, now=NOW, mutate=None):
        temperatures = temperatures or {"alpha": 74.1, "beta": 74.3, "gamma": 68}
        data = states(self.cfg, temperatures, now=now)
        if mutate:
            mutate(data)
        return self.policy.decide(data, now, self.settings)

    def fan(self, plan):
        return plan.fans["fan.test_airflow"]

    def with_third_shared_room(self, *, target=72):
        raw = configuration()
        raw["rooms"]["delta"] = {
            "head": "shared", "sensors": [{"entity_id": "sensor.test_g"}, {"entity_id": "sensor.test_h"}],
            "profile": {"target_f": target},
        }
        return raw

    def start_cadence(self):
        self.seconds = 0
        temperatures = {name: 72 for name in self.cfg.zones}
        temperatures.update(alpha=74.1, beta=74.3, gamma=68)
        self.data = states(self.cfg, temperatures)
        self.policy.decide = Mock(wraps=self.policy.decide)
        self.cadence = DecisionCadence(self.cfg, self.policy, clock=lambda: self.seconds)
        return self.maintain(0)

    def maintain(self, seconds, **temperatures):
        self.seconds = seconds
        now = NOW + timedelta(seconds=seconds)
        for room, temperature in temperatures.items():
            for probe in self.cfg.zones[room].probes:
                self.data[probe.entity_id]["state"] = str(temperature)
                self.data[probe.entity_id]["last_reported"] = now.isoformat()
        return self.cadence.decide(self.data, now, self.settings)

    def test_both_destination_fans_assist_cooling_despite_warmer_source(self):
        raw = configuration()
        raw["rooms"]["alpha"]["fans"].append({
            "entity_id": "fan.test_second_airflow", "strategy": "transfer",
            "source_room": "beta", "percentage": 70,
        })
        self.setup_policy(raw)
        plan = self.decide()
        self.assertEqual(plan.mode, "cool")
        self.assertIn("shared", plan.head_targets_f)
        self.assertEqual(self.fan(plan), 60)
        self.assertEqual(plan.fans["fan.test_second_airflow"], 70)
        self.assertTrue(all(self.policy.transfer_on.values()))

    def test_shared_head_heating_assists_despite_colder_source(self):
        self.setup_policy()
        plan = self.decide({"alpha": 70, "beta": 69.8, "gamma": 68})
        self.assertEqual(plan.mode, "heat")
        self.assertIn("shared", plan.head_targets_f)
        self.assertEqual(self.fan(plan), 60)

    def test_assistance_stops_at_destination_target_even_if_another_zone_calls(self):
        for destination, source, requester, mode in (
                (71.8, 71.9, 74, "cool"), (72.2, 72.1, 70, "heat"),
                (72, 72, 74, "cool"), (72, 72, 70, "heat")):
            with self.subTest(mode=mode, destination=destination):
                self.setup_policy(self.with_third_shared_room())
                plan = self.decide({"alpha": destination, "beta": source,
                                    "gamma": 68, "delta": requester})
                self.assertEqual(plan.mode, mode)
                self.assertIn("shared", plan.head_targets_f)
                self.assertEqual(self.fan(plan), 0)

    def test_missing_source_or_destination_temperature_blocks_assistance(self):
        for room in ("alpha", "beta"):
            with self.subTest(room=room):
                self.setup_policy()

                def unavailable(data):
                    for probe in self.cfg.zones[room].probes:
                        data[probe.entity_id]["state"] = "unavailable"

                plan = self.decide(mutate=unavailable)
                self.assertNotIn("shared", plan.head_targets_f)
                self.assertEqual(self.fan(plan), 0)

    def test_missing_third_attached_room_blocks_assistance(self):
        self.setup_policy(self.with_third_shared_room())

        def unavailable(data):
            for probe in self.cfg.zones["delta"].probes:
                data[probe.entity_id]["state"] = "unavailable"

        plan = self.decide({"alpha": 74.1, "beta": 74.3, "gamma": 68, "delta": 72},
                           mutate=unavailable)
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(self.fan(plan), 0)

    def test_unsupported_requested_mode_does_not_start_assistance(self):
        self.setup_policy()

        def heat_only(data):
            data[self.cfg.heads["shared"]]["attributes"]["hvac_modes"] = ["off", "heat"]

        plan = self.decide(mutate=heat_only)
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(self.fan(plan), 0)

    def test_opposite_limit_in_another_attached_room_blocks_cooling_assistance(self):
        self.setup_policy(self.with_third_shared_room())
        plan = self.decide({"alpha": 74.1, "beta": 74.3, "gamma": 68, "delta": 66.2})
        self.assertFalse(plan.mode == "cool" and "shared" in plan.head_targets_f)
        self.assertEqual(self.fan(plan), 0)

    def test_shared_mode_arbitration_finishes_before_assistance(self):
        self.setup_policy()
        plan = self.decide({"alpha": 74.1, "beta": 74.3, "gamma": 66.5})
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(set(plan.head_targets_f), {"second"})
        self.assertEqual(self.fan(plan), 0)

    def test_cross_head_transfer_retains_gradient_only_behavior(self):
        raw = configuration()
        raw["rooms"]["beta"]["head"] = "second"
        self.setup_policy(raw)
        self.assertEqual(self.fan(self.decide()), 0)
        self.assertEqual(self.fan(self.decide({"alpha": 74.1, "beta": 73, "gamma": 68})), 60)

    def test_useful_gradient_remains_available_without_a_selected_head(self):
        self.setup_policy()

        def heat_only(data):
            data[self.cfg.heads["shared"]]["attributes"]["hvac_modes"] = ["off", "heat"]

        plan = self.decide({"alpha": 73, "beta": 72, "gamma": 68}, mutate=heat_only)
        self.assertEqual(plan.mode, "off")
        self.assertEqual(self.fan(plan), 60)

    def test_assistance_preserves_transfer_hysteresis_when_gradient_becomes_useful(self):
        self.setup_policy()
        self.assertEqual(self.fan(self.decide()), 60)

        def heat_only(data):
            data[self.cfg.heads["shared"]]["attributes"]["hvac_modes"] = ["off", "heat"]

        for seconds, source, percentage in ((90, 73.7, 60), (180, 73.9, 0), (270, 73.7, 0)):
            plan = self.decide({"alpha": 74.1, "beta": source, "gamma": 68},
                               now=NOW + timedelta(seconds=seconds), mutate=heat_only)
            self.assertEqual(self.fan(plan), percentage)
            self.assertEqual(any(self.policy.transfer_on.values()), bool(percentage))

    def test_fast_head_inhibition_latches_assist_off_until_next_decision(self):
        self.setup_policy()
        first = self.start_cadence()
        self.assertEqual(self.fan(first), 60)
        self.assertEqual(first.fan_head_assists, {"fan.test_airflow": "shared"})
        modes = self.data[self.cfg.heads["shared"]]["attributes"]
        modes["hvac_modes"] = ["off", "heat"]
        stopped = self.maintain(15)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertEqual(self.fan(stopped), 0)
        self.assertEqual(stopped.fan_head_assists, {})
        self.assertFalse(any(self.policy.transfer_on.values()))
        modes["hvac_modes"] = ["off", "heat", "cool"]
        self.assertEqual(self.fan(self.maintain(30)), 0)
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertEqual(self.fan(self.maintain(90)), 60)

    def test_fast_head_inhibition_preserves_independently_useful_ambient_transfer(self):
        self.setup_policy()
        self.start_cadence()
        self.data[self.cfg.heads["shared"]]["attributes"]["hvac_modes"] = ["off", "heat"]
        retained = self.maintain(15, beta=73.7)
        self.assertNotIn("shared", retained.head_targets_f)
        self.assertEqual(self.fan(retained), 60)
        self.assertEqual(retained.fan_head_assists, {})
        self.assertEqual(self.policy.decide.call_count, 1)
        self.assertTrue(any(self.policy.transfer_on.values()))

    def test_fast_probe_guard_wins_over_useful_ambient_fallback(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                raw = configuration()
                raw["rooms"]["alpha"]["sensors"].append({"entity_id": "sensor.test_extra"})
                self.setup_policy(raw)
                self.start_cadence()
                if missing:
                    for probe in self.cfg.zones["beta"].probes:
                        self.data[probe.entity_id]["state"] = "unavailable"
                    stopped = self.maintain(15)
                else:
                    # A single raw cold source trips the hard guard even though
                    # median feedback and an ambient transfer remain useful.
                    self.data[self.cfg.zones["alpha"].probes[0].entity_id]["state"] = "66.2"
                    stopped = self.maintain(15, beta=73.7)
                self.assertEqual(self.fan(stopped), 0)
                self.assertEqual(stopped.fan_head_assists, {})
                self.assertTrue(any("fresh room temperature/probe check" in warning
                                    for warning in stopped.warnings))
                self.assertEqual(self.policy.decide.call_count, 1)

    def test_fast_loss_of_another_attached_zone_cancels_assistance(self):
        self.setup_policy(self.with_third_shared_room())
        first = self.start_cadence()
        self.assertIn("shared", first.head_targets_f)
        self.assertEqual(self.fan(first), 60)
        for probe in self.cfg.zones["delta"].probes:
            self.data[probe.entity_id]["state"] = "unavailable"
        stopped = self.maintain(15)
        self.assertNotIn("shared", stopped.head_targets_f)
        self.assertEqual(self.fan(stopped), 0)
        self.assertEqual(stopped.fan_head_assists, {})
        self.assertEqual(self.policy.decide.call_count, 1)



if __name__ == "__main__":
    unittest.main()
