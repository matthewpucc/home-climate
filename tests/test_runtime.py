import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from house_climate.__main__ import run_loop
from tests.test_controller import FakeHA, add_helpers, config, fixture


class BoundedEvent:
    def __init__(self, loops=3):
        self.waits = 0
        self.loops = loops

    def is_set(self):
        return self.waits >= self.loops

    def set(self):
        self.waits = self.loops

    def wait(self, timeout):
        self.waits += 1


class RuntimeTests(unittest.TestCase):
    def setup_run(self, live, enabled):
        cfg = replace(config(), equipment_timing_confirmed=True, heater_setpoint_confirmed=True)
        states = fixture(cfg, datetime.now(timezone.utc), guest=76, living=67)
        add_helpers(states)
        states["input_boolean.hp_enabled"] = {"state": "on" if enabled else "off"}
        states["input_boolean.hp_heater_owned"]["state"] = "off"
        ha = FakeHA(states)
        ha.request = Mock(return_value={"time_zone": "America/Los_Angeles", "unit_system": {"temperature": "°F"}})
        args = SimpleNamespace(live=live, once=not live, snapshot=None, at=None)
        return cfg, ha, args

    def execute(self, cfg, ha, args):
        with patch("house_climate.__main__.HomeAssistant", return_value=ha), \
             patch("house_climate.__main__.threading.Event", return_value=BoundedEvent()), \
             patch("house_climate.__main__.signal.signal"), \
             patch("house_climate.__main__.emit") as output:
            run_loop(args, cfg)
        return output

    def test_shadow_never_writes_even_when_ha_enable_switch_is_on(self):
        cfg, ha, args = self.setup_run(live=False, enabled=True)
        output = self.execute(cfg, ha, args)
        self.assertEqual(ha.calls, [])
        self.assertTrue(any(call.args[0] == "shadow" and call.kwargs.get("mode") == "cool" for call in output.call_args_list))

    def test_live_flag_alone_does_not_activate_devices(self):
        cfg, ha, args = self.setup_run(live=True, enabled=False)
        self.execute(cfg, ha, args)
        self.assertEqual(ha.calls, [])

    def test_live_heating_requires_prior_ownership_and_heartbeat_and_stops_on_exit(self):
        cfg, ha, args = self.setup_run(live=True, enabled=True)
        self.execute(cfg, ha, args)
        activation = next(i for i, call in enumerate(ha.calls) if call[1] == "set_temperature" and call[2]["entity_id"] == cfg.heater_entity)
        before = ha.calls[:activation]
        self.assertTrue(any(call[1] == "turn_on" and call[2]["entity_id"] == "input_boolean.hp_heater_owned" for call in before))
        self.assertTrue(any(call[1] == "set_datetime" and call[2]["entity_id"] == "input_datetime.hp_heartbeat" for call in before))
        self.assertEqual(ha.data[cfg.heater_entity]["state"], "off")
        self.assertEqual(ha.data["input_boolean.hp_heater_owned"]["state"], "off")

    def test_disabling_watchdog_during_operation_stops_heater(self):
        cfg, ha, args = self.setup_run(live=True, enabled=True)
        original = ha.states
        reads = 0

        def read():
            nonlocal reads
            reads += 1
            if reads >= 3:
                ha.data["automation.hp_heater_watchdog"]["state"] = "off"
            return original()

        ha.states = read
        output = self.execute(cfg, ha, args)
        self.assertEqual(ha.data[cfg.heater_entity]["state"], "off")
        self.assertTrue(any(call.args[0] == "fault" for call in output.call_args_list))

    def test_live_snapshot_is_rejected_before_connection(self):
        cfg, ha, args = self.setup_run(live=True, enabled=True)
        args.snapshot = "anything.json"
        with self.assertRaisesRegex(ValueError, "continuous real-time"):
            self.execute(cfg, ha, args)
        ha.request.assert_not_called()

    def test_warm_living_room_uses_fan_only_without_high_heat_setpoint(self):
        cfg, ha, args = self.setup_run(live=True, enabled=True)
        for probe in cfg.zones["living"].probes:
            ha.data[probe.entity_id]["state"] = "72"
        ha.data["binary_sensor.hp_living_probe_safe"]["state"] = "off"
        self.execute(cfg, ha, args)
        calls = [call for call in ha.calls if call[2]["entity_id"] == cfg.heater_entity]
        self.assertTrue(any(call[2].get("hvac_mode") == "fan_only" for call in calls))
        self.assertFalse(any(call[1] == "set_temperature" for call in calls))
        self.assertEqual(ha.data[cfg.heater_entity]["state"], "off")
