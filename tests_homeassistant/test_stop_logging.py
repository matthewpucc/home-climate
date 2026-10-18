"""Runtime stop diagnostics add durable logs without changing status or commands."""
import logging
import unittest

from tests_homeassistant import test_native as fixtures


LOGGER = "custom_components.house_climate.runtime"


class StopLoggingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runner = fixtures.NativeTests.runner

    def stop_data(self, *, cause="fault", reason="Controller fault: transport unavailable"):
        return {
            "reason": reason, "stop_cause": cause, "forced_stop": cause == "fault",
            "stop_scope": "all_configured_devices",
            "stop_entities": [*self.cfg.climate_entities, *self.cfg.fans.values()],
        }

    async def test_fault_stop_logs_error_once_and_keeps_every_fault_occurrence(self):
        runtime = self.runner(False)
        data = self.stop_data()
        with self.assertLogs(LOGGER, level="INFO") as captured:
            for _ in range(2):
                runtime._async_report("fault", {"error": "transport unavailable",
                                                "shutdown_requested": True})
                runtime._async_report("stopping", dict(data))
                runtime._async_report("stopped", {**data, "failures": []})
        errors = [record for record in captured.records if record.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn(data["reason"], errors[0].getMessage())
        for entity in data["stop_entities"]:
            self.assertIn(entity, errors[0].getMessage())
        self.assertEqual(runtime.fault_count, 2)
        self.assertEqual(runtime.status["last_error"], "transport unavailable")
        self.assertEqual(runtime.status["event"], "stopped")
        self.assertEqual(self.calls, [])

    async def test_expected_stop_logs_info_and_later_live_ends_the_dedup_episode(self):
        runtime = self.runner(False)
        startup = self.stop_data(cause="startup", reason="Startup all-off reconciliation")
        with self.assertLogs(LOGGER, level="INFO") as captured:
            runtime._async_report("stopping", dict(startup))
            runtime._async_report("stopping", dict(startup))
            runtime._async_report("shadow", {"mode": "off", "head_targets_f": {}})
            runtime._async_report("stopping", dict(startup))
            runtime._async_report("live", {"mode": "off", "head_targets_f": {}})
            runtime._async_report("stopping", dict(startup))
        starts = [record for record in captured.records
                  if startup["reason"] in record.getMessage()]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(record.levelno == logging.INFO for record in starts))
        self.assertEqual(runtime.fault_count, 0)
        self.assertEqual(self.calls, [])

    async def test_fault_without_active_shutdown_stays_an_error_and_dedups_repeats(self):
        runtime = self.runner(False)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            runtime._async_report("fault", {"error": "Invalid startup configuration",
                                            "shutdown_requested": False})
            runtime._async_report("fault", {"error": "Invalid startup configuration",
                                            "shutdown_requested": False})
            runtime._async_report("live", {"mode": "off", "head_targets_f": {}})
            runtime._async_report("fault", {"error": "Invalid startup configuration",
                                            "shutdown_requested": False})
        self.assertEqual(len(captured.records), 2)
        self.assertEqual(runtime.fault_count, 3)
        self.assertTrue(all("Invalid startup configuration" in record.getMessage()
                            for record in captured.records))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
