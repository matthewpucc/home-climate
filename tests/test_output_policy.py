"""Circuit/reserve exception with fresh evidence at each control boundary."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import test_power_guard as fixtures
from tests import test_heater_power_guard as watts
from tests import test_power_guard_commissioning as commissioning

power, B = fixtures.power, fixtures.B


class OutputPolicyTests(unittest.TestCase):
    setUp = fixtures.PowerPolicyTests.setUp
    reading = fixtures.PowerPolicyTests.reading

    def test_positive_circuit_and_reserve_waive_both_output_limits(self):
        for output in (1599, 1600, 1999, 2000, 2500, 5000):
            for battery in (40.01, 100):
                with self.subTest(output=output, battery=battery):
                    self.states = fixtures.measurements(self.clock)
                    self.states[B.output_power]["state"] = str(output)
                    self.states[B.battery]["state"] = str(battery)
                    self.states[B.input_power]["state"] = "0"  # Charging is not required.
                    self.states[B.circuit_power] = watts.report(self.clock, .001, "kW")
                    result = self.reading()
                    self.assertTrue(result.output_limits_bypassed)
                    self.assertIsNone(result.issue)
                    self.assertTrue(result.safe_to_rearm)

    def test_invalid_or_zero_circuit_reinstates_limits_without_inventing_outage(self):
        for case in ("zero", "stale", "future", "restored", "assumed_state",
                     "unavailable", "unknown", "nonfinite", "wrong_unit", "missing", "negative", "infinite"):
            for output in (150, 1600, 2500):
                with self.subTest(case=case, output=output):
                    self.states = fixtures.measurements(self.clock)
                    self.states[B.output_power]["state"] = str(output)
                    circuit = fixtures.advisory_report(self.clock, case)
                    if case in ("zero", "negative", "infinite"):
                        circuit = watts.report(self.clock, {"zero": 0, "negative": -1, "infinite": "inf"}[case])
                    if circuit is None:
                        self.states.pop(B.circuit_power)
                    else:
                        self.states[B.circuit_power] = circuit
                    result = self.reading()
                    self.assertFalse(result.output_limits_bypassed)
                    self.assertEqual(result.safe_to_rearm, output < 1600)
                    self.assertEqual(result.issue is not None, output >= 2000)
                    if output >= 2000:
                        self.assertIn("output spike", result.issue)
                    if case not in ("zero",):
                        self.assertIsNone(result.circuit_w)
                        self.assertIsNotNone(result.circuit_issue)

    def test_battery_must_be_fresh_trusted_and_strictly_above_reserve(self):
        cases = [watts.report(self.clock, value, "%") for value in
                 (0, 39.99, 40, 101, "NaN", "inf", "unknown", "unavailable")]
        cases += [watts.report(self.clock, 100, "%", age=901),
                  watts.report(self.clock, 100, "%", restored=True),
                  watts.report(self.clock, 100, "%", assumed_state=True)]
        for battery in cases:
            with self.subTest(battery=battery):
                self.states = fixtures.measurements(self.clock)
                self.states[B.battery] = battery
                self.states[B.output_power]["state"] = "2500"
                result = self.reading()
                self.assertFalse(result.output_limits_bypassed)
                self.assertIsNotNone(result.issue)
                self.assertFalse(result.safe_to_rearm)
                # The battery alone still blocks when output is low.
                self.states[B.output_power]["state"] = "150"
                self.assertIsNotNone(self.reading().issue)
                self.assertFalse(self.reading().safe_to_rearm)

    def test_latest_reports_qualify_and_time_alone_revokes_exception(self):
        self.states[B.output_power]["state"] = "2500"
        # Unchanged values may have old last_updated but new last_reported.
        for entity in (B.circuit_power, B.battery):
            self.states[entity]["last_updated"] = datetime.fromtimestamp(
                self.clock.wall - 3600, timezone.utc).isoformat()
        self.assertTrue(self.reading().output_limits_bypassed)
        circuit = deepcopy(self.states[B.circuit_power])
        self.clock.advance(180)
        self.states = fixtures.measurements(self.clock, **{B.output_power: (2500, "W")})
        self.states[B.circuit_power] = circuit
        self.assertTrue(self.reading().safe_to_rearm)
        self.clock.advance(.001)
        self.assertFalse(self.reading().output_limits_bypassed)
        self.assertIn("output spike", self.reading().issue)

    def test_exception_does_not_replace_required_measurements_or_meter_stop(self):
        for entity in (B.input_power, B.output_power, B.wall_phase_a_voltage,
                       B.wall_phase_b_voltage, B.heater_current):
            with self.subTest(entity=entity):
                self.states = fixtures.measurements(self.clock, **{B.output_power: (2500, "W")})
                self.states[entity]["state"] = "unavailable"
                self.assertTrue(self.reading().output_limits_bypassed)
                self.assertTrue(self.reading().telemetry_missing)
                self.assertFalse(self.reading().safe_to_rearm)
        self.states = fixtures.measurements(self.clock, **{B.output_power: (2500, "W"), B.heater_current: (1, "A")})
        self.assertFalse(self.reading().safe_to_rearm)
        self.states[B.heater_current]["state"] = "0"
        self.states[B.input_power]["state"] = "1476"
        self.assertTrue(self.reading().wall_load_only)
        self.assertFalse(self.reading().safe_to_rearm)


class OutputRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PowerGuardAsyncTests.asyncSetUp
    asyncTearDown = fixtures.PowerGuardAsyncTests.asyncTearDown
    settle = fixtures.PowerGuardAsyncTests.settle
    trip = fixtures.PowerGuardAsyncTests.trip
    healthy = fixtures.PowerGuardAsyncTests.healthy
    refresh_sensor_reports = fixtures.PowerGuardAsyncTests.refresh_sensor_reports
    observe_for = fixtures.PowerGuardAsyncTests.observe_for
    prepare_restore = fixtures.PowerGuardAsyncTests.prepare_restore

    async def test_running_high_output_preserves_wall_qualification(self):
        self.hass.set(B.output_power, 2500)
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.calls, [])
        self.hass.set(B.input_power, 1500)
        for elapsed in (0, 15):
            self.clock.advance(elapsed)
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
        self.assertIn("Sustained input", self.guard._reading.issue)
        self.assertTrue(self.guard.shutdown_confirmed)

    async def test_manual_acknowledgement_allows_high_output(self):
        self.guard._manual_recovery_required = self.guard._identity_mismatch = True
        self.guard._latched = True
        self.guard._tripped_at = self.clock.now - 400
        self.guard._manual_recovery_wall = self.clock.wall
        self.hass.set(B.output_power, 2500)
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        self.hass.set(B.heater_current, .05)
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.hass.calls, [])

    async def test_manual_acknowledgement_rechecks_exception_after_save(self):
        self.guard._manual_recovery_required = self.guard._identity_mismatch = True
        self.guard._latched = True
        self.guard._tripped_at = self.clock.now - 400
        self.guard._manual_recovery_wall = self.clock.wall
        self.hass.set(B.output_power, 1800)
        self.hass.set(B.heater, "off", {"hvac_action": "off"})
        self.hass.set(B.heater_current, .05)
        async def save(data):
            self.hass.set(B.circuit_power, "unavailable")
        self.guard._store = Mock(async_save=AsyncMock(side_effect=save))
        with self.assertRaisesRegex(RuntimeError, "safe electrical"):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertTrue(self.guard._store.async_save.call_args.args[0]["manual_recovery_required"])
        self.assertEqual(self.hass.calls, [])

    async def test_owned_relay_can_restore_at_high_output_with_fresh_off(self):
        await self.prepare_restore()
        self.hass.set(B.output_power, 2500)
        self.guard._observe()
        await self.settle()
        self.assertEqual(sum(call[1] == "turn_on" for call in self.hass.calls), 1)
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertTrue(self.guard.diagnostics["output_limits_bypassed"])

    async def test_circuit_loss_during_restore_save_prevents_on(self):
        await self.prepare_restore()
        self.hass.set(B.output_power, 1800)

        async def save(data):
            if data.get("restoring"):
                self.hass.set(B.circuit_power, 0)
        self.guard._store = Mock(async_save=AsyncMock(side_effect=save))
        self.guard._observe()
        await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertTrue(self.guard.blocked)

    async def test_time_only_expiry_while_relay_on_is_queued_prevents_dispatch(self):
        await self.prepare_restore()
        self.hass.set(B.output_power, 1800)
        original = self.hass.async_create_task

        def create(coroutine, *, eager_start=False):
            async def queued():
                if coroutine.__qualname__.endswith("_service.<locals>.dispatch"):
                    circuit = deepcopy(self.hass.data[B.circuit_power])
                    self.clock.advance(181)
                    self.hass.refresh()
                    self.hass.data[B.circuit_power] = circuit
                return await coroutine
            return original(queued(), eager_start=eager_start)

        with patch.object(self.hass, "async_create_task", side_effect=create):
            self.guard._observe()
            await self.settle()
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")

    async def test_circuit_loss_after_on_recuts_before_gate_opens(self):
        await self.prepare_restore()
        self.hass.set(B.output_power, 1800)
        original = self.hass.services.async_call

        async def call(domain, service, body, blocking):
            await original(domain, service, body, blocking)
            if service == "turn_on":
                self.hass.set(B.circuit_power, 0)
        with patch.object(self.hass.services, "async_call", side_effect=call):
            self.guard._observe()
            await self.settle()
        self.assertIn("turn_on", [c[1] for c in self.hass.calls])
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")


class OutputCommissioningTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = commissioning.RelayCommissioningTests.asyncSetUp
    asyncTearDown = commissioning.RelayCommissioningTests.asyncTearDown
    settle = commissioning.RelayCommissioningTests.settle

    async def test_high_output_allows_commissioning_prerequisites(self):
        self.hass.set(B.output_power, 2500)
        self.assertTrue(await self.guard.async_commission_relay(confirm=True))
        self.assertTrue(self.guard.commissioning_active)
        self.assertEqual(self.hass.calls[0][:2], ("switch", "turn_off"))
