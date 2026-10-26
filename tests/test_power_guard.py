"""Exercise electrical policy and async failover without importing HA itself."""
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from house_climate.bindings import example_bindings


# The integration package imports HA during __init__; load this deliberately
# standard-library-only module directly for the core Python 3.11/3.12 suite.
_path = Path(__file__).resolve().parents[1] / "custom_components/house_climate/power_guard.py"
_spec = importlib.util.spec_from_file_location("_tested_house_power_guard", _path)
power = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = power
_spec.loader.exec_module(power)

B = example_bindings().power


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.wall = 1800000000.0

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


def measurements(clock, **changes):
    defaults = {
        B.circuit_power: (140, "W"), B.input_power: (130, "W"),
        B.output_power: (150, "W"), B.battery: (43, "%"),
        B.heater_current: (.05, "A"),
        B.wall_phase_a_voltage: (124, "V"), B.wall_phase_b_voltage: (123, "V"),
    }
    defaults.update(changes)
    result = {entity: {"state": str(value), "attributes": {"unit_of_measurement": unit},
                       "last_changed": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
                       "last_reported": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}
              for entity, (value, unit) in defaults.items()}
    result[B.heater] = {"state": "off", "attributes": {"hvac_action": "off"},
                            "last_reported": datetime.fromtimestamp(clock.wall - 1, timezone.utc).isoformat()}
    result[B.heater_switch] = {"state": "on", "attributes": {},
                                 "last_changed": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
                                 "last_reported": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}
    return result


class PowerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.states = measurements(self.clock)

    def reading(self):
        return power.evaluate_power(self.states, B, self.clock.wall)

    def test_guard_has_no_implicit_device_wiring(self):
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), live=True, entry_id="test", notify=lambda: None)
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), None, live=True, entry_id="test", notify=lambda: None)

    def test_input_and_circuit_overlap_not_added_and_output_is_separate(self):
        for entity in (B.circuit_power, B.input_power):
            self.states[entity]["state"] = "1000"
        self.states[B.output_power]["state"] = "1891"
        result = self.reading()
        self.assertIsNone(result.issue)
        self.assertFalse(result.safe_to_rearm)  # Output must settle before recovery.
        self.states[B.input_power]["state"] = "1350"
        self.assertIn("input", self.reading().issue)

    def test_each_budget_sheds_at_boundary(self):
        for entity, value, text in (
            (B.circuit_power, 1350, "budget"),
            (B.input_power, 1350, "budget"),
            (B.output_power, 2000, "output spike"),
            (B.battery, 40, "reserve"),
        ):
            with self.subTest(entity=entity):
                self.states = measurements(self.clock)
                self.states[entity]["state"] = str(value)
                self.assertIn(text, self.reading().issue)
                self.assertFalse(self.reading().safe_to_rearm)

    def test_low_voltage_reduces_budget_and_high_voltage_does_not_raise_it(self):
        self.states[B.wall_phase_a_voltage]["state"] = "110"
        self.states[B.circuit_power]["state"] = "1260"
        self.assertAlmostEqual(self.reading().input_budget_w, 1254)
        self.assertIsNotNone(self.reading().issue)
        self.states[B.wall_phase_a_voltage]["state"] = "130"
        self.assertEqual(self.reading().input_budget_w, 1350)

    def test_units_and_nonfinite_or_stale_telemetry_fail_closed(self):
        self.states[B.circuit_power]["state"] = "1.4"
        self.states[B.circuit_power]["attributes"]["unit_of_measurement"] = "kW"
        self.assertEqual(self.reading().circuit_w, 1400)
        self.assertIsNotNone(self.reading().issue)
        for bad in ("NaN", "inf", "-5", "unavailable"):
            self.states = measurements(self.clock)
            self.states[B.input_power]["state"] = bad
            self.assertTrue(self.reading().telemetry_missing)
        self.states = measurements(self.clock)
        self.clock.advance(power.TELEMETRY_MAX_AGE_SECONDS + 1)
        self.assertTrue(self.reading().telemetry_missing)

    def test_downstream_heater_current_does_not_apply_wall_input_ceiling(self):
        self.states[B.input_power]["state"] = "1200"
        self.states[B.circuit_power]["state"] = "1250"
        self.states[B.output_power]["state"] = "1800"
        self.states[B.heater_current]["state"] = "13"
        self.assertIsNone(self.reading().issue)
        self.assertFalse(self.reading().safe_to_rearm)  # Heat must stop before a tripped guard rearms.
        for entity, value, reason in ((B.output_power, "2000", "output spike"),
                                      (B.input_power, "1350", "budget"),
                                      (B.battery, "40", "reserve")):
            previous = self.states[entity]["state"]
            self.states[entity]["state"] = value
            self.assertIn(reason, self.reading().issue)
            self.states[entity]["state"] = previous

    def test_missing_battery_does_not_hide_definite_input_or_output_trip(self):
        for entity, value, reason in ((B.input_power, "1350", "budget"),
                                      (B.output_power, "2000", "output spike")):
            with self.subTest(entity=entity):
                self.states = measurements(self.clock)
                self.states[B.battery]["state"] = "unavailable"
                self.states[entity]["state"] = value
                self.assertIn(reason, self.reading().issue)
                self.assertFalse(self.reading().telemetry_missing)


