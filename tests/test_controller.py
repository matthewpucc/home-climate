from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from house_climate.__main__ import preflight
from house_climate.config import Config, NUMBERS, TIMES, TOGGLES, Probe, Settings, Zone, helper
from house_climate.executor import ControlFault, Executor
from house_climate.ha import HAError, HomeAssistant
from house_climate.ha_package import package, watchdog_signature
from house_climate.policy import Plan, Policy
from house_climate.sensors import Sensors
from house_climate.locking import live_lock

ROOT = Path(__file__).resolve().parents[1]
NIGHT = datetime(2026, 10, 16, 5, 30, tzinfo=timezone.utc)  # 22:30 PDT


def config():
    return replace(Config.load(ROOT / "tests/fixtures/legacy.toml"),
                   decision_interval_seconds=.03, head_minimum_run_seconds=0, head_minimum_off_seconds=0,
                   head_heat_call_offset_f=0, head_cool_call_offset_f=0,
                   head_activation_timeout_seconds=45)


def fixture(cfg, now=NIGHT, **temperatures):
    values = {"office": 72, "entertainment": 71, "guest": 72, "primary": 68, "living": 68}
    values.update(temperatures)
    states = {}
    for name, zone in cfg.zones.items():
        for probe in zone.probes:
            states[probe.entity_id] = {"state": str(values[name]), "last_updated": now.isoformat(),
                                      "attributes": {"unit_of_measurement": "°F"}}
    for entity in [*cfg.heads.values(), cfg.heater_entity]:
        states[entity] = {"state": "off", "attributes": {"hvac_modes": ["off", "heat", "cool"], "min_temp": 60,
                                                       "max_temp": 90, "target_temp_step": 1, "temperature": 72}}
    for entity in cfg.fans.values():
        states[entity] = {"state": "off", "attributes": {"supported_features": 49, "percentage": 0, "percentage_step": 10}}
    states[cfg.heater_entity]["attributes"]["hvac_modes"] = ["off", "heat", "fan_only"]
    return states


def add_helpers(states):
    for name, (default, *_) in NUMBERS.items():
        states[helper(name)] = {"state": str(default)}
    for name, default in TIMES.items():
        states[helper(name, "input_datetime")] = {"state": default}
    for name, default in TOGGLES.items():
        states[helper(name, "input_boolean")] = {"state": "on" if default else "off"}
    states.update({"automation.hp_heater_watchdog": {"state": "on"},
                   "input_datetime.hp_heartbeat": {"state": "2026-10-15 22:30:00"},
                   "input_text.hp_status": {"state": ""},
                   "input_boolean.hp_heater_owned": {"state": "on"},
                   "binary_sensor.hp_living_probe_safe": {"state": "on", "attributes": {"configuration_signature": watchdog_signature(config())}}})


