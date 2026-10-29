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


def advisory_report(clock, case):
    """Circuit observations cover ordinary disagreement and unusable data."""
    if case == "missing":
        return None
    value, unit = {"high": (5000, "W"), "unavailable": ("unavailable", "W"),
                   "unknown": ("unknown", "W"), "nonfinite": ("NaN", "W"),
                   "wrong_unit": (6, "A")}.get(case, (140, "W"))
    stamp = clock.wall + 6 if case == "future" else clock.wall - (181 if case == "stale" else 0)
    attrs = {"unit_of_measurement": unit}
    if case in ("restored", "assumed_state"):
        attrs[case] = True
    return {"state": str(value), "attributes": attrs,
            "last_reported": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.wall = 1800000000.0

    def advance(self, seconds):
        self.now += seconds
        # HA timestamps have microsecond precision. Normalize the synthetic
        # wall clock to the same representation instead of creating a report
        # a fraction of a microsecond later than its observation clock.
        self.wall = datetime.fromtimestamp(self.wall + seconds, timezone.utc).timestamp()


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

    def test_circuit_reading_is_advisory_for_trip_and_rearm(self):
        for case in ("high", "stale", "future", "restored", "assumed_state",
                     "unavailable", "unknown", "nonfinite", "wrong_unit", "missing"):
            with self.subTest(case=case):
                self.states = measurements(self.clock)
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.states.pop(B.circuit_power)
                else:
                    self.states[B.circuit_power] = circuit
                result = self.reading()
                self.assertIsNone(result.issue)
                self.assertFalse(result.telemetry_missing)
                self.assertFalse(result.wall_load_only)
                self.assertTrue(result.safe_to_rearm)
                if case == "high":
                    self.assertEqual(result.circuit_w, 5000)
                    self.assertIsNone(result.circuit_issue)
                else:
                    self.assertIsNone(result.circuit_w)
                    self.assertIsNotNone(result.circuit_issue)

    def test_advisory_circuit_never_substitutes_for_missing_input(self):
        for case in ("high", "stale", "missing"):
            with self.subTest(case=case):
                self.states = measurements(self.clock)
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.states.pop(B.circuit_power)
                else:
                    self.states[B.circuit_power] = circuit
                self.states[B.input_power]["state"] = "unavailable"
                result = self.reading()
                self.assertTrue(result.telemetry_missing)
                self.assertIn(B.input_power, result.issue)
                self.assertFalse(result.safe_to_rearm)

    def test_guard_has_no_implicit_device_wiring(self):
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), live=True, entry_id="test", notify=lambda: None)
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), None, live=True, entry_id="test", notify=lambda: None)

    def test_circuit_is_not_added_to_input_and_output_is_separate(self):
        for entity in (B.circuit_power, B.input_power):
            self.states[entity]["state"] = "1000"
        self.states[B.output_power]["state"] = "1891"
        result = self.reading()
        self.assertIsNone(result.issue)
        self.assertFalse(result.wall_load_only)
        self.assertFalse(result.safe_to_rearm)  # Output must settle before recovery.
        self.states[B.input_power]["state"] = "1476"
        result = self.reading()
        self.assertIn("input", result.issue)
        self.assertTrue(result.wall_load_only)

    def test_each_budget_is_reported_at_boundary(self):
        for entity, value, text in (
            (B.input_power, 1476, "budget"),
            (B.output_power, 2000, "output spike"),
            (B.battery, 40, "reserve"),
        ):
            with self.subTest(entity=entity):
                self.states = measurements(self.clock)
                self.states[entity]["state"] = str(value)
                result = self.reading()
                self.assertIn(text, result.issue)
                self.assertEqual(result.wall_load_only, entity == B.input_power)
                self.assertFalse(result.safe_to_rearm)

    def test_fresh_lower_phase_sets_budget_without_nominal_watt_cap(self):
        self.assertEqual(self.reading().input_budget_w, 1476)
        for entity in (B.circuit_power, B.input_power):
            self.states[entity]["state"] = "1434"
        self.assertIsNone(self.reading().issue)
        self.states[B.wall_phase_a_voltage]["state"] = "110"
        self.assertAlmostEqual(self.reading().input_budget_w, 1320)
        self.assertIsNotNone(self.reading().issue)
        self.states[B.wall_phase_a_voltage]["state"] = "130"
        self.assertEqual(self.reading().input_budget_w, 1476)
        self.states[B.wall_phase_b_voltage]["state"] = "128"
        self.assertEqual(self.reading().input_budget_w, 1536)
        self.assertIsNone(self.reading().issue)

    def test_rearm_uses_85_percent_of_the_effective_budget(self):
        boundary = 1476 * .85
        self.states[B.input_power]["state"] = str(boundary)
        self.assertIsNone(self.reading().issue)
        self.assertFalse(self.reading().safe_to_rearm)
        self.states[B.input_power]["state"] = str(boundary - .001)
        self.assertTrue(self.reading().safe_to_rearm)

    def test_units_and_nonfinite_or_stale_telemetry_fail_closed(self):
        self.states[B.circuit_power]["state"] = "1.4"
        self.states[B.circuit_power]["attributes"]["unit_of_measurement"] = "kW"
        self.assertEqual(self.reading().circuit_w, 1400)
        self.assertIsNone(self.reading().issue)
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
                                      (B.input_power, "1476", "budget"),
                                      (B.battery, "40", "reserve")):
            previous = self.states[entity]["state"]
            self.states[entity]["state"] = value
            self.assertIn(reason, self.reading().issue)
            self.states[entity]["state"] = previous

    def test_missing_interlock_telemetry_is_not_a_wall_only_reading(self):
        for missing in (B.battery, B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            with self.subTest(missing=missing):
                self.states = measurements(self.clock)
                self.states[B.input_power]["state"] = "1600"
                self.states[missing]["state"] = "unavailable"
                result = self.reading()
                self.assertTrue(result.telemetry_missing)
                self.assertFalse(result.wall_load_only)
                self.assertFalse(result.safe_to_rearm)

    def test_stale_or_invalid_voltage_cannot_supply_a_nominal_budget(self):
        self.states[B.input_power]["state"] = "1600"
        self.states[B.wall_phase_a_voltage]["last_reported"] = datetime.fromtimestamp(
            self.clock.wall - power.TELEMETRY_MAX_AGE_SECONDS - 1,
            timezone.utc).isoformat()
        result = self.reading()
        self.assertTrue(result.telemetry_missing)
        self.assertFalse(result.wall_load_only)
        self.assertIsNone(result.input_budget_w)
        self.assertFalse(result.safe_to_rearm)
        for value in ("NaN", "141", "89", "-1"):
            with self.subTest(value=value):
                self.states = measurements(self.clock)
                self.states[B.input_power]["state"] = "1600"
                self.states[B.wall_phase_a_voltage]["state"] = value
                result = self.reading()
                self.assertTrue(result.telemetry_missing)
                self.assertFalse(result.wall_load_only)
                self.assertIsNone(result.input_budget_w)
                self.assertFalse(result.safe_to_rearm)

    def test_independent_output_or_battery_trip_wins_over_wall_qualification(self):
        for entity, value, reason in ((B.output_power, "2000", "output spike"),
                                      (B.battery, "40", "reserve")):
            with self.subTest(entity=entity):
                self.states = measurements(self.clock)
                self.states[B.input_power]["state"] = "1600"
                self.states[B.circuit_power]["state"] = "1600"
                self.states[entity]["state"] = value
                result = self.reading()
                self.assertIn(reason, result.issue)
                self.assertFalse(result.wall_load_only)
                self.assertFalse(result.safe_to_rearm)

    def test_restored_or_assumed_voltage_cannot_supply_a_budget(self):
        for entity in (B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            for flag in ("restored", "assumed_state"):
                with self.subTest(entity=entity, flag=flag):
                    self.states = measurements(self.clock)
                    self.states[B.input_power]["state"] = "1600"
                    self.states[entity]["attributes"][flag] = True
                    result = self.reading()
                    self.assertTrue(result.telemetry_missing)
                    self.assertIn("restored or assumed", result.issue)
                    self.assertIsNone(result.input_budget_w)
                    self.assertFalse(result.wall_load_only)
                    self.assertFalse(result.safe_to_rearm)

    def test_missing_battery_does_not_hide_definite_output_trip(self):
        self.states[B.battery]["state"] = "unavailable"
        self.states[B.input_power]["state"] = "1600"
        self.states[B.output_power]["state"] = "2000"
        result = self.reading()
        self.assertIn("output spike", result.issue)
        self.assertFalse(result.telemetry_missing)
        self.assertFalse(result.wall_load_only)


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

    def async_create_task(self, coroutine, *, eager_start=False):
        # Python 3.12 already schedules asyncio tasks non-eagerly. Accept HA's
        # keyword without passing an unsupported option to asyncio.create_task.
        return asyncio.create_task(coroutine)

    def set(self, entity, state, attrs=None):
        # Each synthetic device report is a distinct arrival, even when test
        # code performs several writes without yielding the event loop. Keep
        # ordering realistic so a changed value never reuses an old timestamp.
        self.clock.advance(.001)
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
        self.hass.set(B.input_power, "1500")
        for elapsed in (0, 15, 15):
            self.clock.advance(elapsed)
            self.hass.refresh()
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

    async def test_input_load_waits_for_qualification_before_stopping_running_heat(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.input_power, "1500")
        self.guard._observe()
        await self.settle()
        # Public blocked feeds the worker's heater-Off overlay. Positive
        # dispatch is deferred separately while existing heat keeps its window.
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.hass.data[B.heater]["state"], "heat")
        self.clock.advance(15)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])
        self.clock.advance(15)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual([call[0] for call in self.hass.calls], ["climate"])

    async def test_advisory_circuit_cannot_stop_heat_or_inhibit_activation(self):
        for case in ("high", "stale", "future", "restored", "assumed_state",
                     "unavailable", "unknown", "nonfinite", "wrong_unit", "missing"):
            with self.subTest(case=case):
                self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                for elapsed in (0, 15, 15):
                    self.clock.advance(elapsed)
                    for entity, state in list(self.hass.data.items()):
                        if entity != B.circuit_power:
                            self.hass.set(entity, state["state"], state["attributes"])
                    self.guard._observe()
                await self.settle()
                self.assertFalse(self.guard.blocked)
                self.assertFalse(self.guard.diagnostics["latched"])
                self.guard.check_activation(B.heater)
                self.assertEqual(self.hass.data[B.heater]["state"], "heat")
                self.assertEqual(self.hass.calls, [])

    async def test_advisory_circuit_remains_visible_without_enforcement(self):
        for case, status in (("high", "available"), ("stale", "unavailable"),
                             ("unavailable", "unavailable"), ("missing", "unavailable")):
            with self.subTest(case=case):
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                self.guard._observe()
                diagnostics = self.guard.diagnostics
                self.assertFalse(diagnostics["circuit_enforced"])
                wall = diagnostics["wall_power_budget"]
                self.assertEqual(wall["enforced_sources"], ["input"])
                self.assertTrue(wall["sources"]["input"]["enforced"])
                advisory = wall["sources"]["circuit"]
                self.assertEqual(advisory["status"], "advisory")
                self.assertFalse(advisory["enforced"])
                self.assertEqual(advisory["reading_status"], status)
                self.assertEqual(advisory["source"], B.circuit_power)
                self.assertEqual(advisory["report_count"], 0)
                self.assertIsNone(advisory["first_reported_at"])
                self.assertEqual(wall["qualified_sources"], [])
                self.assertEqual(wall["unavailable_sources"], [])
                self.assertTrue(advisory["reason"].startswith("Advisory"))

    async def test_missing_input_is_not_replaced_by_advisory_circuit(self):
        self.hass.set(B.circuit_power, "100")
        self.hass.set(B.input_power, "unavailable")
        self.guard._observe()
        self.assertTrue(self.guard.blocked)
        self.assertIn(B.input_power, self.guard.diagnostics["reason"])
        self.assertEqual(self.guard.hass.calls, [])

    async def test_advisory_circuit_cannot_block_manual_recovery_acknowledgement(self):
        for case in ("high", "unavailable", "missing"):
            with self.subTest(case=case):
                self.guard._manual_recovery_required = self.guard._identity_mismatch = True
                self.guard._latched = True
                self.guard._tripped_at = self.clock.now - 400
                self.guard._manual_recovery_wall = self.clock.wall
                self.hass.set(B.heater, "off", {"hvac_action": "off"})
                self.hass.set(B.heater_current, ".05")
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                self.assertTrue(await self.guard.async_acknowledge_emergency())
                self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
                self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
                self.assertEqual(self.hass.calls, [])

    async def test_advisory_circuit_cannot_block_owned_outlet_restoration(self):
        for case in ("high", "unavailable", "missing"):
            with self.subTest(case=case):
                await self.prepare_restore()
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                before = sum(call[1] == "turn_on" for call in self.hass.calls)
                self.guard._observe()
                await self.settle()
                self.assertEqual(sum(call[1] == "turn_on" for call in self.hass.calls), before + 1)
                self.assertEqual(self.hass.data[B.heater]["state"], "off")
                self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
                self.assertFalse(self.guard.blocked)

    async def test_confirmed_off_and_persistent_wall_load_do_not_repeat_shutdown(self):
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.shutdown_confirmed)
        initial_calls = list(self.hass.calls)
        for _ in range(3):
            self.clock.advance(45)
            self.hass.refresh()
            self.guard._observe()
            await self.settle()
            self.assertTrue(self.guard.shutdown_confirmed)
            self.assertTrue(self.guard.blocked)
            self.assertFalse(self.guard.switch_cut_owned)
            self.assertEqual(self.hass.calls, initial_calls)

    async def test_output_spike_does_not_wait_for_wall_qualification(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.input_power, "1600")
        self.hass.set(B.output_power, "2000")
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual([call[0] for call in self.hass.calls], ["climate"])
        self.assertFalse(self.guard.switch_cut_owned)

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
