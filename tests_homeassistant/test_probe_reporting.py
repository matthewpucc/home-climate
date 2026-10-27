"""On-change probe readings stay usable without bypassing native heat safeguards."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.comfort import RoomComfort
from custom_components.house_climate.controller.config import Config, Probe, Settings
from custom_components.house_climate.controller.policy import Policy
from tests_homeassistant import test_native as native_fixtures


class ProbeReportingTests(unittest.IsolatedAsyncioTestCase):
    handle = native_fixtures.NativeTests.handle

    async def asyncSetUp(self):
        await native_fixtures.NativeTests.asyncSetUp(self)
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(hours=3)

    async def asyncTearDown(self):
        try:
            # These tests evaluate real cached-state interlocks; they never
            # claim equipment or dispatch commands to a physical integration.
            self.assertEqual(self.calls, [])
        finally:
            await native_fixtures.NativeTests.asyncTearDown(self)

    def controls(self, report_on_change=True):
        cfg = Config.from_dict(self.cfg.to_dict())
        room_id = cfg.auxiliary_room
        room = cfg.zones[room_id]
        flags = ((report_on_change,) * len(room.probes)
                 if isinstance(report_on_change, bool) else report_on_change)
        room = replace(room, probes=tuple(
            replace(probe, report_on_change=flag)
            for probe, flag in zip(room.probes, flags, strict=True)))
        cfg = replace(cfg, zones={**cfg.zones, room_id: room})
        return NativeControls(self.hass, cfg, True, "probe_reporting", lambda: None)

    def readings(self, controls, value=68, when=None, unit="°F"):
        return {probe.entity_id: {
            "state": str(value),
            "attributes": {"unit_of_measurement": unit},
            "last_reported": (when or self.old).isoformat(),
        } for probe in controls.config.zones[controls.config.auxiliary_room].probes}

    async def test_old_on_change_values_remain_safe_but_periodic_values_expire(self):
        on_change = self.controls()
        periodic = self.controls(False)
        for value in (65, 68):
            with self.subTest(value=value):
                states = self.readings(on_change, value)
                self.assertTrue(on_change.probes_safe(states))
                self.assertFalse(periodic.probes_safe(states))
                safe_entity = on_change.config.entity("auxiliary_probe_safe", "binary_sensor")
                self.assertEqual(on_change.overlay(states)[safe_entity]["state"], "on")
                self.assertEqual(periodic.overlay(states)[safe_entity]["state"], "off")

    async def test_one_old_hot_probe_still_vetoes_heat_immediately(self):
        controls = self.controls()
        states = self.readings(controls, 65)
        hot = next(iter(states))
        states[hot]["state"] = "70.0"
        # No outlier confirmation or median can hide the independent cutoff.
        self.assertFalse(controls.probes_safe(states))
        states[hot]["state"] = "69.9"
        self.assertTrue(controls.probes_safe(states))

    async def test_on_change_does_not_accept_unavailable_invalid_or_missing_readings(self):
        controls = self.controls()
        for value in ("unavailable", "unknown", "NaN", "inf", "not a temperature"):
            with self.subTest(value=value):
                self.assertFalse(controls.probes_safe(self.readings(controls, value)))
        self.assertFalse(controls.probes_safe({}))
        states = self.readings(controls)
        for state in states.values():
            state.pop("last_reported")
        self.assertFalse(controls.probes_safe(states))

    async def test_future_reports_remain_invalid_and_units_still_convert(self):
        controls = self.controls()
        self.assertFalse(controls.probes_safe(
            self.readings(controls, 68, self.now + timedelta(minutes=5))))
        self.assertTrue(controls.probes_safe(self.readings(controls, 20, unit="°C")))
        self.assertFalse(controls.probes_safe(self.readings(controls, 21.2, unit="°C")))
        self.assertFalse(controls.probes_safe(self.readings(controls, 68, unit="invalid")))

    async def test_reporting_policy_is_per_probe_and_one_valid_probe_remains_usable(self):
        controls = self.controls((True, False, False))
        states = self.readings(controls, 68)
        self.assertTrue(controls.probes_safe(states))
        first, second, third = states.values()
        second["state"], third["state"] = "unavailable", "unknown"
        self.assertTrue(controls.probes_safe(states))
        first["state"] = "70"
        self.assertFalse(controls.probes_safe(states))
        first["state"] = "unavailable"
        self.assertFalse(controls.probes_safe(states))

    async def test_policy_fuses_old_on_change_external_probe_with_fresh_head_probe(self):
        source = Config.from_dict(self.cfg.to_dict())
        external = Probe("sensor.reporting_external", "external", report_on_change=True)
        head = Probe(source.heads["guest"], "head", attribute="current_temperature", unit="°F")
        room = replace(source.zones["guest"], probes=(external, head))
        cfg = replace(source, heads={"guest": source.heads["guest"]},
                      zones={"guest": room}, fans={}, fan_controls={},
                      heater_entity=None, auxiliary_room=None)
        states = {
            external.entity_id: {
                "state": "73.6", "last_reported": self.old.isoformat(),
                "attributes": {"unit_of_measurement": "°F"},
            },
            head.entity_id: {
                "state": "off", "last_reported": self.now.isoformat(),
                "attributes": {"current_temperature": 68, "hvac_modes": ["off", "heat", "cool"]},
            },
        }
        plan = Policy(cfg).decide(states, self.now, Settings.read({}, cfg))
        self.assertAlmostEqual(plan.rooms["guest"]["temperature"], 70.8)
        self.assertEqual(plan.rooms["guest"]["sources"], 2)
        self.assertFalse(any(external.entity_id in warning for warning in plan.warnings))

        periodic_room = replace(room, probes=(replace(external, report_on_change=False), head))
        periodic = replace(cfg, zones={"guest": periodic_room})
        fallback = Policy(periodic).decide(states, self.now, Settings.read({}, periodic))
        self.assertEqual(fallback.rooms["guest"]["temperature"], 68)
        self.assertEqual(fallback.rooms["guest"]["sources"], 1)
        self.assertTrue(any(external.entity_id in warning for warning in fallback.warnings))

    async def test_default_reporting_policy_preserves_pre_upgrade_comfort_hold(self):
        cfg = Config.from_dict(self.cfg.to_dict())
        room_id = "guest"
        room = cfg.zones[room_id]
        old_room = asdict(room)
        old_room.pop("windows", None)  # This field did not exist in the saved format.
        for probe in old_room["probes"]:
            probe.pop("report_on_change", None)
        old_payload = {
            "room": old_room, "head": cfg.heads[room.head],
            "timezone": self.hass.config.time_zone,
            "limits": [cfg.low_f, cfg.high_f],
            "auxiliary_cutoff": cfg.heater_cutoff_f if room_id == cfg.auxiliary_room else None,
        }
        fingerprint = hashlib.sha256(json.dumps(
            old_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manager = RoomComfort(self.hass, cfg, "probe_saved_hold", lambda: None)
        try:
            self.assertEqual(manager._fingerprint(room_id), fingerprint)
            await manager.store.async_save({"rooms": {room_id: {
                "low_f": 73, "high_f": 74, "target_f": 73.5,
                "expires_at": None, "fingerprint": fingerprint,
            }}})
            await manager.async_load()
            restored = manager.effective(room_id)
            self.assertTrue(restored["hold"])
            self.assertEqual(restored["target_f"], 73.5)
            self.assertEqual((restored["low_f"], restored["high_f"]), (73, 74))

            changed_room = replace(room, probes=(
                replace(room.probes[0], report_on_change=True), *room.probes[1:]))
            changed = replace(cfg, zones={**cfg.zones, room_id: changed_room})
            self.assertNotEqual(manager._fingerprint(room_id, changed), fingerprint)
        finally:
            await manager.async_close()


if __name__ == "__main__":
    unittest.main()