class FakeHA:
    def __init__(self, states, ignore=()):
        self.data = copy.deepcopy(states)
        self.calls = []
        self.ignore = set(ignore)

    def states(self):
        return copy.deepcopy(self.data)

    def service(self, domain, service, **data):
        self.calls.append((domain, service, data))
        entity = data["entity_id"]
        if entity in self.ignore:
            return
        state = self.data[entity]
        state["last_reported"] = datetime.now(timezone.utc).isoformat()
        if service in ("set_hvac_mode", "set_temperature"):
            state["state"] = data["hvac_mode"]
            if "temperature" in data:
                state["attributes"]["temperature"] = data["temperature"]
        elif service == "set_percentage":
            state["state"] = "on" if data["percentage"] else "off"
            state["attributes"]["percentage"] = data["percentage"]
        elif service == "turn_off":
            state["state"] = "off"
        elif service == "turn_on":
            state["state"] = "on"
            if "percentage" in data:
                state["attributes"]["percentage"] = data["percentage"]
        elif service == "set_value":
            state["state"] = data["value"]
        elif service == "set_datetime":
            state["state"] = datetime.fromtimestamp(data["timestamp"], timezone.utc).isoformat()


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_median_with_persistent_extreme(self):
        states = fixture(self.cfg)
        probes = self.cfg.zones["office"].probes
        states[probes[0].entity_id]["state"] = "80"
        sensors = Sensors(self.cfg)
        readings, warnings = sensors.read(states, NIGHT)
        self.assertEqual(readings["office"].temperature, 72)
        self.assertEqual(readings["office"].confirmed_high, 72)
        self.assertEqual(readings["office"].high, 80)
        readings, _ = sensors.read(states, NIGHT + timedelta(seconds=61))
        self.assertEqual(readings["office"].confirmed_high, 80)
        self.assertTrue(any("disagree" in item for item in warnings))

    def test_celsius_nan_missing_and_stale(self):
        states = fixture(self.cfg)
        a, b, c = self.cfg.zones["office"].probes
        states[a.entity_id].update(state="20", attributes={"unit_of_measurement": "°C"})
        states[b.entity_id]["state"] = "NaN"
        states[c.entity_id]["last_updated"] = (NIGHT - timedelta(hours=1)).isoformat()
        readings, warnings = Sensors(self.cfg).read(states, NIGHT)
        self.assertEqual(readings["office"].temperature, 68)
        self.assertEqual(readings["office"].sources, 1)
        self.assertTrue(any("one independent" in item for item in warnings))

    def test_unchanged_but_reported_sensor_remains_fresh(self):
        states = fixture(self.cfg)
        for probe in self.cfg.zones["office"].probes:
            states[probe.entity_id]["last_updated"] = (NIGHT - timedelta(days=1)).isoformat()
            states[probe.entity_id]["last_reported"] = NIGHT.isoformat()
        readings, _ = Sensors(self.cfg).read(states, NIGHT)
        self.assertIn("office", readings)

    def test_source_aliases_do_not_count_twice(self):
        probes = self.cfg.zones["office"].probes
        zones = {**self.cfg.zones, "office": Zone("bonus", tuple(replace(p, source="one") for p in probes))}
        cfg = replace(self.cfg, zones=zones)
        readings, _ = Sensors(cfg).read(fixture(cfg), NIGHT)
        self.assertEqual(readings["office"].sources, 1)

    def test_recent_trend_is_used_and_clamped(self):
        sensors = Sensors(self.cfg)
        sensors.read(fixture(self.cfg, office=72), NIGHT)
        later = NIGHT + timedelta(minutes=2)
        readings, _ = sensors.read(fixture(self.cfg, later, office=73), later)
        self.assertAlmostEqual(readings["office"].slope_f_per_minute, .1)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.policy = Policy(self.cfg)
        self.settings = Settings.read({}, self.cfg)

    def decide(self, now=NIGHT, **temps):
        return self.policy.decide(fixture(self.cfg, now, **temps), now, self.settings)

    def test_cooling_hot_rooms_with_local_heating(self):
        plan = self.decide(office=74.2, guest=73, living=67.1)
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(set(plan.head_targets_f), {"bonus", "guest"})
        self.assertTrue(plan.heater_on)
        self.assertEqual(plan.fans[self.cfg.fans["office"]], 60)
        self.assertEqual(plan.fans[self.cfg.fans["primary"]], 60)

    def test_primary_fan_exact_boundaries_and_morning_band(self):
        for stamp, speed, upper in (
            ("2026-10-16T04:59:00+00:00", 0, 75),
            ("2026-10-16T05:00:00+00:00", 60, 69),
            ("2026-10-16T11:59:00+00:00", 60, 69),
            ("2026-10-16T12:00:00+00:00", 0, 70),
            ("2026-10-16T16:00:00+00:00", 0, 75),
        ):
            now = datetime.fromisoformat(stamp)
            plan = self.decide(now)
            self.assertEqual(plan.fans[self.cfg.fans["primary"]], speed)
            self.assertEqual(self.policy.band("primary", self.settings, now)[1], upper)

    def test_dst_repeated_hour_stays_night(self):
        for hour in (8, 9):  # Both 01:30 occurrences on the November fallback.
            now = datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc)
            self.assertEqual(self.decide(now).fans[self.cfg.fans["primary"]], 60)

    def test_primary_night_comfort_wins_over_other_comfort(self):
        # Start a heat call, then retain it within the band until the center is reached.
        plan = self.decide(primary=66.9, office=73, guest=73)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("primary", plan.head_targets_f)
        self.policy.sensors = Sensors(self.cfg)
        plan = self.decide(NIGHT + timedelta(minutes=1), primary=67.5, office=73, guest=73)
        self.assertIn("primary", plan.head_targets_f)

    def test_any_room_limit_outranks_primary_comfort(self):
        plan = self.decide(primary=69.5, guest=65)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("guest", plan.head_targets_f)

    def test_entertainment_cold_blocks_bonus_cooling(self):
        plan = self.decide(office=78, entertainment=66)
        self.assertNotIn("bonus", plan.head_targets_f)
        self.assertTrue(any("airflow/capacity conflict" in item for item in plan.warnings))

    def test_office_can_call_when_entertainment_is_comfortable(self):
        plan = self.decide(office=73, entertainment=71)
        self.assertIn("bonus", plan.head_targets_f)

    def test_local_heater_hysteresis_and_hot_probe_cutoff(self):
        self.assertTrue(self.decide(living=67.4).heater_on)
        self.assertTrue(self.decide(living=68.2).heater_on)
        self.assertFalse(self.decide(living=68.5).heater_on)
        self.assertFalse(self.decide(living=68).heater_on)
        states = fixture(self.cfg, living=67)
        states[self.cfg.zones["living"].probes[0].entity_id]["state"] = "71"
        self.assertFalse(self.policy.decide(states, NIGHT, self.settings).heater_on)

    def test_heater_internal_sensor_is_ignored(self):
        states = fixture(self.cfg, living=67)
        states["sensor.fixture_auxiliary_internal"] = {"state": "99", "attributes": {"unit_of_measurement": "°F"}}
        states[self.cfg.heater_entity]["attributes"]["current_temperature"] = 99
        self.assertTrue(self.policy.decide(states, NIGHT, self.settings).heater_on)

    def test_stale_living_sensors_turn_heater_off_without_blocking_guest(self):
        self.decide(living=67)
        states = fixture(self.cfg, living=67, guest=74)
        for probe in self.cfg.zones["living"].probes:
            states[probe.entity_id]["state"] = "unavailable"
        plan = self.policy.decide(states, NIGHT, self.settings)
        self.assertFalse(plan.heater_on)
        self.assertIn("guest", plan.head_targets_f)
        self.assertNotIn("living", plan.head_targets_f)

    def test_missing_entertainment_inhibits_shared_bonus_head(self):
        states = fixture(self.cfg, office=76)
        states.pop(self.cfg.zones["entertainment"].probes[0].entity_id)
        plan = self.policy.decide(states, NIGHT, self.settings)
        self.assertNotIn("bonus", plan.head_targets_f)

    def test_dwell_and_confirmation_prevent_mode_chatter(self):
        self.assertEqual(self.decide(office=73).mode, "cool")
        # Same current temperatures but no upward trend from the prior call.
        self.policy.sensors = Sensors(self.cfg)
        later = NIGHT + timedelta(seconds=30)
        # A comfort request safely inside the house reserve obeys dwell. A
        # primary reading of 66.9°F would correctly trigger house protection.
        self.assertEqual(self.decide(later, guest=69.5).mode, "off")
        later = NIGHT + timedelta(seconds=1250)
        self.assertEqual(self.decide(later, guest=69.5).mode, "heat")

    def test_actual_limit_can_bypass_software_dwell(self):
        self.decide(office=73)
        plan = self.decide(NIGHT + timedelta(seconds=30), guest=65)
        self.assertEqual(plan.mode, "heat")

    def test_heater_unavailable_living_can_request_mitsubishi(self):
        states = fixture(self.cfg, living=65)
        states[self.cfg.heater_entity]["state"] = "unavailable"
        plan = self.policy.decide(states, NIGHT, self.settings)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("living", plan.head_targets_f)
        self.assertFalse(plan.heater_on)

    def test_helper_changes_take_effect_without_restart(self):
        states = fixture(self.cfg, office=73)
        self.assertEqual(self.policy.decide(states, NIGHT, self.settings).mode, "cool")
        add_helpers(states)
        states[helper("office_target")]["state"] = "74"
        updated = Settings.read(states, self.cfg, strict=True)
        plan = self.policy.decide(states, NIGHT, updated)
        self.assertNotIn("bonus", plan.head_targets_f)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.ha = FakeHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")

    def step(self, plan, now):
        self.executor.step(plan, self.ha.states(), now)

    def activations(self):
        return [c for c in self.ha.calls if c[1] == "set_temperature" and c[2]["entity_id"] in self.cfg.heads.values()]

    def start_cooling(self):
        plan = Plan(mode="cool", head_targets_f={"bonus": 72})
        self.step(plan, 0)
        self.step(plan, 15)
        self.step(plan, 195)
        self.step(plan, 210)
        return plan

    def test_startup_waits_for_off_and_full_settling(self):
        for entity in self.cfg.heads.values():
            self.ha.data[entity]["state"] = "heat"
        plan = Plan(mode="cool", head_targets_f={"bonus": 72})
        self.step(plan, 0)
        self.assertFalse(self.activations())
        self.step(plan, 15)
        self.step(plan, 30)
        self.step(plan, 179)
        self.assertFalse(self.activations())
        self.step(plan, 180)
        self.assertEqual(len(self.activations()), 1)
        self.assertEqual(self.activations()[0][2]["hvac_mode"], "cool")

    def test_failed_off_ack_never_activates_any_head(self):
        entity = self.cfg.heads["guest"]
        self.ha.data[entity]["state"] = "heat"
        self.ha.ignore.add(entity)
        plan = Plan(mode="cool", head_targets_f={"bonus": 72})
        self.step(plan, 0)
        self.step(plan, 15)
        self.step(plan, 45)
        self.assertEqual(self.executor.phase, "stopping")
        self.assertIn(entity, self.executor.waiting_devices)
        self.assertFalse(self.activations())

    def test_direction_switch_has_new_off_barrier(self):
        self.start_cooling()
        self.ha.calls.clear()
        plan = Plan(mode="heat", head_targets_f={"primary": 68})
        self.step(plan, 300)
        self.step(plan, 315)
        self.step(plan, 330)
        self.step(plan, 479)
        self.assertFalse(self.activations())
        self.step(plan, 480)
        self.assertEqual(self.activations()[0][2]["hvac_mode"], "heat")
        self.assertTrue(all(s["state"] in ("off", "heat") for e, s in self.ha.data.items() if e in self.cfg.heads.values()))

    def test_same_mode_room_change_does_not_cycle_other_heads(self):
        plan = self.start_cooling()
        self.ha.calls.clear()
        plan.head_targets_f["guest"] = 72
        self.step(plan, 240)
        self.assertEqual(len(self.ha.calls), 1)
        self.assertEqual(self.ha.calls[0][2]["entity_id"], self.cfg.heads["guest"])

    def test_no_repeated_commands_after_ack(self):
        plan = self.start_cooling()
        self.ha.calls.clear()
        self.step(plan, 240)
        self.step(plan, 255)
        self.assertEqual(self.ha.calls, [])

    def test_external_mixed_mode_forces_off_before_new_calls(self):
        plan = self.start_cooling()
        self.ha.calls.clear()
        self.ha.data[self.cfg.heads["guest"]]["state"] = "heat"
        self.step(plan, 240)
        self.assertIn(self.executor.phase, ("stopping", "settling"))
        self.assertFalse(self.activations())

    def test_failed_activation_waits_for_local_off_recovery(self):
        entity = self.cfg.heads["bonus"]
        self.ha.ignore.add(entity)
        plan = self.start_cooling()
        self.step(plan, 240)
        self.assertIn(entity, self.executor.waiting_devices)
        self.assertEqual(self.executor.phase, "running")

    def test_heater_exaggerated_target_and_explicit_off(self):
        plan = Plan(heater_on=True)
        self.step(plan, 0)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["attributes"]["temperature"], 86)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "heat")
        self.step(plan, 15)
        self.step(Plan(heater_on=False), 30)
        self.assertEqual(self.ha.data[self.cfg.heater_entity]["state"], "off")

    def test_celsius_setpoints_and_supported_range(self):
        entity = self.cfg.heater_entity
        self.ha.data[entity]["attributes"].update(min_temp=16, max_temp=32, target_temp_step=.5)
        executor = Executor(self.cfg, self.ha, "°C")
        self.assertEqual(executor.target(entity, 86, self.ha.states(), heater=True), 30)
        with self.assertRaises(ControlFault):
            executor.target(entity, 100, self.ha.states(), heater=True)

    def test_shutdown_attempts_remaining_devices_after_one_failure(self):
        self.ha.service = Mock(side_effect=[RuntimeError("failure")] + [None] * 8)
        failures = self.executor.shutdown()
        self.assertEqual(failures, [self.cfg.heater_entity])
        self.assertEqual(self.ha.service.call_count, 9)


