"""Purifier base airflow survives thermal inactivity and independent failures."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Plan, Policy
from tests.test_controller import FakeHA
from tests.test_rooms import configuration, states


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
TRANSFER = "fan.test_airflow"
SCHEDULED = "fan.test_rest"
SECOND = "fan.test_second_purifier"


def purifier_config(*, minimum=20, auxiliary=False):
    raw = configuration()
    raw.update(head_minimum_run_seconds=0, head_minimum_off_seconds=0,
               decision_interval_seconds=90)
    for room in raw["rooms"].values():
        room["profile"] = {"target_f": 72, "half_band_f": 1, "comfort_enabled": False}
        for fan in room.get("fans", []):
            fan["minimum_percentage"] = minimum
    raw["rooms"]["beta"]["fans"] = [{
        "entity_id": SECOND, "strategy": "cooling", "percentage": 70,
        "minimum_percentage": minimum,
    }]
    if auxiliary:
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha",
                   heater_cutoff_f=70)
    return raw


class PurifierMinimumPolicyTests(unittest.TestCase):
    def setup_policy(self, raw=None):
        self.cfg = Config.from_dict(raw or purifier_config())
        self.settings = Settings.read({}, self.cfg)
        self.policy = Policy(self.cfg)

    def data(self, temperatures=None, now=NOW):
        return states(self.cfg, temperatures or {"alpha": 72, "beta": 72, "gamma": 72}, now)

    def decide(self, data=None, now=NOW):
        return self.policy.decide(self.data(now=now) if data is None else data,
                                  now, self.settings)

    def test_idle_policy_requests_minimum_for_each_purifier_without_climate_demand(self):
        self.setup_policy(purifier_config(auxiliary=True))
        plan = self.decide()
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_targets_f, {})
        self.assertEqual(plan.fans, {TRANSFER: 20, SECOND: 20, SCHEDULED: 20})
        self.assertFalse(plan.heater_on)
        self.assertFalse(plan.auxiliary_fan_only)
        ha = FakeHA(self.data())
        executor = Executor(self.cfg, ha, "°F")
        executor.step(plan, ha.states(), 0)
        executor.step(plan, ha.states(), 15)
        for fan in self.cfg.fans.values():
            self.assertEqual(ha.data[fan]["state"], "on")
            self.assertEqual(ha.data[fan]["attributes"]["percentage"], 20)
        self.assertEqual(ha.data[self.cfg.heater_entity]["state"], "off")
        self.assertFalse(any(call[0] == "climate" and
                             call[2].get("hvac_mode") in ("heat", "cool", "fan_only")
                             for call in ha.calls))

    def test_each_strategy_retains_its_minimum_when_not_thermally_active(self):
        for strategy in ("cooling", "conditioning", "schedule", "transfer", "always"):
            with self.subTest(strategy=strategy):
                raw = purifier_config()
                fan = raw["rooms"]["alpha"]["fans"][0]
                fan["strategy"] = strategy
                if strategy != "transfer":
                    fan.pop("source_room")
                if strategy == "always":
                    fan["percentage"] = 0
                self.setup_policy(raw)
                self.assertEqual(self.decide().fans[TRANSFER], 20)

    def test_missing_destination_or_transfer_source_keeps_minimum(self):
        for missing_room in ("alpha", "beta", "gamma"):
            with self.subTest(missing_room=missing_room):
                self.setup_policy()
                data = self.data()
                for probe in self.cfg.zones[missing_room].probes:
                    data[probe.entity_id]["state"] = "unavailable"
                plan = self.decide(data)
                self.assertEqual(plan.fans, {TRANSFER: 20, SECOND: 20, SCHEDULED: 20})

    def test_no_room_measurements_still_requests_base_purifier_airflow(self):
        self.setup_policy(purifier_config(auxiliary=True))
        data = self.data()
        for room in self.cfg.zones.values():
            for probe in room.probes:
                data.pop(probe.entity_id)
        plan = self.decide(data)
        self.assertEqual(plan.rooms, {})
        self.assertEqual(plan.fans, {TRANSFER: 20, SECOND: 20, SCHEDULED: 20})
        self.assertFalse(plan.heater_on)
        self.assertFalse(plan.auxiliary_fan_only)

    def test_schedule_boost_and_daytime_minimum_both_survive_boundaries(self):
        for hour, minute, expected in ((21, 59, 20), (22, 0, 60), (4, 59, 60), (5, 0, 20)):
            with self.subTest(hour=hour, minute=minute):
                self.setup_policy()
                now = NOW.replace(hour=hour, minute=minute)
                plan = self.decide(self.data({"alpha": 72, "beta": 72, "gamma": 68}, now), now)
                self.assertEqual(plan.fans[SCHEDULED], expected)

    def test_minimum_above_a_schedule_boost_is_still_respected(self):
        self.setup_policy(purifier_config(minimum=70))
        now = NOW.replace(hour=23)
        self.assertEqual(self.decide(self.data({"alpha": 72, "beta": 72, "gamma": 68}, now), now)
                         .fans[SCHEDULED], 70)

    def test_helpful_transfer_boost_survives_above_minimum(self):
        self.setup_policy()
        data = self.data({"alpha": 69, "beta": 73.5, "gamma": 72})
        plan = self.decide(data)
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.fans[TRANSFER], 60)

    def test_cooling_and_conditioning_boosts_remain_above_the_floor(self):
        for strategy in ("cooling", "conditioning"):
            with self.subTest(strategy=strategy):
                raw = purifier_config()
                fan = raw["rooms"]["alpha"]["fans"][0]
                fan.update(strategy=strategy, percentage=70)
                fan.pop("source_room")
                self.setup_policy(raw)
                self.assertEqual(self.decide(self.data({"alpha": 74, "beta": 72, "gamma": 72}))
                                 .fans[TRANSFER], 70)

    def test_opposing_conditioned_air_removes_boost_but_preserves_minimum(self):
        for destination, source, direction in ((69, 78, "cooling"), (73.1, 64, "heating")):
            with self.subTest(direction=direction):
                self.setup_policy()
                plan = self.decide(self.data({"alpha": destination, "beta": source, "gamma": 72}))
                self.assertEqual(plan.fans[TRANSFER], 20)
                self.assertNotIn(TRANSFER, plan.fan_head_assists)
                self.assertTrue(any(direction + " at its source" in item for item in plan.warnings))

    def test_generic_default_zero_preserves_idle_off_and_thermal_veto(self):
        raw = purifier_config()
        for room in raw["rooms"].values():
            for fan in room.get("fans", []):
                fan.pop("minimum_percentage")
        self.setup_policy(raw)
        self.assertTrue(all(fan.minimum_percentage == 0 for fan in self.cfg.fan_controls.values()))
        self.assertEqual(self.decide().fans, {TRANSFER: 0, SECOND: 0, SCHEDULED: 0})
        self.policy = Policy(self.cfg)
        self.assertEqual(self.decide(self.data({"alpha": 69, "beta": 78, "gamma": 72}))
                         .fans[TRANSFER], 0)

    def test_maintenance_probe_loss_keeps_same_floor_as_full_policy(self):
        self.setup_policy()
        self.policy.decide = Mock(wraps=self.policy.decide)
        cadence = DecisionCadence(self.cfg, self.policy, clock=lambda: 0)
        first = cadence.decide(self.data({"alpha": 69, "beta": 73.5, "gamma": 72}),
                               NOW, self.settings, monotonic_now=0)
        self.assertEqual(first.fans[TRANSFER], 60)
        data = self.data({"alpha": 69, "beta": 73.5, "gamma": 72}, NOW + timedelta(seconds=15))
        for probe in self.cfg.zones["beta"].probes:
            data[probe.entity_id]["state"] = "unavailable"
        maintained = cadence.decide(data, NOW + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assertEqual(maintained.fans[TRANSFER], 20)
        self.assertEqual(maintained.fans[SECOND], 20)
        self.assertEqual(self.policy.decide.call_count, 1)
        full = Policy(self.cfg).decide(data, NOW + timedelta(seconds=15), self.settings)
        self.assertEqual(maintained.fans, full.fans)

    def test_maintenance_conditioning_veto_keeps_purification_floor(self):
        self.setup_policy()
        self.policy.decide = Mock(wraps=self.policy.decide)
        cadence = DecisionCadence(self.cfg, self.policy, clock=lambda: 0)
        first = cadence.decide(self.data({"alpha": 69, "beta": 73.5, "gamma": 72}),
                               NOW, self.settings, monotonic_now=0)
        self.assertEqual(first.fans[TRANSFER], 60)
        now = NOW + timedelta(seconds=15)
        data = self.data({"alpha": 69, "beta": 73.5, "gamma": 72}, now)
        data[self.cfg.heads["shared"]].update(state="cool", last_reported=now.isoformat())
        maintained = cadence.decide(data, now, self.settings, monotonic_now=15)
        self.assertEqual(maintained.fans[TRANSFER], 20)
        self.assertEqual(self.policy.decide.call_count, 1)
        ha = FakeHA(data)
        executor = Executor(self.cfg, ha, "°F")
        executor.step(maintained, ha.states(), 15)
        self.assertEqual(ha.data[TRANSFER]["state"], "on")
        self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], 20)

    def test_unavailable_purifier_does_not_stop_another_purifier_or_shared_cooling(self):
        raw = purifier_config()
        raw["rooms"]["alpha"]["profile"]["comfort_enabled"] = True
        self.setup_policy(raw)
        data = self.data({"alpha": 74.1, "beta": 72, "gamma": 72})
        data[TRANSFER]["state"] = "unavailable"
        plan = self.decide(data)
        self.assertEqual(plan.mode, "cool")
        ha = FakeHA(data)
        executor = Executor(self.cfg, ha, "°F")
        for elapsed in (0, 15, 195, 210):
            executor.step(plan, ha.states(), elapsed)
        self.assertEqual(ha.data[SECOND]["state"], "on")
        self.assertEqual(ha.data[SECOND]["attributes"]["percentage"], 20)
        self.assertEqual(ha.data[self.cfg.heads["shared"]]["state"], "cool")
        self.assertTrue(any(TRANSFER in warning and "retrying independently" in warning
                            for warning in plan.warnings))


class PurifierMinimumCommandTests(unittest.TestCase):
    def test_idle_minimum_restarts_an_externally_switched_off_purifier(self):
        cfg = Config.from_dict(purifier_config())
        ha = FakeHA(states(cfg, now=NOW))
        executor = Executor(cfg, ha, "°F")
        plan = Plan(fans={TRANSFER: 20})
        executor.step(plan, ha.states(), 0)
        executor.step(plan, ha.states(), 15)
        ha.data[TRANSFER]["state"] = "off"
        ha.calls.clear()
        executor.step(plan, ha.states(), 30)
        self.assertEqual(ha.data[TRANSFER]["state"], "on")
        self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], 20)
        self.assertEqual(len([call for call in ha.calls if call[0] == "fan"]), 1)

    def test_an_observed_speed_below_minimum_is_corrected_even_within_ack_tolerance(self):
        for floor, step, observed in ((20, 10, 19), (66, 100 / 3, 65), (100, 10, 99)):
            with self.subTest(floor=floor, observed=observed):
                cfg = Config.from_dict(purifier_config(minimum=floor))
                ha = FakeHA(states(cfg, now=NOW))
                ha.data[TRANSFER].update(state="on")
                ha.data[TRANSFER]["attributes"].update(percentage=observed, percentage_step=step)
                executor = Executor(cfg, ha, "°F")
                executor.step(Plan(fans={TRANSFER: floor}), ha.states(), 0)
                self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], floor)
                self.assertTrue(any(call[0] == "fan" and call[2].get("percentage") == floor
                                    for call in ha.calls))

    def test_supported_speed_rungs_never_quantize_below_configured_floor(self):
        for floor, step, requested, expected in (
            (20, 10, 20, 20), (25, 10, 25, 30), (20.1, 10, 20.1, 30),
            (66, 100 / 3, 66, 66), (67, 100 / 3, 67, 100),
            (100, 10, 100, 100), (25, 10, 60, 60),
        ):
            with self.subTest(floor=floor, step=step, requested=requested):
                cfg = Config.from_dict(purifier_config(minimum=floor))
                ha = FakeHA(states(cfg, {"alpha": 72, "beta": 72, "gamma": 72}, NOW))
                ha.data[TRANSFER]["attributes"].update(percentage_step=step,
                                                       supported_features=1 | 16 | 32)
                executor = Executor(cfg, ha, "°F")
                plan = Plan(fans={TRANSFER: requested})
                executor.step(plan, ha.states(), 0)
                command = next(call for call in ha.calls if call[0] == "fan")
                self.assertEqual(command[2], {"entity_id": TRANSFER, "percentage": expected})
                self.assertIs(type(command[2]["percentage"]), int)
                self.assertEqual(ha.data[TRANSFER]["state"], "on")
                self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], expected)
                executor.step(plan, ha.states(), 15)
                self.assertNotIn(TRANSFER, executor.pending)
                self.assertNotIn(TRANSFER, executor.fan_failures)
                self.assertEqual(len([call for call in ha.calls if call[0] == "fan"]), 1)

    def test_direct_fan_call_default_keeps_legacy_nearest_speed(self):
        cfg = Config.from_dict(purifier_config())
        ha = FakeHA(states(cfg, now=NOW))
        ha.data[TRANSFER]["attributes"]["percentage_step"] = 10
        executor = Executor(cfg, ha, "°F")
        executor.fan(TRANSFER, 25, Plan(), ha.states(), 0)
        self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], 20)

    def test_direct_fan_call_can_explicitly_enforce_minimum_supported_speed(self):
        cfg = Config.from_dict(purifier_config())
        ha = FakeHA(states(cfg, now=NOW))
        ha.data[TRANSFER]["attributes"]["percentage_step"] = 10
        executor = Executor(cfg, ha, "°F")
        executor.fan(TRANSFER, 25, Plan(), ha.states(), 0, minimum_percentage=25)
        self.assertEqual(ha.data[TRANSFER]["attributes"]["percentage"], 30)


if __name__ == "__main__":
    unittest.main()
