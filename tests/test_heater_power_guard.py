"""Dedicated-watt supervision without HA or physical equipment."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from tests import test_power_guard as fixtures


power = fixtures.power
B = fixtures.B
WATTS = "sensor.fixture_heater_power"


def watt_bindings(**changes):
    return replace(B, heater_current=None, heater_power=WATTS, **changes)


def report(clock, value, unit="W", *, age=0, **attributes):
    return {"state": str(value),
            "attributes": {"unit_of_measurement": unit, **attributes},
            "last_reported": datetime.fromtimestamp(clock.wall - age, timezone.utc).isoformat()}


class HeaterWattPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = fixtures.Clock()
        self.bindings = watt_bindings()
        self.states = fixtures.measurements(self.clock)
        self.states.pop(B.heater_current)
        self.states[WATTS] = report(self.clock, 5.5)

    def reading(self):
        return power.evaluate_power(self.states, self.bindings, self.clock.wall)

    def test_native_watts_and_kilowatts_have_identical_load_semantics(self):
        for value, unit in ((5.5, "W"), (.0055, "kW")):
            with self.subTest(unit=unit):
                self.states[WATTS] = report(self.clock, value, unit)
                reading = self.reading()
                self.assertEqual(reading.heater_w, 5.5)
                self.assertIsNone(reading.heater_a)
                self.assertIsNone(reading.issue)
                self.assertTrue(reading.safe_to_rearm)

    def test_low_power_boundary_is_separate_from_a_heating_trip(self):
        for value, safe in ((0, True), (5.5, True), (99.999, True),
                            (100, False), (1250, False)):
            with self.subTest(watts=value):
                self.states[WATTS] = report(self.clock, value)
                reading = self.reading()
                # High heater watts alone is expected while heating. Wall,
                # total-output and battery limits provide independent trips.
                self.assertIsNone(reading.issue)
                self.assertEqual(reading.safe_to_rearm, safe)

    def test_no_dedicated_watt_report_is_not_fresh_zero(self):
        self.states.pop(WATTS)
        reading = self.reading()
        self.assertTrue(reading.telemetry_missing)
        self.assertIsNone(reading.heater_w)
        self.assertFalse(reading.safe_to_rearm)

    def test_stale_low_and_stale_high_are_both_unknown(self):
        for value in (0, 5.5, 1400):
            with self.subTest(watts=value):
                self.states[WATTS] = report(self.clock, value, age=180.001)
                reading = self.reading()
                self.assertTrue(reading.telemetry_missing)
                self.assertIn(WATTS, reading.issue)
                self.assertFalse(reading.safe_to_rearm)

    def test_current_report_not_value_change_defines_freshness(self):
        self.states[WATTS] = report(self.clock, 5.5)
        self.states[WATTS]["last_updated"] = datetime.fromtimestamp(
            self.clock.wall - 900, timezone.utc).isoformat()
        self.assertTrue(self.reading().safe_to_rearm)
        self.states[WATTS]["last_reported"] = datetime.fromtimestamp(
            self.clock.wall - 181, timezone.utc).isoformat()
        self.states[WATTS]["last_updated"] = datetime.fromtimestamp(
            self.clock.wall, timezone.utc).isoformat()
        self.assertTrue(self.reading().telemetry_missing)

    def test_last_updated_fallback_keeps_reports_without_last_reported_supported(self):
        state = report(self.clock, 5.5)
        state["last_updated"] = state.pop("last_reported")
        self.states[WATTS] = state
        self.assertTrue(self.reading().safe_to_rearm)
        state["last_updated"] = datetime.fromtimestamp(
            self.clock.wall - 181, timezone.utc).isoformat()
        self.assertTrue(self.reading().telemetry_missing)

    def test_freshness_boundary_and_future_reports_are_checked(self):
        self.states[WATTS] = report(self.clock, 5.5, age=180)
        self.assertTrue(self.reading().safe_to_rearm)
        self.states[WATTS] = report(self.clock, 5.5, age=-5.001)
        self.assertTrue(self.reading().telemetry_missing)

    def test_invalid_or_wrong_unit_watts_fail_closed(self):
        for value, unit in ((5, "A"), (5, "VA"), (5, "Wh"),
                            ("NaN", "W"), ("inf", "W"), (-1, "W"),
                            ("unknown", "W"), ("unavailable", "W")):
            with self.subTest(value=value, unit=unit):
                self.states[WATTS] = report(self.clock, value, unit)
                self.assertTrue(self.reading().telemetry_missing)
                self.assertFalse(self.reading().safe_to_rearm)

    def test_restored_or_assumed_watts_cannot_authorize_rearm(self):
        for flag in ("restored", "assumed_state"):
            with self.subTest(flag=flag):
                self.states[WATTS] = report(self.clock, 0, **{flag: True})
                self.assertTrue(self.reading().telemetry_missing)
                self.assertFalse(self.reading().safe_to_rearm)

    def test_fresh_output_and_battery_trip_are_not_hidden_by_missing_watts(self):
        self.states[WATTS] = report(self.clock, "unavailable")
        for entity, value, reason in ((B.output_power, 2000, "output spike"),
                                      (B.battery, 40, "reserve")):
            with self.subTest(entity=entity):
                original = self.states[entity]
                self.states[entity] = report(self.clock, value,
                                             "%" if entity == B.battery else "W")
                self.assertIn(reason, self.reading().issue)
                self.assertFalse(self.reading().safe_to_rearm)
                self.states[entity] = original

    def test_unbound_stale_amp_sensor_cannot_poison_selected_watts(self):
        self.states[B.heater_current] = report(self.clock, "unavailable", "A", age=900)
        self.assertTrue(self.reading().safe_to_rearm)
        self.assertIsNone(self.reading().heater_a)

    def test_legacy_current_and_explicitly_absent_meter_remain_supported(self):
        states = fixtures.measurements(self.clock)
        self.assertTrue(power.evaluate_power(states, B, self.clock.wall).safe_to_rearm)
        states[B.heater_current] = report(self.clock, 1, "A")
        self.assertFalse(power.evaluate_power(states, B, self.clock.wall).safe_to_rearm)
        states[B.heater_current] = report(self.clock, 5.5, "W")
        self.assertTrue(power.evaluate_power(states, B, self.clock.wall).telemetry_missing)
        absent = replace(B, heater_current=None)
        self.assertTrue(power.evaluate_power(states, absent, self.clock.wall).safe_to_rearm)


class WattHass(fixtures.FakeHass):
    """Optional independent meter feedback alongside the native service ack."""
    def __init__(self, clock):
        super().__init__(clock)
        self.data.pop(B.heater_current)
        self.data[WATTS] = report(clock, 5.5)
        self.post_off_watts = None

    async def service(self, domain, service, body, blocking):
        await super().service(domain, service, body, blocking)
        if domain == "climate" and not self.ignore_climate and self.post_off_watts is not None:
            self.set(WATTS, self.post_off_watts, {"unit_of_measurement": "W"})


class HeaterWattAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = fixtures.Clock()
        self.hass = WattHass(self.clock)
        self.bindings = watt_bindings()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.bindings, live=True, entry_id="watts-fixture", notify=lambda: None,
            clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self):
        self.hass.release.set()
        self.hass.release_switch_on.set()
        await self.guard.async_close()
        await asyncio.sleep(.01)
        for patcher in self.patchers:
            patcher.stop()

    async def settle(self):
        await asyncio.sleep(.09)

    def fresh_sources(self, *, include_watts=True):
        for entity, state in list(self.hass.data.items()):
            if include_watts or entity != WATTS:
                self.hass.set(entity, state["state"], state["attributes"])

    async def battery_trip(self, *, ignore_native=False):
        self.hass.ignore_climate = ignore_native
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.battery, 40)
        self.guard._observe()
        await self.settle()

    async def test_watt_diagnostics_identify_configured_meter_without_raw_history(self):
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["heater_power_configured"])
        self.assertFalse(self.guard.diagnostics["heater_current_configured"])

    async def test_stale_startup_watts_inhibits_without_manufactured_trip(self):
        self.hass.data[WATTS] = report(self.clock, 0, age=181)
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])
        self.hass.set(WATTS, 5.5)
        self.guard._observe()
        self.assertFalse(self.guard.blocked)

    async def test_stale_meter_during_heat_requests_native_off(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.data[WATTS] = report(self.clock, 0, age=181)
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertEqual([call[:2] for call in self.hass.calls], [("climate", "set_hvac_mode")])

    async def test_fresh_low_meter_before_stop_is_not_post_command_proof(self):
        await self.battery_trip()
        self.assertTrue(self.guard.shutdown_confirmed)  # Fresh native Off: state-only.
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.hass.set(WATTS, 5.5)
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_thermostat_idle_alone_is_not_native_off(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "idle"})
        self.hass.set(WATTS, 5.5)
        self.assertFalse(self.guard.shutdown_confirmed)
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_post_command_high_watts_contradict_off_and_cut_dedicated_outlet(self):
        await self.battery_trip()
        self.hass.set(WATTS, 1300)
        self.guard._observe()
        await self.settle()
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.hass.calls])
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")

    async def test_stale_high_watts_are_unknown_not_a_post_command_contradiction(self):
        await self.battery_trip()
        self.hass.data[WATTS] = report(self.clock, 1300, age=181)
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertNotIn(("switch", "turn_off"), [call[:2] for call in self.hass.calls])
        self.assertTrue(self.guard.blocked)

    async def test_restored_and_assumed_low_reports_cannot_confirm_stop(self):
        await self.battery_trip()
        for flag in ("restored", "assumed_state"):
            with self.subTest(flag=flag):
                self.hass.set(WATTS, 5.5, {"unit_of_measurement": "W", flag: True})
                self.guard._observe()
                self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.hass.set(WATTS, 5.5, {"unit_of_measurement": "W"})
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])

    async def prepare_dedicated_restore(self):
        await self.battery_trip(ignore_native=True)
        self.assertTrue(self.guard.switch_cut_owned)
        self.hass.set(B.battery, 43)
        self.hass.set(WATTS, 5.5)
        self.fresh_sources()
        self.guard._observe()
        self.clock.advance(400)
        self.fresh_sources()
        self.hass.ignore_climate = False

    async def test_stale_relay_off_meter_cannot_authorize_automatic_restore(self):
        await self.prepare_dedicated_restore()
        self.hass.data[WATTS] = report(self.clock, 0, age=181)
        self.guard._observe()
        await self.settle()
        self.assertNotIn(("switch", "turn_on"), [call[:2] for call in self.hass.calls])
        self.assertTrue(self.guard.blocked)

    async def test_restore_requires_new_low_watts_after_native_off_request(self):
        await self.prepare_dedicated_restore()
        self.guard._observe()
        await self.settle()
        self.assertIn(("switch", "turn_on"), [call[:2] for call in self.hass.calls])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.blocked)

    async def test_post_restore_fresh_low_watts_and_off_can_clear_ordinary_latch(self):
        await self.prepare_dedicated_restore()
        self.hass.post_off_watts = 5.5
        self.guard._observe()
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.switch_cut_owned)

    async def test_new_stop_requires_post_command_watts_before_safe_interval_accrues(self):
        await self.battery_trip()
        self.hass.set(B.battery, 43)
        self.clock.advance(400)  # Original trip cooldown is already complete.
        self.fresh_sources()
        # The independent low measurement is fresh, but a newer explicit
        # native stop supersedes its command boundary.
        self.assertTrue(await self.guard.async_request_heater_off())
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.guard._observe()
        self.clock.advance(61)
        self.fresh_sources(include_watts=False)
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["latched"])

        self.hass.set(WATTS, 5.5)
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertTrue(self.guard.diagnostics["latched"])
        self.clock.advance(59)
        self.fresh_sources()
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["latched"])
        self.clock.advance(2)
        self.fresh_sources()
        self.guard._observe()
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertTrue(all(call[0] == "climate" for call in self.hass.calls))

    async def test_shadow_watt_protection_never_sends_controls(self):
        self.guard.live = False
        await self.battery_trip()
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")

    async def test_unbound_watts_do_not_invalidate_switch_only_operation(self):
        bindings = replace(B, heater_current=None)
        guard = power.HeaterPowerGuard(
            self.hass, bindings, live=True, entry_id="switch-only", notify=lambda: None,
            clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        try:
            self.hass.set(WATTS, "unavailable")
            guard._observe()
            self.assertFalse(guard.blocked)
            self.assertFalse(guard.diagnostics["electrical_stop_confirmed"])
            self.assertEqual(self.hass.calls, [])
        finally:
            await guard.async_close()

    async def test_direct_watt_path_still_needs_explicit_shared_fallback(self):
        for enabled in (False, True):
            with self.subTest(fallback_enabled=enabled):
                entity = "switch.fixture_shared_ac"
                bindings = watt_bindings(heater_switch=None, emergency_ac_switch=entity,
                                         emergency_ac_fallback_enabled=enabled)
                self.hass.set(entity, "on")
                guard = power.HeaterPowerGuard(
                    self.hass, bindings, live=True, entry_id="direct-watts", notify=lambda: None,
                    clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
                try:
                    guard._observe()
                    self.assertEqual(guard.blocked, not enabled)
                    self.assertEqual(self.hass.calls, [])
                finally:
                    await guard.async_close()

    def pending_manual_recovery(self):
        self.guard._manual_recovery_required = self.guard._identity_mismatch = True
        self.guard._manual_recovery_wall = self.clock.wall
        self.guard._latched = True
        self.guard._tripped_at = self.clock.now - 400
        self.guard._trip_wall = self.clock.wall - 400

    async def test_manual_acknowledgement_requires_low_watts_after_recovery(self):
        self.pending_manual_recovery()
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        # The native report is new, but the power report still predates the
        # recovery boundary. A low cached value must not release wiring recovery.
        with self.assertRaises(RuntimeError):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.calls, [])

    async def test_verified_manual_acknowledgement_then_safe_interval_clears_without_commands(self):
        self.pending_manual_recovery()
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        self.hass.set(WATTS, 5.5)
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        await asyncio.sleep(.01)
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertTrue(self.guard.diagnostics["latched"])
        self.clock.advance(59)
        self.fresh_sources()
        self.guard._observe()
        self.assertTrue(self.guard.diagnostics["latched"])
        self.clock.advance(2)
        self.fresh_sources()
        self.guard._observe()
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])

    async def test_thermostat_idle_cannot_acknowledge_manual_recovery(self):
        self.pending_manual_recovery()
        self.hass.set(B.heater, "heat", {"hvac_action": "idle"})
        self.hass.set(WATTS, 5.5)
        with self.assertRaises(RuntimeError):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.calls, [])


if __name__ == "__main__":
    unittest.main()
