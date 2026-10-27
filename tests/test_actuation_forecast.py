"""Completed conditioning is not evidence of continuing passive room drift."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.executor import Executor, HeadCycle
from house_climate.head_guard import HeadGuard, protection_requested
from house_climate.sensors import Reading, forecast_slope
from house_climate.temperature_guard import opposite_limit_reached
from tests.test_controller import FakeHA
from tests.test_rooms import NOW, states


def configuration(target=68, **changes):
    raw = {"schema_version": 2, "timezone": "Etc/UTC",
           "heads": {"solo": "climate.test_head"},
           "rooms": {"room": {"head": "solo", "sensors": [
               {"entity_id": "sensor.test_one"}, {"entity_id": "sensor.test_two"}],
               "profile": {"target_f": target, "low_f": target - 1, "high_f": target + 1}}},
           "forecast_minutes": 10, "decision_interval_seconds": 90,
           "mode_hold_seconds": 1, "switch_confirmation_seconds": 1,
           "head_minimum_run_seconds": 0, "head_minimum_off_seconds": 0,
           "head_cool_call_offset_f": 5, "head_heat_call_offset_f": 5,
           "head_cool_min_f": {"climate.test_head": 65}}
    raw.update(changes)
    return Config.from_dict(raw)


class ActuationForecastTests(unittest.TestCase):
    def setup_controller(self, target=68, **changes):
        self.cfg = configuration(target, **changes)
        self.cadence = DecisionCadence(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        self.data = states(self.cfg)
        self.head = self.cfg.heads["solo"]

    def tick(self, seconds, mode, temperature, *, missing=False, split=0):
        now = NOW + timedelta(seconds=seconds)
        self.data[self.head]["state"] = mode
        for index, probe in enumerate(self.cfg.zones["room"].probes):
            self.data[probe.entity_id].update(
                state="unavailable" if missing else str(temperature + (-split if index == 0 else split)),
                last_reported=now.isoformat())
        return self.cadence.decide(self.data, now, self.settings, monotonic_now=seconds)

    def cooled_to_target(self):
        self.setup_controller()
        self.tick(0, "off", 70)
        self.tick(90, "cool", 69.5)
        self.tick(180, "cool", 68.5)
        return self.tick(270, "cool", 68, split=.18)

    def heated_to_target(self):
        self.setup_controller(73)
        self.tick(0, "off", 71.5)
        self.tick(90, "heat", 72)
        self.tick(180, "heat", 72.6)
        return self.tick(270, "heat", 73, split=.18)

    def test_cooling_target_then_late_off_and_stable_probes_do_not_request_heat(self):
        stopped = self.cooled_to_target()
        self.assertEqual(stopped.mode, "off")
        self.assertEqual(stopped.rooms["room"]["slope_f_per_minute"], -.1)
        for seconds, mode in ((285, "cool"), (300, "off"), (330, "off"), (360, "off"), (450, "off")):
            with self.subTest(seconds=seconds):
                plan = self.tick(seconds, mode, 68, split=.18)
                reading = plan.rooms["room"]
                self.assertEqual(reading["coast_after_mode"], "cool")
                self.assertEqual(forecast_slope(reading, "heat"), 0)
                self.assertFalse(protection_requested(self.cfg, reading, "heat"))
                self.assertFalse(plan.room_targets["room"]["heat_requested"])
                self.assertNotEqual(plan.mode, "heat")

    def test_heating_mirror_does_not_forecast_cooling_at_target(self):
        self.assertEqual(self.heated_to_target().mode, "off")
        for seconds, mode in ((285, "heat"), (300, "off"), (360, "off"), (450, "off")):
            plan = self.tick(seconds, mode, 73, split=.18)
            reading = plan.rooms["room"]
            self.assertEqual(reading["slope_f_per_minute"], .1)
            self.assertEqual(forecast_slope(reading, "cool"), 0)
            self.assertFalse(protection_requested(self.cfg, reading, "cool"))
            self.assertNotEqual(plan.mode, "cool")

    def test_unrequested_active_bounce_after_off_does_not_revive_old_trend(self):
        self.cooled_to_target()
        for seconds, mode in ((300, "off"), (315, "cool"), (330, "off"), (390, "off")):
            with self.subTest(seconds=seconds):
                plan = self.tick(seconds, mode, 68, split=.18)
                self.assertEqual(plan.rooms["room"]["coast_after_mode"], "cool")
                self.assertEqual(forecast_slope(plan.rooms["room"], "heat"), 0)
                self.assertNotEqual(plan.mode, "heat")

    def test_genuine_post_stop_cooling_can_forecast_cold_protection(self):
        self.cooled_to_target()
        self.tick(300, "off", 68)
        before_confirmation = self.tick(330, "off", 67.95)
        self.assertEqual(forecast_slope(before_confirmation.rooms["room"], "heat"), 0)
        plan = self.tick(360, "off", 67.8)
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(plan.rooms["room"]["coast_slope_f_per_minute"], -.1)
        self.assertGreater(plan.rooms["room"]["confirmed_low"], 67)
        self.assertIn("solo", plan.head_protections)

    def test_genuine_post_stop_warming_can_forecast_hot_protection(self):
        self.heated_to_target()
        self.tick(300, "off", 73)
        plan = self.tick(360, "off", 73.2)
        self.assertEqual(plan.mode, "cool")
        self.assertEqual(plan.rooms["room"]["coast_slope_f_per_minute"], .1)
        self.assertLess(plan.rooms["room"]["confirmed_high"], 74)

    def test_unknown_head_cannot_supply_an_off_anchor(self):
        self.cooled_to_target()
        self.tick(300, "unavailable", 68)
        unknown = self.tick(360, "unavailable", 67.8)
        self.assertEqual(forecast_slope(unknown.rooms["room"], "heat"), 0)
        restored = self.tick(375, "off", 67.8)
        self.assertEqual(forecast_slope(restored.rooms["room"], "heat"), 0)
        self.assertNotIn("solo", restored.head_targets_f)
        continuing = self.tick(435, "off", 67.6)
        self.assertTrue(protection_requested(self.cfg, continuing.rooms["room"], "heat"))

    def test_missing_samples_do_not_manufacture_post_stop_drift(self):
        self.cooled_to_target()
        self.tick(300, "off", 68)
        self.tick(360, "off", 67.8)
        self.tick(375, "off", 67.8, missing=True)
        restored = self.tick(390, "off", 67.8)
        self.assertEqual(restored.rooms["room"]["slope_f_per_minute"], 0)
        self.assertEqual(forecast_slope(restored.rooms["room"], "heat"), 0)
        stable = self.tick(450, "off", 67.8)
        self.assertEqual(forecast_slope(stable.rooms["room"], "heat"), 0)

    def test_off_without_climate_metadata_cannot_supply_passive_evidence(self):
        self.cooled_to_target()
        modes = self.data[self.head]["attributes"].pop("hvac_modes")
        self.tick(300, "off", 68)
        incomplete = self.tick(360, "off", 67.8)
        self.assertEqual(forecast_slope(incomplete.rooms["room"], "heat"), 0)
        self.data[self.head]["attributes"]["hvac_modes"] = modes
        restored = self.tick(375, "off", 67.8)
        self.assertEqual(forecast_slope(restored.rooms["room"], "heat"), 0)

    def test_actual_confirmed_house_reserve_still_requests_immediate_heat(self):
        self.cooled_to_target()
        plan = self.tick(285, "cool", 66.9)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("solo", plan.head_protections)
        self.assertTrue(opposite_limit_reached(plan.rooms["room"], "cool", 66, 75))

    def test_actual_hot_reserve_still_requests_immediate_cooling(self):
        self.heated_to_target()
        plan = self.tick(285, "heat", 74.1)
        self.assertEqual(plan.mode, "cool")
        self.assertIn("solo", plan.head_protections)
        self.assertTrue(opposite_limit_reached(plan.rooms["room"], "heat", 66, 75))

    def test_confirmed_cold_probe_protection_is_not_erased_by_stable_median(self):
        self.cooled_to_target()
        for seconds in (300, 330, 360):
            plan = self.tick(seconds, "off", 68, split=1.5)
        reading = plan.rooms["room"]
        self.assertEqual(reading["temperature"], 68)
        self.assertEqual(forecast_slope(reading, "heat"), 0)
        self.assertEqual(reading["confirmed_low"], 66.5)
        self.assertTrue(protection_requested(self.cfg, reading, "heat"))
        self.assertEqual(plan.mode, "heat")

    def test_limits_only_uses_its_actual_release_target_not_band_midpoint(self):
        for mode, temperatures in (("cool", (77, 75.5, 74.5, 73.5)),
                                   ("heat", (64, 65.5, 66.5, 67.5))):
            with self.subTest(mode=mode):
                self.setup_controller()
                zone = self.cfg.zones["room"]
                self.cfg = replace(self.cfg, zones={"room": replace(zone, profile=replace(zone.profile, comfort_enabled=False))})
                self.cadence = DecisionCadence(self.cfg)
                self.settings = Settings.read({}, self.cfg)
                for seconds, temperature in zip((0, 90, 180, 270), temperatures):
                    plan = self.tick(seconds, "off" if seconds == 0 else mode, temperature)
                self.assertEqual(plan.mode, "off")
                self.assertEqual(plan.rooms["room"]["coast_after_mode"], mode)

    def test_raw_two_minute_stop_projection_is_not_replaced_by_coast_slope(self):
        self.cooled_to_target()
        plan = self.tick(285, "cool", 67.3, split=.15)
        reading = plan.rooms["room"]
        self.assertEqual(forecast_slope(reading, "heat"), 0)
        self.assertEqual(reading["slope_f_per_minute"], -.1)
        self.assertTrue(opposite_limit_reached(reading, "cool", 66, 75))

    def test_shared_warm_room_keeps_hot_forecast_while_another_room_heats(self):
        cfg = configuration(72)
        zone = cfg.zones["room"]
        other = replace(zone, probes=(replace(zone.probes[0], entity_id="sensor.test_cold"),))
        cfg = replace(cfg, zones={"room": zone, "other": other})
        cadence = DecisionCadence(cfg)
        settings = Settings.read({}, cfg)
        data = states(cfg)
        for elapsed, warm in ((0, 73), (90, 73.1), (180, 73.3)):
            now = NOW + timedelta(seconds=elapsed)
            for probe in zone.probes:
                data[probe.entity_id].update(state=str(warm), last_reported=now.isoformat())
            data["sensor.test_cold"].update(state="66.9", last_reported=now.isoformat())
            data[cfg.heads["solo"]]["state"] = "off" if elapsed == 0 else "heat"
            plan = cadence.decide(data, now, settings, monotonic_now=elapsed)
        self.assertIsNone(plan.rooms["room"]["coast_after_mode"])
        self.assertTrue(protection_requested(cfg, plan.rooms["room"], "cool"))

    def test_cleared_forecast_cancels_call_before_minimum_off_expires(self):
        self.setup_controller(head_minimum_off_seconds=180)
        ha = FakeHA(self.data)
        executor = Executor(self.cfg, ha, "°F")
        # Isolate local minimum-Off dispatch from the separately tested shared
        # startup/changeover barrier.
        executor.phase, executor.direction = "running", "heat"
        executor.head_cycles[self.head] = HeadCycle(mode="off", off_since=0)
        self.tick(0, "off", 67.9)
        pending = self.tick(60, "off", 67.8)
        self.assertEqual(pending.mode, "heat")
        self.assertIn("solo", pending.head_targets_f)
        ha.data = self.data.copy()
        executor.step(pending, ha.states(), 60)
        self.assertIn(self.head, executor.cycle_holds)
        cleared = self.tick(105, "off", 67.85)
        self.assertNotIn("solo", cleared.head_targets_f)
        self.assertIn("cleared before activation", cleared.inhibited_heads["solo"])
        self.assertLess(cleared.rooms["room"]["temperature"], 68)
        self.assertNotIn(("room", "heat"), self.cadence.policy.forecast_pending)
        for seconds in (105, 150, 180, 210):
            plan = self.tick(seconds, "off", 67.85)
            ha.data = self.data.copy()
            executor.step(plan, ha.states(), seconds)
        self.assertFalse(any(service == "set_temperature" for _, service, _ in ha.calls))

    def test_new_forecast_origin_does_not_inherit_another_origins_started_latch(self):
        cfg = configuration(72)
        zone = cfg.zones["room"]
        cfg = replace(cfg, zones={"room": zone, "other": replace(zone)})
        guard = HeadGuard(cfg)
        targets = {name: {"low_f": 71, "high_f": 73, "target_f": 72,
                          "comfort_enabled": True} for name in cfg.zones}
        def reading(temperature, slope=0):
            return Reading(temperature, temperature, temperature, temperature, temperature, slope, 2)
        head = cfg.heads["solo"]
        guard.observe({"room": reading(66.9), "other": reading(68)}, targets,
                      {head: {"state": "heat"}})
        guard.observe({"room": reading(70), "other": reading(67.8, -.1)}, targets,
                      {head: {"state": "off"}})
        protection = guard.protections["solo", "heat"]
        self.assertEqual(protection["rooms"], ["other", "room"])
        self.assertEqual(protection["started_rooms"], ["room"])
        guard.observe({"room": reading(72), "other": reading(67.8)}, targets,
                      {head: {"state": "off"}})
        self.assertNotIn(("solo", "heat"), guard.protections)

    def test_new_shared_run_does_not_inherit_a_completed_own_coast(self):
        cases = (("heat", 73, (71.5, 72, 72.6, 73), 70, (73.2, 73.4, 73.6), "cool"),
                 ("cool", 68, (70, 69.5, 68.5, 68), 73.1, (67.9, 67.6, 67.5), "heat"))
        for mode, target, original, other_temperature, drifting, opposite in cases:
            with self.subTest(mode=mode):
                cfg = configuration(target)
                zone = cfg.zones["room"]
                other = replace(zone, probes=(replace(zone.probes[0], entity_id="sensor.test_other"),),
                                profile=replace(zone.profile, target_f=72, low_f=71, high_f=73))
                cfg = replace(cfg, zones={"room": zone, "other": other})
                cadence, settings, data = DecisionCadence(cfg), Settings.read({}, cfg), states(cfg)
                def tick(seconds, observed, own, remote=72):
                    now = NOW + timedelta(seconds=seconds)
                    for probe in zone.probes:
                        data[probe.entity_id].update(state=str(own), last_reported=now.isoformat())
                    data["sensor.test_other"].update(state=str(remote), last_reported=now.isoformat())
                    data[cfg.heads["solo"]]["state"] = observed
                    return cadence.decide(data, now, settings, monotonic_now=seconds)
                for seconds, own in zip((0, 90, 180, 270), original):
                    plan = tick(seconds, "off" if seconds == 0 else mode, own)
                self.assertEqual(plan.rooms["room"]["coast_after_mode"], mode)
                tick(360, "off", target)
                tick(930, "off", target)
                tick(1080, "off", target, other_temperature)
                for seconds, own in zip((1170, 1260, 1350), drifting):
                    plan = tick(seconds, mode, own, other_temperature)
                reading = plan.rooms["room"]
                self.assertTrue(reading["post_stop_forecast"])
                self.assertTrue(protection_requested(cfg, reading, opposite))
                self.assertFalse(opposite_limit_reached(reading, mode, cfg.low_f, cfg.high_f))
                self.assertEqual(plan.mode, opposite)

    def test_forecast_that_becomes_actual_keeps_ordinary_hysteresis(self):
        self.setup_controller(head_minimum_off_seconds=900)
        zone = self.cfg.zones["room"]
        self.cfg = replace(self.cfg, zones={"room": replace(zone, profile=replace(zone.profile, low_f=67.5))})
        self.cadence = DecisionCadence(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        self.tick(0, "off", 67.9)
        self.tick(60, "off", 67.8)
        self.assertIn(("room", "heat"), self.cadence.policy.forecast_pending)
        # Actual comfort demand appears between full decisions while the head
        # is still waiting. That is stronger evidence than the initial forecast.
        self.tick(75, "off", 67.4)
        self.assertNotIn(("room", "heat"), self.cadence.policy.forecast_pending)
        for seconds in (90, 180, 270, 360):
            plan = self.tick(seconds, "off", 67.6)
        self.assertEqual(plan.mode, "heat")
        self.assertIn("solo", plan.head_targets_f)
        self.assertNotIn("solo", plan.head_protections)
        self.assertTrue(plan.room_targets["room"]["heat_requested"])

    def test_short_shared_restart_uses_post_stop_evidence_not_completed_run(self):
        for fresh_drift in (False, True):
            with self.subTest(fresh_drift=fresh_drift):
                cfg = configuration()
                zone = cfg.zones["room"]
                other = replace(zone, probes=(replace(zone.probes[0], entity_id="sensor.test_other"),),
                                profile=replace(zone.profile, target_f=69, low_f=68, high_f=70))
                cfg = replace(cfg, zones={"room": zone, "other": other})
                cadence, settings, data = DecisionCadence(cfg), Settings.read({}, cfg), states(cfg)
                def tick(seconds, mode, own, remote=69):
                    now = NOW + timedelta(seconds=seconds)
                    for probe in zone.probes:
                        data[probe.entity_id].update(state=str(own), last_reported=now.isoformat())
                    data["sensor.test_other"].update(state=str(remote), last_reported=now.isoformat())
                    data[cfg.heads["solo"]]["state"] = mode
                    return cadence.decide(data, now, settings, monotonic_now=seconds)
                for seconds, own in ((0, 70), (90, 69.5), (180, 68.5), (270, 68)):
                    tick(seconds, "off" if seconds == 0 else "cool", own)
                tick(285, "off", 68)
                tick(360, "off", 68, 70.1)
                resumed = tick(375, "cool", 68, 70.1)
                self.assertEqual(resumed.mode, "cool")
                self.assertEqual(resumed.rooms["room"]["slope_f_per_minute"], -.1)
                self.assertEqual(forecast_slope(resumed.rooms["room"], "heat"), 0)
                if fresh_drift:
                    changed = tick(435, "cool", 67.8, 70.1)
                    self.assertTrue(protection_requested(cfg, changed.rooms["room"], "heat"))
                    self.assertEqual(changed.mode, "heat")
                    self.assertFalse(opposite_limit_reached(changed.rooms["room"], "cool", 66, 75))
                else:
                    for seconds in (390, 465, 555):
                        stopped = tick(seconds, "off", 68)
                        self.assertNotEqual(stopped.mode, "heat")
                        self.assertEqual(forecast_slope(stopped.rooms["room"], "heat"), 0)

    def test_ongoing_same_direction_forecast_survives_unrequested_off_at_stop_target(self):
        self.setup_controller()
        zone = self.cfg.zones["room"]
        self.cfg = replace(self.cfg, zones={"room": replace(zone, profile=replace(zone.profile, comfort_enabled=False))})
        self.cadence = DecisionCadence(self.cfg)
        self.settings = Settings.read({}, self.cfg)
        self.tick(0, "off", 73.2)
        self.tick(60, "off", 73.4)
        active = self.tick(90, "cool", 73.5)
        self.assertEqual(active.mode, "cool")
        self.assertTrue(protection_requested(self.cfg, active.rooms["room"], "cool"))
        interrupted = self.tick(105, "off", 73.5)
        self.assertTrue(protection_requested(self.cfg, interrupted.rooms["room"], "cool"))
        self.assertIn("solo", interrupted.head_targets_f)


if __name__ == "__main__":
    unittest.main()