class ConfigurationTests(unittest.TestCase):
    def test_only_one_live_process_can_hold_the_local_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.lock"
            with live_lock(path):
                with self.assertRaisesRegex(RuntimeError, "Another live"):
                    with live_lock(path):
                        self.fail("Acquired duplicate lock")
            with live_lock(path):
                pass

    def test_live_requires_commissioning_flags(self):
        with self.assertRaises(ValueError):
            config().validate(live=True)
        replace(config(), equipment_timing_confirmed=True, heater_setpoint_confirmed=True).validate(live=True)

    def test_invalid_helper_values_are_rejected(self):
        cfg = config()
        states = fixture(cfg)
        add_helpers(states)
        for value in ("nan", "100", "unavailable"):
            states[helper("office_target")]["state"] = value
            with self.assertRaises(ValueError):
                Settings.read(states, cfg, strict=True)

    def test_default_package_preserves_edits_and_disables_control_on_ha_restart(self):
        generated = package(config())
        self.assertFalse(generated["input_boolean"]["hp_enabled"]["initial"])
        self.assertNotIn("initial", generated["input_boolean"]["hp_heater_owned"])
        self.assertTrue(all("initial" not in value for value in generated["input_number"].values()))
        self.assertNotIn("sensor.fixture_auxiliary_internal", json.dumps(generated))

    def test_watchdog_is_required_for_live_preflight(self):
        cfg = config()
        states = fixture(cfg)
        add_helpers(states)
        executor = Executor(cfg, FakeHA(states), "°F")
        preflight(cfg, states, executor)
        states["automation.hp_heater_watchdog"]["state"] = "off"
        with self.assertRaises(ControlFault):
            preflight(cfg, states, executor)

    def test_outdated_watchdog_mapping_is_rejected(self):
        cfg = config()
        states = fixture(cfg)
        add_helpers(states)
        changed = replace(cfg, heater_cutoff_f=69.5)
        with self.assertRaisesRegex(ControlFault, "mapping differs"):
            preflight(changed, states, Executor(changed, FakeHA(states), "°F"))

    def test_shadow_client_rejects_all_service_calls_before_network(self):
        ha = HomeAssistant("http://ha.invalid:8123", "test-token", live=False)
        ha.opener = Mock()
        with self.assertRaises(HAError):
            ha.service("climate", "set_hvac_mode", entity_id="climate.example", hvac_mode="heat")
        ha.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
