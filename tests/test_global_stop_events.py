"""Whole-controller Off requests preserve cause, scope, order, and outcomes."""
import unittest

from house_climate.__main__ import release, run_loop
from house_climate.executor import Executor
from house_climate.ha import HAError
from tests import test_runtime as fixtures


class GlobalStopEventTests(unittest.TestCase):
    setup_run = fixtures.RuntimeTests.setup_run

    def active_equipment(self, ha, cfg):
        for entity in cfg.heads.values():
            ha.data[entity]["state"] = "cool"
        ha.data[cfg.heater_entity]["state"] = "heat"
        for entity in cfg.fans.values():
            ha.data[entity]["state"] = "on"

    def expected_devices(self, cfg):
        return set(cfg.climate_entities) | set(cfg.fans.values())

    def recorder(self, ha):
        events = []

        def report(event, **data):
            events.append((event, data, list(ha.calls)))

        return events, report

    def test_release_reports_cause_before_off_and_result_after_every_device_attempt(self):
        cfg, ha, _ = self.setup_run(True, True)
        self.active_equipment(ha, cfg)
        executor = Executor(cfg, ha, "°F")
        events, report = self.recorder(ha)
        release(ha, executor, report, reason="Controller fault: heartbeat unavailable",
                cause="fault", forced=True)
        self.assertEqual([event for event, _, _ in events], ["stopping", "stopped"])
        before, after = events
        self.assertEqual(before[2], [])
        for _, data, _ in events:
            self.assertEqual(data["reason"], "Controller fault: heartbeat unavailable")
            self.assertEqual(data["stop_cause"], "fault")
            self.assertTrue(data["forced_stop"])
            self.assertEqual(data["stop_scope"], "all_configured_devices")
            self.assertEqual(set(data["stop_entities"]), self.expected_devices(cfg))
        self.assertEqual(after[1]["failures"], [])
        requested = {data["entity_id"] for domain, service, data in after[2]
                     if domain in ("climate", "fan")
                     and service in ("set_hvac_mode", "turn_off")}
        self.assertEqual(requested, self.expected_devices(cfg))
        self.assertTrue(all(ha.data[entity]["state"] == "off"
                            for entity in self.expected_devices(cfg)))
        for entity in cfg.heads.values():
            self.assertEqual(executor.head_stop_reasons[entity]["reason"],
                             "Controller fault: heartbeat unavailable")

    def test_release_keeps_failed_off_visible_without_skipping_other_devices(self):
        cfg, ha, _ = self.setup_run(True, True)
        self.active_equipment(ha, cfg)
        failed = next(iter(cfg.fans.values()))
        original = ha.service

        def service(domain, action, **data):
            if data["entity_id"] == failed and action == "turn_off":
                ha.calls.append((domain, action, data))
                raise HAError("fan unavailable")
            return original(domain, action, **data)

        ha.service = service
        events, report = self.recorder(ha)
        release(ha, Executor(cfg, ha, "°F"), report,
                reason="Controller disabled", cause="disabled")
        self.assertEqual(events[-1][1]["failures"], [failed])
        self.assertFalse(events[-1][1]["forced_stop"])
        self.assertEqual(ha.data[failed]["state"], "on")
        self.assertTrue(all(ha.data[entity]["state"] == "off"
                            for entity in self.expected_devices(cfg) - {failed}))

    def test_startup_reconciliation_is_visible_before_actuator_off_requests(self):
        cfg, ha, args = self.setup_run(True, True)
        events, report = self.recorder(ha)
        run_loop(args, cfg, ha_client=ha, stop_event=fixtures.BoundedEvent(1),
                 event_callback=report)
        startup = next(item for item in events
                       if item[0] == "stopping" and item[1]["stop_cause"] == "startup")
        self.assertFalse(startup[1]["forced_stop"])
        self.assertEqual(startup[1]["stop_scope"], "all_configured_devices")
        self.assertEqual(set(startup[1]["stop_entities"]), self.expected_devices(cfg))
        self.assertFalse(any(domain in ("climate", "fan") for domain, _, _ in startup[2]))
        self.assertTrue(any(event == "arming" for event, _, _ in events))

    def test_active_global_api_fault_requests_shutdown_and_preserves_enabled_intent(self):
        cfg, ha, args = self.setup_run(True, True)
        original = ha.service
        quit_event = fixtures.BoundedEvent(4)
        events = []

        def service(domain, action, **data):
            if domain == "input_datetime" and action == "set_datetime":
                raise HAError("heartbeat dispatch failed")
            return original(domain, action, **data)

        def report(event, **data):
            events.append((event, data, list(ha.calls)))
            if event == "stopped" and data.get("stop_cause") == "fault":
                quit_event.set()

        ha.service = service
        run_loop(args, cfg, ha_client=ha, stop_event=quit_event, event_callback=report)
        fault = next(data for event, data, _ in events if event == "fault")
        self.assertEqual(fault["error"], "heartbeat dispatch failed")
        self.assertTrue(fault["shutdown_requested"])
        stopping = next(item for item in events
                        if item[0] == "stopping" and item[1]["stop_cause"] == "fault")
        stopped = next(item for item in events
                       if item[0] == "stopped" and item[1]["stop_cause"] == "fault")
        self.assertTrue(stopping[1]["forced_stop"])
        self.assertIn("heartbeat dispatch failed", stopping[1]["reason"])
        calls = stopped[2][len(stopping[2]):]
        self.assertEqual({data["entity_id"] for domain, _, data in calls
                          if domain in ("climate", "fan")}, self.expected_devices(cfg))
        self.assertEqual(ha.data[cfg.entity("enabled")]["state"], "on")


if __name__ == "__main__":
    unittest.main()
