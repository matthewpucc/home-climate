"""Status distinguishes intent, HA reports and independent electrical evidence."""
import tempfile
from types import SimpleNamespace
import unittest

from homeassistant.core import HomeAssistant

from custom_components.house_climate.sensor import (
    AuxiliaryPlan, ControllerStatus, CurrentIssues, HeaterPowerStatus, Overview, RecoveryStatus, RoomPlan, RoomTemperature,
    heartbeat_attributes, plan_warnings, reported_device,
)
from tests.test_controller import config


class OperationEvidenceStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.config = config()
        self.status = {"event": "live", "mode": "heat", "phase": "running",
                       "head_targets_f": {"living": 69}, "head_call_targets_f": {"living": 74},
                       "heater_on": True, "auxiliary_fan_only": False,
                       "room_targets": {}, "power_guard": {"heater_draw_status": "idle"}}
        self.guard = {"blocked": True, "live": False, "shutdown_evidence": "reported_off_unverified",
                      "shutdown_confirmation": "state_only", "electrical_stop_confirmed": False}
        self.guard["heater_draw_status"] = "idle"
        self.status["power_guard"] = self.guard
        self.entry = SimpleNamespace(entry_id="evidence_fixture", runtime_data=SimpleNamespace(
            config=self.config, status=self.status, power_guard=SimpleNamespace(diagnostics=self.guard)))

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def sensor(self, cls, *args):
        entity = cls(self.entry, *args)
        entity.hass = self.hass
        return entity

    async def test_head_heat_echo_is_reported_not_independent_operation_proof(self):
        entity = self.config.heads["living"]
        self.hass.states.async_set(entity, "heat", {"temperature": 74, "hvac_action": "heating"})
        view = self.sensor(RoomPlan, "living").extra_state_attributes
        self.assertEqual(view["requested"]["target_f"], 69)
        self.assertEqual(view["reported"]["mode"], "heat")
        self.assertEqual(view["reported"]["action"], "heating")
        self.assertFalse(view["reported"]["physical_operation_corroborated"])
        self.assertIn("ha_last_reported", view["reported"])
        self.assertNotIn("measurement_at", view["reported"])

    async def test_assumed_and_restored_feedback_remains_labelled(self):
        for attrs, expected in (({"assumed_state": True}, "assumed"), ({"restored": True}, "restored")):
            self.hass.states.async_set(self.config.heater_entity, "heat", attrs)
            self.assertEqual(reported_device(self.hass, self.config.heater_entity)["feedback_quality"], expected)

    async def test_manual_auxiliary_has_no_automation_request_even_with_old_heat_plan(self):
        self.status.update(auxiliary_manual=True, event="stopped")
        sensor = self.sensor(AuxiliaryPlan)
        self.assertTrue(sensor.available)
        self.assertIn("Manual", sensor.native_value)
        self.assertIsNone(sensor.extra_state_attributes["requested"]["mode"])
        self.assertEqual(sensor.extra_state_attributes["ownership"], "manual")

    async def test_mode_heat_low_load_is_not_reported_as_corroborated_heating(self):
        self.hass.states.async_set(self.config.heater_entity, "heat", {"temperature": 80})
        sensor = self.sensor(AuxiliaryPlan)
        view = sensor.extra_state_attributes
        self.assertEqual(view["requested"]["mode"], "heat")
        self.assertEqual(view["reported"]["mode"], "heat")
        self.assertEqual(view["operation_evidence"]["dedicated_load_status"], "idle")
        self.assertFalse(view["reported"]["physical_operation_corroborated"])

    async def test_guard_exposes_authoritative_three_state_shutdown_evidence(self):
        sensor = self.sensor(HeaterPowerStatus)
        for evidence in ("corroborated_off", "reported_off_unverified", "contradictory"):
            self.guard["shutdown_evidence"] = evidence
            self.assertEqual(sensor.extra_state_attributes["shutdown_evidence"], evidence)
        self.assertIn("contradicted", sensor.native_value)

    async def test_source_provenance_and_seasonal_state_are_visible(self):
        source = {"source": "fixture_probe", "measurement_at": None, "reported_at": 100,
                  "timestamp_kind": "ha_report", "freshness": "qualified"}
        self.status["rooms"] = {"living": {"temperature": 69, "sources": 1,
                                             "source_evidence": (source,), "observed_at": 100}}
        self.status["seasonal"] = {"profile": "cool", "degraded": True, "reason": "Using fallback"}
        self.assertEqual(self.sensor(RoomTemperature, "living").extra_state_attributes["source_evidence"], (source,))
        self.assertEqual(self.sensor(ControllerStatus).extra_state_attributes["seasonal"], self.status["seasonal"])

    async def test_independent_lease_evidence_visible_without_poll_countdown_churn(self):
        status = {"heartbeat": {"state": "confirmed", "blocked": False,
                                "independent_auxiliary_authorization": True,
                                "controller_timeout_seconds": 300, "auxiliary_timeout_seconds": 120,
                                "auxiliary_authorization_state": "valid", "auxiliary_lease_remaining_seconds": 110}}
        view = heartbeat_attributes(status)["heartbeat"]
        self.assertTrue(view["independent_auxiliary_authorization"])
        self.assertEqual(view["auxiliary_timeout_seconds"], 120)
        self.assertNotIn("auxiliary_lease_remaining_seconds", view)

    async def test_critical_route_gap_is_visible_without_claiming_device_delivery(self):
        status = {"notifications": {"active": [{"severity": "critical", "message": "Shutdown unconfirmed",
                    "critical_route_unverified": True, "delivery_state": "service_accepted"}]}}
        text = " ".join(plan_warnings(status))
        self.assertIn("Critical attention", text)
        self.assertIn("route is unverified", text)
        self.assertNotIn("delivered", text)

    async def test_stopped_worker_keeps_incomplete_handoff_visible(self):
        self.status.update(event="stopped", handoff={"stage": "blocked", "problems": {
            "climate.fixture_head": "Auto range not confirmed"}})
        for cls in (Overview, RecoveryStatus, CurrentIssues):
            self.assertIn("Recovery handoff incomplete", self.sensor(cls).native_value)
        self.assertEqual(self.sensor(RecoveryStatus).extra_state_attributes["handoff"], self.status["handoff"])

    async def test_stopped_controller_does_not_hide_critical_operator_attention(self):
        self.status.update(event="stopped", notifications={"active": [{"severity": "critical",
            "message": "Battery reserve reached", "critical_route_unverified": True}]})
        self.assertIn("Battery reserve reached", self.sensor(CurrentIssues).native_value)

    async def test_overview_does_not_claim_automation_heat_during_manual_takeover(self):
        self.status["auxiliary_manual"] = True
        text = self.sensor(Overview).native_value
        self.assertIn("auxiliary under manual control", text)
        self.assertNotIn("auxiliary heat requested", text)


if __name__ == "__main__":
    unittest.main()