class FakeHass:
    def __init__(self, clock):
        self.clock = clock
        self.data = measurements(clock)
        self.states = SimpleNamespace(get=self.data.get)
        self.calls = []
        self.ignore_climate = False
        self.hang_climate = False
        self.ignore_cancellation = False
        self.release = asyncio.Event()
        self.restore_to_heat = False
        self.restore_overload = False
        self.ignore_switch_on_feedback = False
        self.hang_switch_on = False
        self.release_switch_on = asyncio.Event()
        self.services = SimpleNamespace(async_call=self.service)

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def set(self, entity, state, attrs=None):
        old = self.data.get(entity, {})
        self.data[entity] = {"state": str(state),
                            "attributes": dict(old.get("attributes", {}) if attrs is None else attrs),
                            "last_changed": old.get("last_changed") if old.get("state") == str(state)
                                else datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat(),
                            "last_reported": datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()}

    def refresh(self):
        for entity, old in list(self.data.items()):
            self.set(entity, old["state"], old["attributes"])

    async def service(self, domain, service, body, blocking):
        self.calls.append((domain, service, dict(body)))
        self.clock.advance(.001)
        entity = body["entity_id"]
        if domain == "climate":
            if self.hang_climate:
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        raise
                    await self.release.wait()
            if self.ignore_climate:
                return
            self.set(entity, "off", {"hvac_action": "off"})
        elif service == "turn_off":
            self.set(entity, "off")
            self.set(B.heater, "unavailable", {})
        elif service == "turn_on":
            if self.hang_switch_on:
                await self.release_switch_on.wait()
            if self.ignore_switch_on_feedback:
                return
            self.set(entity, "on")
            self.set(B.heater, "heat" if self.restore_to_heat else "off",
                     {"hvac_action": "heating" if self.restore_to_heat else "off"})
            if self.restore_overload:
                self.set(B.input_power, "1600")


class PowerGuardAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.hass = FakeHass(self.clock)
        self.guard = power.HeaterPowerGuard(self.hass, B, live=True, entry_id="test", notify=lambda: None,
                                           clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        self.hass.release.set()
        self.hass.release_switch_on.set()
        await self.guard.async_close()
        await asyncio.sleep(.01)
        for p in self.patchers:
            p.stop()

    async def settle(self):
        await asyncio.sleep(.09)

    def trip(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.circuit_power, "1500")
        self.guard._observe()

    def healthy(self):
        self.hass.set(B.circuit_power, "140")
        self.hass.set(B.input_power, "130")
        self.hass.set(B.output_power, "150")
        self.hass.set(B.heater_current, ".05")
        self.hass.set(B.battery, "43")
        self.hass.refresh()

    async def test_off_ack_avoids_cutting_plug_and_never_touches_network(self):
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertEqual([c[0] for c in self.hass.calls], ["climate"])

    async def test_buffered_normal_heating_above_twelve_output_amps_is_not_shed(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.input_power, "1200")
        self.hass.set(B.circuit_power, "1250")
        self.hass.set(B.output_power, "1800")
        self.hass.set(B.heater_current, "13")
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])

    async def test_hung_uncooperative_dyson_cannot_delay_dedicated_cutoff(self):
        self.hass.hang_climate = self.hass.ignore_cancellation = True
        self.trip()
        await self.settle()
        self.assertFalse(self.hass.release.is_set())
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertEqual({c[2]["entity_id"] for c in self.hass.calls},
                         {B.heater, B.heater_switch})

    async def test_service_return_without_observed_off_still_cuts_plug(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)

    async def test_recovery_needs_strict_battery_safe_stability_and_cooldown(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.healthy()
        self.hass.set(B.battery, "40")
        self.clock.advance(360)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.hass.set(B.battery, "40.01")
        self.guard._observe()
        self.clock.advance(59)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.clock.advance(2)
        self.hass.refresh()
        self.hass.ignore_climate = False
        self.hass.restore_to_heat = True
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        on_index = next(i for i,c in enumerate(self.hass.calls) if c[1] == "turn_on")
        self.assertEqual(self.hass.calls[on_index + 1][:2], ("climate", "set_hvac_mode"))
        self.assertTrue(all(c[2].get("hvac_mode", "off") == "off" for c in self.hass.calls))

    async def test_recovery_never_turns_on_a_manually_off_plug(self):
        self.hass.set(B.heater_switch, "off")
        self.hass.set(B.battery, "39")
        self.guard._observe()
        await self.settle()
        self.healthy()
        self.guard._observe()
        self.clock.advance(400)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_manual_off_is_physical_isolation_without_restore_ownership(self):
        self.hass.set(B.heater_switch, "off")
        self.hass.set(B.heater, "unavailable", {})
        self.guard._observe()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.blocked)
        self.assertTrue(await self.guard.async_close())
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_late_heat_after_confirmed_stop_cuts_without_second_dyson_wait(self):
        self.trip()
        await self.settle()
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.guard._observe()
        await asyncio.sleep(.01)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual(sum(c[0] == "climate" for c in self.hass.calls), 1)

    async def test_output_spike_during_restore_cuts_again_and_keeps_gate_closed(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.healthy()
        self.guard._observe()
        self.clock.advance(400)
        self.hass.refresh()
        self.hass.ignore_climate = False
        self.hass.restore_overload = True
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.guard.diagnostics["restore_attempts"], 1)

    async def test_shadow_and_close_never_restore_or_send_commands(self):
        self.guard.live = False
        self.trip()
        await self.settle()
        self.assertEqual(self.hass.calls, [])

    async def prepare_restore(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.healthy()
        self.guard._observe()
        self.clock.advance(400)
        self.hass.refresh()
        self.hass.ignore_climate = False

    async def test_service_receipt_without_fresh_plug_on_never_clears_latch(self):
        await self.prepare_restore()
        self.hass.ignore_switch_on_feedback = True
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.power_cut_confirmed)
        calls = [c[1] for c in self.hass.calls]
        self.assertEqual(calls[-2:], ["turn_on", "turn_off"])

    async def test_late_plug_on_is_tracked_then_superseded_before_close(self):
        await self.prepare_restore()
        self.hass.hang_switch_on = True
        self.guard._observe()
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertFalse(self.guard.power_cut_confirmed)  # Old On is still capable of undoing this Off.
        self.assertTrue(self.guard.diagnostics["power_restore_inflight"])
        self.assertFalse(await self.guard.async_close())
        self.assertFalse(self.guard._closed)
        self.hass.release_switch_on.set()
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertTrue(await self.guard.async_close())

    async def test_newer_manual_off_transition_loses_automatic_restore_ownership(self):
        await self.prepare_restore()
        self.clock.advance(1)
        self.hass.set(B.heater_switch, "on")
        self.guard._observe()
        self.clock.advance(1)
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_realistic_fan_only_load_does_not_block_recovery(self):
        self.hass.set(B.heater_current, ".6")
        self.guard._observe()
        self.assertTrue(self.guard._reading.safe_to_rearm)

    async def test_raw_measurements_do_not_wake_comfort_loop(self):
        self.guard.notify = Mock()
        self.guard._observe()
        self.assertEqual(self.guard.notify.call_count, 1)
        self.hass.set(B.circuit_power, "150")
        self.hass.set(B.battery, "44")
        self.guard._observe()
        self.assertEqual(self.guard.notify.call_count, 1)
        await self.guard.async_close()
        self.assertEqual(self.hass.calls, [])

    async def test_missing_startup_telemetry_inhibits_without_spurious_trip_timer(self):
        self.hass.set(B.input_power, "unavailable")
        self.guard._observe()
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.hass.set(B.input_power, "130")
        self.guard._observe()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.calls, [])


if __name__ == "__main__":
    unittest.main()
