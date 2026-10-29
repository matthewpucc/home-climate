"""Circuit telemetry is visible but never an independent heater interlock."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.sensor import HeaterPowerStatus
from tests_homeassistant import test_power_guard as fixtures


B = fixtures.B


class NativeCircuitAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real HA service/state fixture without inheriting its test cases.
    asyncSetUp = fixtures.NativePowerGuardTests.asyncSetUp
    asyncTearDown = fixtures.NativePowerGuardTests.asyncTearDown
    handle = fixtures.NativePowerGuardTests.handle
    use_source_clock = fixtures.NativePowerGuardTests.use_source_clock
    report = fixtures.NativePowerGuardTests.report
    start = fixtures.NativePowerGuardTests.start

    async def test_sustained_high_circuit_does_not_stop_or_defer_heat(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for elapsed in (1, 16, 31, 61):
            await self.report(elapsed, {B.circuit_power: (3000, {"unit_of_measurement": "W"})})
            self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
            self.assertFalse(self.guard.blocked)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get(B.heater).state, "heat")

    async def test_unavailable_and_invalid_circuit_do_not_invalidate_heat(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for elapsed, value, unit in ((1, "unavailable", "W"), (2, "unknown", "W"),
                                     (3, "NaN", "W"), (4, -1, "W"), (5, 50, "VA")):
            with self.subTest(value=value, unit=unit):
                await self.report(elapsed, {B.circuit_power: (value, {"unit_of_measurement": unit})})
                self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
                self.assertFalse(self.guard.blocked)
                self.assertFalse(self.guard.diagnostics["latched"])
                self.assertEqual(self.calls, [])

    async def test_expired_circuit_and_removed_entity_do_not_block_activation(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        # Keep enforced sources continuously available; age only the advisory
        # sensor rather than introducing an unrelated input/voltage gap.
        authoritative = tuple(entity for entity in self.guard.entity_ids if entity != B.circuit_power)
        for elapsed in (30, 60, 90, 120, 150, 180, 181):
            await self.report(elapsed, only=authoritative)
        self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.hass.states.async_remove(B.circuit_power)
        await self.hass.async_block_till_done()
        self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_missing_authoritative_input_is_not_replaced_by_circuit(self):
        await self.start()
        self.hass.states.async_set(B.circuit_power, "100", {"unit_of_measurement": "W"})
        self.hass.states.async_set(B.input_power, "unavailable")
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.assertTrue(self.guard.blocked)
        self.assertIn(B.input_power, self.guard.diagnostics["reason"])
        self.assertEqual(self.calls, [])

    async def test_missing_voltage_is_not_replaced_by_circuit_or_nominal_voltage(self):
        await self.start()
        self.hass.states.async_set(B.circuit_power, "100", {"unit_of_measurement": "W"})
        self.hass.states.async_set(B.wall_phase_a_voltage, "unavailable")
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.assertTrue(self.guard.blocked)
        self.assertIsNone(self.guard._reading.input_budget_w)
        self.assertEqual(self.calls, [])

    async def test_manual_recovery_acknowledgement_ignores_bad_circuit(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.guard._latched = self.guard._manual_recovery_required = True
        self.guard._identity_mismatch = True
        self.guard._manual_recovery_wall = self.origin
        self.guard._tripped_at = self.guard._clock() - 600
        self.guard._trip_wall = self.origin - 600
        await self.report(1, {B.circuit_power: ("unavailable", {"unit_of_measurement": "W"})})
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertEqual(self.calls, [])

    async def test_high_circuit_cannot_prevent_ordinary_latch_recovery(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        await self.report(1, {B.battery: (40, {"unit_of_measurement": "%"})})
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.states.get(B.heater).state, "off")
        for elapsed in (46, 106, 166, 226, 286, 332):
            await self.report(elapsed, {B.battery: (90, {"unit_of_measurement": "%"}),
                                        B.circuit_power: (3000, {"unit_of_measurement": "W"})})
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])

    async def test_unavailable_circuit_cannot_prevent_owned_outlet_restoration(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.ignore_climate = True
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        await self.report(1, {B.battery: (40, {"unit_of_measurement": "%"})})
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.states.get(B.heater_switch).state, "off")
        self.ignore_climate = False
        for elapsed in (46, 106, 166, 226, 286, 332):
            await self.report(elapsed, {B.battery: (90, {"unit_of_measurement": "%"}),
                                        B.circuit_power: ("unavailable", {"unit_of_measurement": "W"})})
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.states.get(B.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(B.heater).state, "off")
        self.assertEqual(sum(call[1] == "turn_on" for call in self.calls), 1)

    async def test_advisory_role_is_explicit_in_real_sensor_attributes(self):
        await self.start()
        self.hass.states.async_set(B.circuit_power, "unavailable")
        await self.hass.async_block_till_done()
        sensor = HeaterPowerStatus(SimpleNamespace(entry_id="advisory-fixture",
            runtime_data=SimpleNamespace(power_guard=self.guard)))
        attributes = sensor.extra_state_attributes
        self.assertFalse(attributes["circuit_enforced"])
        advisory = attributes["wall_power_budget"]["sources"]["circuit"]
        self.assertFalse(advisory["enforced"])
        self.assertEqual(advisory["status"], "advisory")
        self.assertIn("unavailable", json.dumps(advisory))
        self.assertEqual(attributes["wall_power_budget"]["qualified_sources"], [])
        self.assertEqual(attributes["wall_power_budget"]["enforced_sources"], ["input"])
        self.assertLess(len(json.dumps(attributes).encode()), 4096)
        self.assertEqual(sensor.native_value, "Ready")

    async def test_advisory_health_transitions_notify_without_numeric_churn(self):
        await self.start()
        self.guard.notify = Mock()
        self.hass.states.async_set(B.circuit_power, "400", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.guard.notify.assert_not_called()
        self.hass.states.async_set(B.circuit_power, "unavailable")
        await self.hass.async_block_till_done()
        self.assertEqual(self.guard.notify.call_count, 1)
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertEqual(self.guard.notify.call_count, 1)
        self.hass.states.async_set(B.circuit_power, "300", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertEqual(self.guard.notify.call_count, 2)
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])
