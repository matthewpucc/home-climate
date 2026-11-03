"""Supported per-device aim, comfort-only intent, thermal boost and stable routing."""
from datetime import timedelta
import unittest

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor
from house_climate.policy import Policy
from house_climate.bindings import example_bindings
from house_climate.house_policy import house_configuration, editable_policy_settings
from tests.test_controller import FakeHA
from tests.test_rooms import configuration, states, NOW

FAN = "fan.test_airflow"


def directed_config():
    raw = configuration()
    raw["forecast_minutes"] = 0
    raw["rooms"]["alpha"]["profile"] = {"target_f": 72, "low_f": 70, "high_f": 74}
    raw["rooms"]["beta"]["profile"] = {"target_f": 72, "low_f": 70, "high_f": 74}
    raw["rooms"]["gamma"].update(profile={"target_f": 68, "low_f": 67, "high_f": 69}, schedules=[])
    raw["rooms"]["alpha"]["fans"] = [{
        "entity_id": FAN, "strategy": "directed", "percentage": 60, "minimum_percentage": 20,
        "airflow": {"profiles": {
            "toward_alpha": {"source": "beta", "destination": "alpha", "lower_angle": 100,
                             "upper_angle": 190, "calibration": "provisional"},
            "toward_beta": {"source": "alpha", "destination": "beta", "lower_angle": 43,
                            "upper_angle": 75, "calibration": "verified"},
        }},
    }]
    return raw


def fixed_config():
    raw = directed_config()
    raw["rooms"]["alpha"]["fans"][0]["airflow"] = {
        "outlet": "calibrated_fixed", "calibration_key": "synthetic_local_purifier",
        "profiles": {"local_circulation": {
            "source": "alpha", "destination": "alpha", "purpose": "circulation",
            "lower_angle": 100, "upper_angle": 190, "calibration": "verified", "direction": None}}}
    return raw


class DirectedAirflowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(directed_config())
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)

    def data(self, alpha=73, beta=73, seconds=0):
        now = NOW + timedelta(seconds=seconds)
        result = states(self.cfg, {"alpha": alpha, "beta": beta, "gamma": 68}, now)
        result[FAN]["attributes"].update(supported_features=39, percentage_step=10,
            direction="forward", angle_low=43, angle_high=75, oscillating=True, oscillation_enabled=True)
        result[FAN]["last_reported"] = now.isoformat()
        return result

    def decide(self, alpha=73, beta=73, seconds=0, data=None):
        return self.policy.decide(data or self.data(alpha, beta, seconds),
                                  NOW + timedelta(seconds=seconds), self.settings)

    def test_comfort_aim_without_hvac_or_useful_gradient_does_not_claim_cooling(self):
        plan = self.decide(73, 73.1)
        # Two plausible destinations have no arbitrary winner.
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.cfg = Config.from_dict(directed_config())
        raw = self.cfg.to_dict()
        raw["rooms"]["beta"]["profile"].update(comfort_enabled=False)
        self.cfg = Config.from_dict(raw)
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        plan = self.decide(73, 73.1)
        self.assertEqual(plan.mode, "off")
        self.assertEqual(plan.head_targets_f, {})
        self.assertEqual(plan.fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertEqual(plan.fans[FAN], 20)
        self.assertFalse(plan.fan_airflow[FAN]["physical_benefit_verified"])

    def test_both_device_specific_directions_and_provisional_status(self):
        for alpha, beta, name, low, high, provisional in (
                (73, 72, "toward_alpha", 100, 190, True),
                (72, 73, "toward_beta", 43, 75, False)):
            with self.subTest(name=name):
                self.policy = Policy(self.cfg)
                plan = self.decide(alpha, beta)
                self.assertEqual(plan.fan_airflow[FAN]["profile"], name)
                self.assertEqual(plan.fans[FAN], 60)
                intent = plan.fan_airflow[FAN]
                self.assertEqual((intent["requested"]["lower_angle"], intent["requested"]["upper_angle"]), (low, high))
                self.assertEqual(intent["provisional"], provisional)

    def test_limits_only_nominal_deviation_is_not_comfort_demand(self):
        raw = directed_config()
        for name in ("alpha", "beta"):
            raw["rooms"][name]["profile"]["comfort_enabled"] = False
        self.cfg = Config.from_dict(raw)
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        plan = self.decide(73.5, 72)
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertEqual(plan.fans[FAN], 20)

    def test_limits_demand_uses_limit_stop_not_nominal_target(self):
        raw = directed_config()
        raw["rooms"]["alpha"]["profile"]["comfort_enabled"] = False
        self.cfg = Config.from_dict(raw)
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        self.assertEqual(self.decide(74.5, 72).fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertIsNone(self.decide(73.4, 72, 90).fan_airflow[FAN]["profile"])

    def test_no_demand_retains_physical_profile_without_new_angle_intent(self):
        self.decide(73, 72)
        plan = self.decide(72, 72, 90)
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertEqual(plan.fan_airflow[FAN]["last_selected_profile"], "toward_alpha")
        self.assertEqual(plan.fans[FAN], 20)

    def test_invalid_old_direction_cancels_immediately_and_reverse_waits_for_dwell(self):
        self.assertEqual(self.decide(73, 72).fan_airflow[FAN]["profile"], "toward_alpha")
        for seconds in (90, 180, 270):
            plan = self.decide(72, 73, seconds)
            self.assertIsNone(plan.fan_airflow[FAN]["profile"])
            self.assertEqual(plan.fans[FAN], 20)
        self.assertEqual(self.decide(72, 73, 360).fan_airflow[FAN]["profile"], "toward_beta")

    def test_ambiguous_demand_retains_only_still_valid_previous_choice(self):
        self.decide(73, 72)
        self.assertEqual(self.decide(73, 73.1, 90).fan_airflow[FAN]["profile"], "toward_alpha")

    def test_stale_source_or_destination_cancels_profile_and_boost(self):
        for room in ("alpha", "beta"):
            self.policy = Policy(self.cfg)
            self.decide(73, 72)
            data = self.data(73, 72, 15)
            for probe in self.cfg.zones[room].probes:
                data[probe.entity_id]["last_reported"] = (NOW - timedelta(hours=1)).isoformat()
            plan = self.decide(seconds=15, data=data)
            self.assertIsNone(plan.fan_airflow[FAN]["profile"])
            self.assertEqual(plan.fans[FAN], 20)

    def test_opposing_source_cancels_even_during_dwell(self):
        self.decide(73, 72)
        data = self.data(73, 72, 15)
        data[self.cfg.heads["shared"]]["state"] = "heat"
        plan = self.decide(seconds=15, data=data)
        self.assertIsNone(plan.fan_airflow[FAN]["profile"])
        self.assertEqual(plan.fans[FAN], 20)

    def test_fast_maintenance_cancels_but_cannot_switch_or_restart(self):
        cadence = DecisionCadence(self.cfg, clock=lambda: 0)
        first = cadence.decide(self.data(73, 72), NOW, self.settings, monotonic_now=0)
        self.assertEqual(first.fan_airflow[FAN]["profile"], "toward_alpha")
        stop = cadence.decide(self.data(72, 73, 15), NOW + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assertIsNone(stop.fan_airflow[FAN]["profile"])
        again = cadence.decide(self.data(73, 72, 30), NOW + timedelta(seconds=30), self.settings, monotonic_now=30)
        self.assertIsNone(again.fan_airflow[FAN]["profile"])

    def test_maintenance_cannot_upgrade_comfort_aim_into_transfer_boost(self):
        raw = directed_config()
        del raw["rooms"]["alpha"]["fans"][0]["airflow"]["profiles"]["toward_beta"]
        self.cfg = Config.from_dict(raw)
        self.settings = Settings.read({}, self.cfg)
        cadence = DecisionCadence(self.cfg, clock=lambda: 0)
        first = cadence.decide(self.data(73, 72.8), NOW, self.settings, monotonic_now=0)
        self.assertEqual(first.fans[FAN], 20)
        interim = cadence.decide(self.data(73, 72, 15), NOW + timedelta(seconds=15), self.settings, monotonic_now=15)
        self.assertEqual(interim.fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertEqual(interim.fans[FAN], 20)
        next_plan = cadence.decide(self.data(73, 72, 90), NOW + timedelta(seconds=90), self.settings, monotonic_now=90)
        self.assertEqual(next_plan.fans[FAN], 60)

    def test_retained_comfort_aim_does_not_lower_a_new_boosts_start_threshold(self):
        raw = directed_config()
        raw["rooms"]["beta"]["profile"]["comfort_enabled"] = False
        self.cfg = Config.from_dict(raw)
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        first = self.decide(73, 72.8)
        self.assertEqual(first.fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertEqual(first.fans[FAN], 20)
        self.assertEqual(self.decide(73, 72.6, 90).fans[FAN], 20)
        self.assertEqual(self.decide(73, 72.2, 180).fans[FAN], 60)
        self.assertEqual(self.decide(73, 72.6, 270).fans[FAN], 60)
        self.assertEqual(self.decide(73, 72.8, 360).fans[FAN], 20)
        self.assertEqual(self.decide(73, 72.6, 450).fans[FAN], 20)

    def test_interrupted_direction_candidate_requires_new_continuous_confirmation(self):
        self.decide(73, 72)
        self.assertIsNone(self.decide(72, 73, 180).fan_airflow[FAN]["profile"])
        self.assertEqual(self.decide(73, 72, 270).fan_airflow[FAN]["profile"], "toward_alpha")
        self.assertIsNone(self.decide(72, 73, 360).fan_airflow[FAN]["profile"])
        self.assertEqual(self.decide(72, 73, 480).fan_airflow[FAN]["profile"], "toward_beta")

    def test_backend_without_guarded_adapter_keeps_purification(self):
        data = self.data(73, 72)
        ha = FakeHA(data)
        executor = Executor(self.cfg, ha, "°F")
        plan = self.decide(73, 72)
        for seconds in (0, 15):
            executor.step(plan, ha.states(), seconds)
        self.assertEqual(ha.data[FAN]["attributes"]["percentage"], 20)
        self.assertEqual(plan.fan_airflow[FAN]["execution"]["phase"], "unsupported")
        self.assertTrue(any("native guarded" in warning for warning in plan.warnings))

    def test_configuration_roundtrip_and_invalid_or_fabricated_profiles(self):
        self.assertEqual(Config.from_dict(self.cfg.to_dict()).fan_controls, self.cfg.fan_controls)
        for edits in ({"lower_angle": True}, {"lower_angle": 351}, {"upper_angle": 42},
                      {"direction": "reverse"}, {"calibration": "assumed"}, {"destination": "unknown"}):
            raw = directed_config()
            raw["rooms"]["alpha"]["fans"][0]["airflow"]["profiles"]["toward_beta"].update(edits)
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_house_profiles_are_distinct_and_calibration_edits_roundtrip(self):
        bindings = example_bindings()
        cfg = house_configuration(bindings=bindings)
        office = cfg.fan_controls[bindings.fans["office"]].airflow
        entertainment = cfg.fan_controls[bindings.fans["entertainment"]].airflow
        self.assertEqual(office.profiles["toward_office"].calibration, "provisional")
        self.assertEqual(set(entertainment.profiles), {"entertainment_circulation"})
        self.assertEqual(entertainment.profiles["entertainment_circulation"].purpose, "circulation")
        self.assertEqual(entertainment.outlet, "calibrated_fixed")
        self.assertIsNone(entertainment.profiles["entertainment_circulation"].direction)
        self.assertEqual(office.outlet, "switchable")
        overrides = {"office": {"toward_office": {"lower_angle": 105, "calibration": "verified"}}}
        changed = house_configuration(bindings=bindings, policy_settings={"purifiers": {"airflow_profiles": overrides}})
        exported = editable_policy_settings(changed, bindings)
        self.assertEqual(exported["purifiers"]["airflow_profiles"], overrides)
        restored = house_configuration(bindings=bindings, policy_settings=exported)
        self.assertEqual(restored.fan_controls, changed.fan_controls)

    def test_fixed_outlet_requires_explicit_verified_local_calibration(self):
        cfg = Config.from_dict(fixed_config())
        self.assertEqual(Config.from_dict(cfg.to_dict()).fan_controls, cfg.fan_controls)
        for edit in ({"calibration": "provisional"}, {"direction": "forward"},
                     {"purpose": "transfer", "source": "beta"}):
            raw = fixed_config()
            raw["rooms"]["alpha"]["fans"][0]["airflow"]["profiles"]["local_circulation"].update(edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                Config.from_dict(raw)
        for edit in ({"calibration_key": None}, {"outlet": "automatic"}, {"outlet": "switchable"}):
            raw = fixed_config()
            raw["rooms"]["alpha"]["fans"][0]["airflow"].update(edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                Config.from_dict(raw)

    def test_known_fixed_calibration_can_be_explicitly_rekeyed_after_recommissioning(self):
        bindings = example_bindings()
        overrides = {"entertainment": {"entertainment_circulation": {
            "calibration_key": "synthetic_recommissioned_local_purifier"}}}
        cfg = house_configuration(bindings=bindings, policy_settings={"purifiers": {"airflow_profiles": overrides}})
        self.assertEqual(cfg.fan_controls[bindings.fans["entertainment"]].airflow.calibration_key,
                         "synthetic_recommissioned_local_purifier")
        self.assertEqual(editable_policy_settings(cfg, bindings)["purifiers"]["airflow_profiles"], overrides)


if __name__ == "__main__":
    unittest.main()
