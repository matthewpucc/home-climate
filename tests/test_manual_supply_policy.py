"""Corroborating supply and explicit manual ownership must remain distinct."""
from datetime import datetime, timezone
from dataclasses import replace
import unittest
from unittest.mock import Mock

from tests import test_power_guard as f

B, power = f.B, f.power


class CorroboratingSupplyTests(unittest.TestCase):
    def setUp(self):
        self.clock = f.Clock()
        self.states = f.measurements(self.clock)

    def test_unchanged_event_driven_on_uses_current_supply_not_fake_freshness(self):
        ac = self.states[B.ac_output_switch]
        ac['attributes'] = {}
        old = datetime.fromtimestamp(self.clock.wall - 86400, timezone.utc).isoformat()
        ac['last_reported'] = ac['last_changed'] = old
        self.states[B.output_power]['state'] = '0'
        reading = power.evaluate_power(self.states, B, self.clock.wall)
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.supply_evidence, 'corroborating_reports')
        self.assertFalse(reading.supplying_loads)
        self.assertEqual(ac['last_reported'], old)
        self.clock.advance(181)
        self.assertIsNotNone(power.evaluate_power(self.states, B, self.clock.wall).issue)

    def test_invalid_ac_state_cannot_be_replaced_by_positive_output(self):
        for invalid in ('unavailable', 'unknown', 'off'):
            with self.subTest(invalid=invalid):
                self.states[B.ac_output_switch]['state'] = invalid
                self.assertIsNotNone(power.evaluate_power(self.states, B, self.clock.wall).issue)
        for flag in ('restored', 'assumed_state'):
            self.states = f.measurements(self.clock)
            self.states[B.ac_output_switch]['attributes'][flag] = True
            self.assertIsNotNone(power.evaluate_power(self.states, B, self.clock.wall).issue)


class ManualReserveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = f.PowerGuardAsyncTests.asyncSetUp
    asyncTearDown = f.PowerGuardAsyncTests.asyncTearDown
    settle = f.PowerGuardAsyncTests.settle

    async def test_emergency_enabled_direct_path_uses_same_unchanged_on_contract(self):
        self.guard.bindings = replace(B, heater_switch=None,
            emergency_ac_switch=B.ac_output_switch, emergency_ac_fallback_enabled=True)
        ac = self.hass.data[B.ac_output_switch]
        ac['attributes'] = {}
        old = datetime.fromtimestamp(self.clock.wall - 86400, timezone.utc).isoformat()
        ac['last_reported'] = ac['last_changed'] = old
        self.guard._observe()
        self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})
        self.assertFalse(self.guard.blocked)
        self.assertTrue(self.guard._ac_observed('on'))
        # Explicit restoration still needs a new post-emergency report.
        self.assertFalse(self.guard._ac_observed('on', after=self.clock.wall - 1))
        self.assertEqual(self.hass.calls, [])

    async def test_manual_reserve_alert_does_not_command_stop_or_claim_new_latch(self):
        self.guard.manual_override = lambda: True
        self.guard.alerts = Mock()
        self.hass.set(B.heater, 'heat', {'hvac_action': 'heating'})
        self.hass.set(B.battery, 20)
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard._latched)
        self.assertEqual(self.hass.calls, [])
        notices = self.guard.alerts.update.call_args_list
        self.assertTrue(any(c.args[0] == 'auxiliary_low_battery' and c.args[1]
                            and c.kwargs.get('severity') == 'critical' for c in notices))
        with self.assertRaises(Exception):
            self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})
        # Losing a report is not evidence that the low reserve recovered.
        self.hass.set(B.battery, 'unavailable')
        self.guard._observe()
        self.assertTrue(self.guard._low_battery_alert_active)
        self.assertEqual(self.hass.calls, [])

    async def test_manual_claim_retains_saved_cut_obligation_without_policy_recut(self):
        self.guard.manual_override = lambda: True
        self.guard._cut_owned = self.guard._latched = True
        self.guard._retained_cut_claim_after = self.clock.wall - 1
        self.hass.set(B.heater, 'heat', {'hvac_action': 'heating'})
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard._cut_owned)
        self.assertTrue(self.guard._latched)
        self.assertIsNotNone(self.guard.manual_return_issue)
        self.assertEqual(self.hass.calls, [])
        # Graceful restart is not a policy-driven reclaim of manual heat.
        # The saved claim remains unresolved for explicit return/recovery.
        self.assertTrue(await self.guard.async_close())
        self.assertTrue(self.guard._cut_owned)
        self.assertEqual(self.hass.calls, [])

    async def test_manual_takeover_cannot_suppress_actual_failed_shutdown(self):
        self.guard.manual_override = lambda: True
        self.guard._shutdown_fault = self.guard._manual_recovery_required = True
        self.hass.set(B.heater, 'heat', {'hvac_action': 'heating'})
        self.guard._observe()
        await self.settle()
        self.assertTrue(any(c[1] == 'set_hvac_mode' and c[2].get('hvac_mode') == 'off'
                            for c in self.hass.calls))
        self.assertIsNotNone(self.guard.manual_return_issue)

    async def test_reserve_inhibition_alone_does_not_require_fault_acknowledgement(self):
        self.hass.set(B.battery, 20)
        self.guard._latched = True
        self.assertIsNone(self.guard.manual_return_issue)

    async def ordinary_recovered_stop(self):
        self.hass.set(B.battery, 20)
        self.guard._observe()
        await self.settle()
        self.hass.set(B.battery, 80)
        self.hass.set(B.heater, 'off', {'hvac_action': 'off'})
        self.hass.set(B.heater_current, .05)
        self.guard._observe()
        await self.settle()

    async def test_proved_policy_stop_allows_fan_only_until_heat_hold_completes(self):
        await self.ordinary_recovered_stop()
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.fan_blocked)
        self.guard.check_activation(B.heater, {'hvac_mode': 'fan_only'})
        with self.assertRaises(Exception):
            self.guard.check_activation(B.heater, {'hvac_mode': 'heat'})
        before = list(self.hass.calls)
        self.hass.set(B.heater, 'fan_only', {'hvac_action': 'fan'})
        self.hass.set(B.heater_current, .05)
        self.guard._observe()
        await self.settle()
        self.assertEqual(self.hass.calls, before)
        self.assertFalse(self.guard.fan_blocked)
        self.clock.advance(power.SAFE_LOAD_SECONDS + 1)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.calls, before)

    async def test_fan_exception_never_bypasses_missing_stop_or_fault_evidence(self):
        await self.ordinary_recovered_stop()
        for field in ('_shutdown_fault', '_shutdown_contradicted', '_cut_owned',
                      '_legacy_cooldown', '_manual_recovery_required'):
            with self.subTest(field=field):
                setattr(self.guard, field, True)
                self.assertTrue(self.guard.fan_blocked)
                setattr(self.guard, field, False)
        self.hass.set(B.heater_current, 'unavailable')
        self.assertTrue(self.guard.fan_blocked)
        self.hass.set(B.heater_current, .05)
        self.hass.set(B.battery, 40)
        self.assertTrue(self.guard.fan_blocked)

    async def test_lost_supply_during_fan_only_stops_entire_auxiliary(self):
        await self.ordinary_recovered_stop()
        self.guard.check_activation(B.heater, {'hvac_mode': 'fan_only'})
        self.hass.set(B.heater, 'fan_only', {'hvac_action': 'fan'})
        self.hass.set(B.battery, 40)
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.fan_blocked)
        self.assertEqual(self.hass.data[B.heater]['state'], 'off')

    async def test_stop_evidence_distinguishes_missing_low_and_contradictory_reports(self):
        self.guard._off_request_wall = self.clock.wall
        self.clock.advance(1)
        self.hass.set(B.heater, 'off', {'hvac_action': 'off'})
        self.hass.set(B.heater_current, 'unavailable')
        self.assertEqual(self.guard.diagnostics['shutdown_evidence'], 'reported_off_unverified')
        self.hass.set(B.heater_current, .05)
        self.assertEqual(self.guard.diagnostics['shutdown_evidence'], 'corroborated_off')
        self.guard._shutdown_contradicted = True
        self.assertEqual(self.guard.diagnostics['shutdown_evidence'], 'contradictory')
