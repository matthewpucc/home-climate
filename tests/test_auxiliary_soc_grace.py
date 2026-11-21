"""The worker treats native battery-reading grace as retention, not permission."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from house_climate.__main__ import authorize_auxiliary_plan
from house_climate.executor import Executor
from house_climate.policy import Plan, Policy
from house_climate.sensors import Sensors
from tests.test_controller import FakeHA, NIGHT, config, fixture


class AuxiliarySocGraceTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), legacy=False)
        self.heater = self.config.heater_entity
        self.probe_safe = self.config.entity("auxiliary_probe_safe", "binary_sensor")
        self.policy = Policy(self.config)
        self.states = fixture(self.config, living=66)
        self.states[self.probe_safe] = {"state": "on"}
        self.states[self.heater]["state"] = "heat"
        self.states[self.heater]["attributes"].update(
            hvac_action="heating",
            home_climate_auxiliary_blocked=True,
            home_climate_auxiliary_fan_blocked=True,
            home_climate_auxiliary_soc_grace_active=True,
        )
        self.plan = Plan(room_targets={self.config.auxiliary_room: {
            "low_f": 68, "high_f": 72, "target_f": 70, "comfort_enabled": True,
            "cool_requested": True,
        }})
        self.ha = FakeHA(self.states)
        self.ha.controls = object()
        self.ha.authorize_auxiliary = Mock(return_value=True)
        self.executor = Executor(self.config, self.ha, "°F")

    def decide(self):
        rooms, _ = Sensors(self.config).read(self.states, NIGHT)
        self.policy.update_auxiliary(self.plan, self.states, rooms, NIGHT)

    def test_retains_active_heat_without_service_or_lease_renewal(self):
        self.policy.auxiliary_dispatch.active = True
        self.decide()
        self.assertTrue(self.plan.heater_on)
        self.assertTrue(self.plan.auxiliary_policy["soc_grace_retaining"])
        self.assertFalse(self.plan.auxiliary_fan_only)
        authorize_auxiliary_plan(self.ha, self.config, self.plan, self.states, NIGHT)
        self.assertTrue(self.plan.heater_on)
        # Existing native temperature is 72 F versus the ordinary 86 F
        # equipment call target; do not program a new target during grace.
        self.executor.auxiliary(self.plan, self.states, 1)
        self.assertEqual([], self.ha.calls)
        self.ha.authorize_auxiliary.assert_not_called()

    def test_grace_is_not_a_new_heat_or_fan_plan(self):
        self.policy._auxiliary_fan_enabled = True
        self.decide()
        self.assertFalse(self.plan.heater_on)
        self.assertFalse(self.plan.auxiliary_fan_only)
        self.assertNotIn("soc_grace_retaining", self.plan.auxiliary_policy)

    def test_room_cutoff_can_still_remove_the_grace_plan(self):
        self.policy.auxiliary_dispatch.active = True
        for probe in self.config.zones[self.config.auxiliary_room].probes:
            self.states[probe.entity_id]["state"] = "72"
        self.decide()
        self.assertFalse(self.plan.heater_on)
        self.executor.auxiliary(self.plan, self.states, 1)
        self.assertEqual(["off"], [data.get("hvac_mode") for _, _, data in self.ha.calls])

    def test_expired_grace_snapshot_cannot_turn_retention_into_positive_dispatch(self):
        self.policy.auxiliary_dispatch.active = True
        self.decide()
        self.assertTrue(self.plan.heater_on)
        for attrs in (self.states[self.heater]["attributes"], self.ha.data[self.heater]["attributes"]):
            attrs["home_climate_auxiliary_soc_grace_active"] = False
        self.executor.auxiliary(self.plan, self.states, 1)
        self.assertEqual(["off"], [data.get("hvac_mode") for _, _, data in self.ha.calls])

    def test_bad_probe_or_invalid_input_cannot_retain_or_renew(self):
        self.policy.auxiliary_dispatch.active = True
        self.decide()
        self.states[self.probe_safe]["state"] = "off"
        authorize_auxiliary_plan(self.ha, self.config, self.plan, self.states, NIGHT)
        self.assertFalse(self.plan.heater_on)
        self.ha.authorize_auxiliary.assert_not_called()
        self.executor.auxiliary(self.plan, self.states, 1)
        self.assertEqual(["off"], [data.get("hvac_mode") for _, _, data in self.ha.calls])

    def test_unresolved_positive_device_job_is_not_retention(self):
        self.policy.auxiliary_dispatch.active = True
        self.decide()
        self.assertTrue(self.plan.heater_on)
        self.ha.uncertain_service_jobs = Mock(return_value=(object(),))
        self.executor.auxiliary(self.plan, self.states, 1)
        self.assertEqual(["off"], [data.get("hvac_mode") for _, _, data in self.ha.calls])

    def test_retain_requires_native_controls_and_positive_heating_feedback(self):
        for mode, action, native in (("heat", "idle", True), ("heat", "heating", False)):
            with self.subTest(mode=mode, action=action, native=native):
                self.ha.controls = object() if native else None
                self.states[self.heater]["state"] = mode
                self.states[self.heater]["attributes"]["hvac_action"] = action
                self.plan.heater_on = True
                self.plan.auxiliary_policy["soc_grace_retaining"] = True
                authorize_auxiliary_plan(self.ha, self.config, self.plan, self.states, NIGHT)
                self.assertFalse(self.plan.heater_on)
        self.ha.authorize_auxiliary.assert_not_called()
        self.assertEqual([], self.ha.calls)

    def test_normal_authorization_resumes_only_from_a_new_nongrace_plan(self):
        self.policy.auxiliary_dispatch.active = True
        self.decide()
        attrs = self.states[self.heater]["attributes"]
        attrs["home_climate_auxiliary_soc_grace_active"] = False
        attrs["home_climate_auxiliary_blocked"] = False
        authorize_auxiliary_plan(self.ha, self.config, self.plan, self.states, NIGHT)
        self.assertFalse(self.plan.heater_on)
        self.ha.authorize_auxiliary.assert_not_called()
        self.decide()
        self.assertTrue(self.plan.heater_on)
        self.assertNotIn("soc_grace_retaining", self.plan.auxiliary_policy)
        authorize_auxiliary_plan(self.ha, self.config, self.plan, self.states, NIGHT)
        self.ha.authorize_auxiliary.assert_called_once_with(self.plan, self.states)
