"""Verify the fixed power guard against HA state events, services, and storage."""
import asyncio
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from custom_components.house_climate.controller.bindings import example_bindings

from homeassistant.core import HomeAssistant

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.ha import HAActivationDeferred

B = example_bindings().power


class NativePowerGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.calls = []
        self.ignore_climate = False
        self.ignore_switch = False
        self.guard = power.HeaterPowerGuard(self.hass, B, live=True, entry_id="fixed-house", notify=lambda: None)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for p in self.patchers:
            p.start()
        for domain, names in (("climate", ("set_hvac_mode",)),
                              ("switch", ("turn_off", "turn_on"))):
            for service in names:
                self.hass.services.async_register(domain, service, self.handle)
        for entity, value, unit in (
            (B.circuit_power, 140, "W"), (B.input_power, 130, "W"),
            (B.output_power, 150, "W"), (B.battery, 43, "%"),
            (B.heater_current, .05, "A"),
            (B.wall_phase_a_voltage, 124, "V"), (B.wall_phase_b_voltage, 123, "V"),
        ):
            self.hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})
        self.hass.states.async_set(B.heater, "off", {"hvac_action": "off"})
        self.hass.states.async_set(B.heater_switch, "on")

    async def asyncTearDown(self):
        self.ignore_climate = self.ignore_switch = False
        await self.guard.async_close()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()
        for p in self.patchers:
            p.stop()

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "climate":
            if not self.ignore_climate:
                self.hass.states.async_set(B.heater, "off", {"hvac_action": "off"}, force_update=True)
        elif not self.ignore_switch:
            state = "on" if call.service == "turn_on" else "off"
            self.hass.states.async_set(B.heater_switch, state, force_update=True)
            if state == "off":
                self.hass.states.async_set(B.heater, "unavailable")

    async def start(self):
        await self.guard.async_load()
        self.guard.async_start()
        await self.hass.async_block_till_done()

    async def test_real_interval_checks_expiry_without_state_events(self):
        wall = [datetime.now(timezone.utc).timestamp()]
        self.guard._wall_clock = lambda: wall[0]
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        callback_errors = []
        loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
        try:
            with patch.object(power, "CHECK_SECONDS", .01), patch.object(
                self.guard, "_observe", wraps=self.guard._observe
            ) as observe:
                await self.start()
                self.assertFalse(self.guard.blocked)
                initial_checks = observe.call_count
                # No state event follows this clock advance: only HA's actual
                # interval helper, which supplies a datetime, can notice expiry.
                wall[0] += power.TELEMETRY_MAX_AGE_SECONDS + 1
                deadline = loop.time() + 2
                while observe.call_count < initial_checks + 3 and loop.time() < deadline:
                    await asyncio.sleep(.01)
                self.assertEqual(len(callback_errors), 0,
                                 [str(error.get("exception")) for error in callback_errors[:3]])
                self.assertGreaterEqual(observe.call_count, initial_checks + 3)
                self.assertTrue(self.guard.blocked)
                self.assertIn("reading not current", self.guard.diagnostics["reason"])
                self.assertFalse(self.guard.diagnostics["latched"])
                self.assertEqual(self.calls, [])
                await self.guard.async_close()
                closed_checks = observe.call_count
                await asyncio.sleep(.04)
                self.assertEqual(observe.call_count, closed_checks)
                self.assertEqual(len(callback_errors), 0,
                                 [str(error.get("exception")) for error in callback_errors[:3]])
        finally:
            await self.guard.async_close()
            loop.set_exception_handler(previous_handler)

    async def test_state_events_preserve_manual_on_off_ownership(self):
        self.hass.states.async_set(B.heater_switch, "off")
        store = Mock(async_load=AsyncMock(return_value={
            "latched": True, "switch_cut_owned": True, "restore_attempts": 0,
            "trip_wall": datetime.now(timezone.utc).timestamp(),
            "binding_identity": self.guard._binding_identity(),
        }), async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.start()
        self.assertTrue(self.guard.switch_cut_owned)
        self.hass.states.async_set(B.heater_switch, "on")
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard._manual_power_seen)
        self.assertTrue(self.guard.switch_cut_owned)
        self.hass.states.async_set(B.heater_switch, "off")
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertFalse(self.guard._manual_power_seen)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.guard.diagnostics["restore_attempts"], 0)

    async def test_manual_heat_is_shed_without_enabling_comfort_controller(self):
        await self.start()
        self.ignore_climate = True
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(B.circuit_power, "1500", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual({c[2]["entity_id"] for c in self.calls}, {B.heater, B.heater_switch})
        self.assertEqual(self.hass.states.get(B.heater_switch).state, "off")

    async def test_activation_rechecks_new_input_before_listener_runs(self):
        await self.start()
        self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.hass.states.async_set(B.input_power, "unavailable")
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(B.heater, {"hvac_mode": "heat"})

    async def test_shadow_never_sends_off_or_restoration_services(self):
        self.guard.live = False
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(B.battery, "39", {"unit_of_measurement": "%"})
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_shadow_teardown_does_not_cut_persisted_owned_power(self):
        self.guard.live = False
        store = Mock(async_load=AsyncMock(return_value={"latched": True,
                     "switch_cut_owned": True, "trip_wall": 1800000000}), async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.guard.async_load()
        self.assertTrue(await self.guard.async_close())
        self.assertEqual(self.calls, [])

    async def test_malformed_restore_counter_inhibits_instead_of_breaking_setup(self):
        store = Mock(async_load=AsyncMock(return_value={"latched": True, "restore_attempts": "bad"}),
                     async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.guard.async_load()
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.guard.diagnostics["restore_attempts"], 3)

    async def test_normal_restart_keeps_real_cutoff_ownership_and_restores_safely(self):
        self.hass.states.async_set(B.heater_switch, "off")
        old = datetime.now(timezone.utc).timestamp() - 600
        store = Mock(async_load=AsyncMock(return_value={"latched": True,
                     "switch_cut_owned": True, "restore_attempts": 0,
                     "binding_identity": self.guard._binding_identity(),
                     "trip_wall": old, "cut_changed": old, "cut_request_wall": old - 1}),
                     async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.guard.async_load()
        clock = [self.guard._clock()]
        self.guard._clock = lambda: clock[0]
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.switch_cut_owned)
        clock[0] += 61
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.states.get(B.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(B.heater).state, "off")

    async def test_restore_intent_must_be_durable_before_power_on(self):
        self.hass.states.async_set(B.heater_switch, "off")
        self.guard._latched = self.guard._cut_owned = True
        self.guard._store = Mock(async_save=AsyncMock(side_effect=OSError("storage failed")))
        await self.guard._restore()
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertNotIn("turn_on", [c[1] for c in self.calls])

    async def test_failed_final_clear_cannot_reclaim_later_manual_off_on_restart(self):
        self.hass.states.async_set(B.heater_switch, "off")
        self.guard._latched = self.guard._cut_owned = True
        self.guard._tripped_at = self.guard._clock() - 600
        self.guard._trip_wall = datetime.now(timezone.utc).timestamp() - 600
        saved = {}

        async def save(data):
            if not data["latched"]:
                raise OSError("final clear failed")
            saved.clear()
            saved.update(data)

        self.guard._store = Mock(async_save=save)
        await self.guard._restore()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(B.heater_switch).state, "on")
        self.assertFalse(saved["switch_cut_owned"])
        self.hass.states.async_set(B.heater_switch, "off")
        await self.guard.async_close()
        self.guard = power.HeaterPowerGuard(self.hass, B, live=True, entry_id="fixed-house", notify=lambda: None)
        store = Mock(async_load=AsyncMock(return_value=dict(saved)), async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.guard.async_load()
        previous_ons = sum(c[1] == "turn_on" for c in self.calls)
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertEqual(sum(c[1] == "turn_on" for c in self.calls), previous_ons)

    async def test_physical_off_requires_feedback_and_defers_close_if_unconfirmed(self):
        await self.start()
        self.ignore_climate = self.ignore_switch = True
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.states.async_set(B.circuit_power, "1500", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.power_cut_confirmed)
        self.assertFalse(await self.guard.async_close())
        self.ignore_switch = False
        self.assertTrue(await self.guard.async_close())


if __name__ == "__main__":
    unittest.main()
