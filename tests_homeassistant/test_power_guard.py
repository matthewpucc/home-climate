"""Verify the fixed power guard against HA state events, services, and storage."""
import asyncio
from dataclasses import replace
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
        self.wall = None
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
        stamp = self.wall[0] + .001 if self.wall is not None else None
        if call.domain == "climate":
            if not self.ignore_climate:
                self.hass.states.async_set(B.heater, "off", {"hvac_action": "off"},
                                           force_update=True, timestamp=stamp)
        elif not self.ignore_switch:
            state = "on" if call.service == "turn_on" else "off"
            self.hass.states.async_set(B.heater_switch, state, force_update=True, timestamp=stamp)
            if state == "off":
                self.hass.states.async_set(B.heater, "unavailable", timestamp=stamp)

    def use_source_clock(self):
        """Advance actual HA report timestamps without sleeping for qualification."""
        self.origin = round(datetime.now(timezone.utc).timestamp(), 3)
        self.wall = [self.origin]
        monotonic = self.guard._clock()
        self.guard._wall_clock = lambda: self.wall[0]
        self.guard._clock = lambda: monotonic + self.wall[0] - self.origin

    async def report(self, elapsed, changes=None, *, only=None):
        self.wall[0] = self.origin + elapsed
        changes = changes or {}
        # Keep explicit source packets ordered instead of depending on a
        # frozenset's per-process iteration order. Other source refreshes are
        # separate events, not an atomic whole-device snapshot.
        entities = (only if only is not None else
                    (*changes, *(entity for entity in sorted(self.guard.entity_ids)
                                  if entity not in changes)))
        for entity in entities:
            old = self.hass.states.get(entity)
            if old is None:
                continue
            value, attrs = changes.get(entity, (old.state, old.attributes))
            self.hass.states.async_set(entity, str(value), attrs,
                                       force_update=True, timestamp=self.wall[0])
        await self.hass.async_block_till_done()

    async def high_wall(self, elapsed, *, entity=None, watts=1600):
        await self.report(elapsed, {(entity or B.circuit_power):
                                   (watts, {"unit_of_measurement": "W"})})

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
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.ignore_climate = True
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for elapsed in (1, 16, 31):
            await self.high_wall(elapsed)
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
        self.use_source_clock()
        await self.report(0, {B.heater_switch: ("off", {})})
        old = self.origin - 600
        store = Mock(async_load=AsyncMock(return_value={"latched": True,
                     "switch_cut_owned": True, "restore_attempts": 0,
                     "binding_identity": self.guard._binding_identity(),
                     "trip_wall": old, "cut_changed": old, "cut_request_wall": old - 1}),
                     async_save=AsyncMock())
        with patch("homeassistant.helpers.storage.Store", return_value=store):
            await self.guard.async_load()
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.calls, [])
        # A saved claim does not authenticate a startup-recreated Off state.
        # A real post-load relay report starts the new safe-load interval.
        await self.report(1, only=(B.heater_switch,))
        self.guard._observe()
        await self.hass.async_block_till_done()
        sensors = tuple(entity for entity in sorted(self.guard.entity_ids)
                        if entity.startswith("sensor."))
        for elapsed in (31, 60):
            await self.report(elapsed, only=sensors)
            self.guard._observe()
            await self.hass.async_block_till_done()
            self.assertTrue(self.guard.blocked)
        await self.report(62, only=sensors)
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
        self.hass.states.async_set(B.output_power, "2000", {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.power_cut_confirmed)
        self.assertFalse(await self.guard.async_close())
        self.ignore_switch = False
        self.assertTrue(await self.guard.async_close())

    async def test_normal_heating_below_voltage_scaled_budget_is_not_shed(self):
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for entity, value in ((B.circuit_power, "1434"), (B.input_power, "1366"),
                              (B.output_power, "1856")):
            self.hass.states.async_set(entity, value, {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])

    async def test_pending_wall_load_defers_new_heat_without_stopping_running_heat(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        await self.high_wall(1)
        # Worker overlay treats blocked as an instruction to stop. Pending
        # evidence must defer only a new start, preserving the running call.
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.assertEqual(self.calls, [])
        # Exercise the real worker overlay boundary: pending power evidence
        # must not flip the engine's auxiliary-safe input to Off.
        from threading import Event
        from types import SimpleNamespace
        from custom_components.house_climate.house_policy import house_configuration
        from custom_components.house_climate.runtime import NativeAPI
        config = house_configuration(bindings=example_bindings())
        interlock = config.entity("auxiliary_probe_safe", "binary_sensor")
        api = NativeAPI(self.hass, True, Event(), config)
        api.controls = SimpleNamespace(
            config=config,
            overlay=lambda states: {**states, interlock: {"state": "on"}},
        )
        api.power_guard = self.guard
        worker_states = await asyncio.to_thread(api.states)
        self.assertEqual(worker_states[interlock]["state"], "on")
        await self.high_wall(20, watts=140)
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])

    async def test_same_unchanged_power_reports_qualify_at_voltage_scaled_boundary(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        await self.high_wall(1, entity=B.input_power, watts=1476)
        unchanged_since = self.hass.states.get(B.input_power).last_updated
        for elapsed in (16, 30):
            self.wall[0] = self.origin + elapsed
            self.hass.states.async_set(B.input_power, "1476", {"unit_of_measurement": "W"},
                                       timestamp=self.wall[0])
            self.guard._observe()
            await self.hass.async_block_till_done()
            self.assertEqual(self.hass.states.get(B.input_power).last_updated, unchanged_since)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        self.wall[0] = self.origin + 31
        self.hass.states.async_set(B.input_power, "1476", {"unit_of_measurement": "W"},
                                   timestamp=self.wall[0])
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(B.input_power).last_updated, unchanged_since)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])
        self.assertEqual(self.hass.states.get(B.heater).state, "off")

    async def test_alternating_input_and_circuit_exceedances_do_not_combine(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for elapsed, circuit, grid in ((1, 1600, 130), (16, 140, 1600),
                                       (31, 1600, 130), (46, 140, 1600)):
            await self.report(elapsed, {
                B.circuit_power: (circuit, {"unit_of_measurement": "W"}),
                B.input_power: (grid, {"unit_of_measurement": "W"}),
            })
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])

    async def test_cached_polls_and_voltage_reports_do_not_count_as_power_reports(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        for elapsed in (16, 31, 46):
            await self.report(elapsed, only=(B.wall_phase_a_voltage, B.wall_phase_b_voltage))
            self.guard._observe()
            await self.hass.async_block_till_done()
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        await self.high_wall(47)
        self.assertFalse(self.guard.diagnostics["latched"])
        await self.high_wall(62)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_hidden_reporting_gap_restarts_wall_qualification(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        await self.high_wall(16)
        # Recovery of stale voltage creates a new validity barrier. Reports
        # timestamped exactly at that barrier cannot count as later evidence.
        await self.high_wall(300)
        self.assertFalse(self.guard.diagnostics["latched"])
        for elapsed in (301, 316):
            await self.high_wall(elapsed)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        await self.high_wall(331)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_coalesced_low_then_high_event_resets_wall_qualification(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1, entity=B.input_power)
        await self.high_wall(16, entity=B.input_power)
        self.wall[0] = self.origin + 31
        self.hass.states.async_set(B.input_power, "130", {"unit_of_measurement": "W"},
                                   force_update=True, timestamp=self.origin + 30)
        self.hass.states.async_set(B.input_power, "1600", {"unit_of_measurement": "W"},
                                   force_update=True, timestamp=self.origin + 31)
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])
        for elapsed in (32, 47):
            await self.high_wall(elapsed, entity=B.input_power)
            self.assertFalse(self.guard.diagnostics["latched"])
        await self.high_wall(62, entity=B.input_power)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_out_of_order_power_report_resets_wall_qualification(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        await self.high_wall(16)
        self.wall[0] = self.origin + 26
        self.hass.states.async_set(B.circuit_power, "1600", {"unit_of_measurement": "W"},
                                   force_update=True, timestamp=self.origin + 10)
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.diagnostics["latched"])
        for elapsed in (31, 46):
            await self.high_wall(elapsed)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        await self.high_wall(61)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_stale_voltage_interrupts_wall_qualification_without_nominal_fallback(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        await self.high_wall(16)
        self.wall[0] = self.origin + 20
        self.hass.states.async_set(B.wall_phase_a_voltage, "123", {"unit_of_measurement": "V"},
                                   force_update=True,
                                   timestamp=self.origin - power.TELEMETRY_MAX_AGE_SECONDS - 1)
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])
        await self.high_wall(31)
        self.assertFalse(self.guard.diagnostics["latched"])
        for elapsed in (32, 47):
            await self.high_wall(elapsed)
            self.assertFalse(self.guard.diagnostics["latched"])
        await self.high_wall(62)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_restored_or_assumed_voltage_cannot_supply_a_wall_budget(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        for elapsed, flag in ((1, "restored"), (3, "assumed_state")):
            await self.report(elapsed, {
                B.wall_phase_a_voltage: (123, {"unit_of_measurement": "V", flag: True}),
            })
            self.assertTrue(self.guard.blocked)
            self.assertIsNone(self.guard._reading.input_budget_w)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
            await self.report(elapsed + 1, {
                B.wall_phase_a_voltage: (123, {"unit_of_measurement": "V"}),
            })
            self.assertFalse(self.guard.blocked)

    async def test_changed_value_at_duplicate_timestamp_cannot_count_as_a_new_report(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        await self.high_wall(16)
        self.hass.states.async_set(B.circuit_power, "1601", {"unit_of_measurement": "W"},
                                   force_update=True, timestamp=self.origin + 16)
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.diagnostics["latched"])
        for elapsed in (31, 46):
            await self.high_wall(elapsed)
            self.assertFalse(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        await self.high_wall(61)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_voltage_increase_invalidates_earlier_insufficient_power(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1, watts=1500)
        await self.high_wall(16, watts=1500)
        await self.report(31, {
            B.wall_phase_a_voltage: (126, {"unit_of_measurement": "V"}),
            B.wall_phase_b_voltage: (126, {"unit_of_measurement": "V"}),
        }, only=(B.wall_phase_a_voltage, B.wall_phase_b_voltage))
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])
        for elapsed in (32, 47):
            await self.high_wall(elapsed)
            self.assertFalse(self.guard.diagnostics["latched"])
        await self.high_wall(62)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_voltage_decrease_does_not_backdate_a_new_exceedance(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1, watts=1450)
        await self.high_wall(16, watts=1450)
        await self.report(31, {
            B.wall_phase_a_voltage: (120, {"unit_of_measurement": "V"}),
            B.wall_phase_b_voltage: (120, {"unit_of_measurement": "V"}),
        }, only=(B.wall_phase_a_voltage, B.wall_phase_b_voltage))
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])
        for elapsed in (32, 47):
            await self.high_wall(elapsed, watts=1450)
            self.assertFalse(self.guard.diagnostics["latched"])
        await self.high_wall(62, watts=1450)
        self.assertTrue(self.guard.diagnostics["latched"])

    async def test_output_spike_remains_immediate_during_pending_wall_load(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        self.assertFalse(self.guard.diagnostics["latched"])
        await self.report(2, {B.output_power: (2000, {"unit_of_measurement": "W"})})
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls[0][:2], ("climate", "set_hvac_mode"))

    async def test_battery_reserve_remains_immediate_during_pending_wall_load(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        await self.high_wall(1)
        self.assertFalse(self.guard.diagnostics["latched"])
        await self.report(2, {B.battery: (40, {"unit_of_measurement": "%"})})
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls[0][:2], ("climate", "set_hvac_mode"))

    async def test_confirmed_heater_off_does_not_cut_power_for_continuing_charging(self):
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.hass.states.async_set(B.heater, "heat", {"hvac_action": "heating"})
        for elapsed in (1, 16, 31):
            await self.report(elapsed, {
                B.circuit_power: (1600, {"unit_of_measurement": "W"}),
                B.input_power: (1600, {"unit_of_measurement": "W"}),
            })
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.states.get(B.heater).state, "off")
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])
        for elapsed in (46, 61, 91):
            await self.report(elapsed)
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.power_cut_confirmed)
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])

    async def test_fresh_already_off_heater_needs_no_command_for_charging_only_trip(self):
        await self.guard.async_close()
        shared_ac = "switch.fixture_shared_ac"
        bindings = replace(B, heater_switch=None, heater_current=None,
                           emergency_ac_switch=shared_ac,
                           emergency_ac_fallback_enabled=True)
        self.guard = power.HeaterPowerGuard(
            self.hass, bindings, live=True, entry_id="direct-fixture", notify=lambda: None)
        self.hass.states.async_set(shared_ac, "on")
        self.use_source_clock()
        await self.report(0)
        await self.start()
        # The heater is genuinely reported Off before the load episode. A
        # redundant Off would not produce new feedback on this device.
        self.ignore_climate = True
        for elapsed in (1, 16, 31):
            await self.high_wall(elapsed, entity=B.input_power)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.states.get(B.heater).state, "off")
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertFalse(self.guard.power_cut_confirmed)
        self.assertFalse(self.guard.diagnostics["emergency_ac_latched"])
        self.assertEqual(self.calls, [])
        # Clear charging load and preserve fresh reports across the ordinary
        # safe-load and trip cooldown. Recovery must not energize anything.
        for elapsed in (46, 106, 166, 226, 286):
            await self.high_wall(elapsed, entity=B.input_power, watts=130)
            self.assertTrue(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        await self.high_wall(332, entity=B.input_power, watts=130)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.states.get(shared_ac).state, "on")
        self.assertEqual(self.calls, [])

    async def passive_meter_trip(self):
        """Qualify a wall-only event with genuine native Off and low current."""
        self.use_source_clock()
        await self.report(0)
        await self.start()
        self.ignore_climate = True
        for elapsed in (1, 16, 31):
            await self.high_wall(elapsed, entity=B.input_power)
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.shutdown_confirmed)
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertEqual(self.calls, [])

    async def assert_untrusted_passive_current_holds_latch(self, flag):
        await self.passive_meter_trip()
        # The meter still has a low, numerically fresh value, but the source
        # explicitly says it is not trustworthy. Fresh native Off alone must
        # not replace configured electrical evidence at ordinary recovery.
        for elapsed in (46, 106, 166, 226, 286, 332):
            await self.report(elapsed, {
                B.input_power: (130, {"unit_of_measurement": "W"}),
                B.heater_current: (.05, {"unit_of_measurement": "A", flag: True}),
            })
            self.assertTrue(self.guard.diagnostics["latched"])
            self.assertTrue(self.guard.blocked)
            self.assertFalse(self.guard.shutdown_confirmed)
            self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
            self.assertEqual(self.calls, [])

    async def test_assumed_passive_current_cannot_clear_cooldown_latch(self):
        await self.assert_untrusted_passive_current_holds_latch("assumed_state")

    async def test_restored_passive_current_cannot_clear_cooldown_latch(self):
        await self.assert_untrusted_passive_current_holds_latch("restored")

    async def test_recovered_passive_current_requires_new_stable_safe_interval(self):
        await self.assert_untrusted_passive_current_holds_latch("assumed_state")
        # Five minutes of numeric low current that is explicitly untrusted
        # cannot bank the safe-load interval. Its first real packet starts a
        # new continuously trusted sixty seconds after cooldown has elapsed.
        await self.report(333, {
            B.heater_current: (.05, {"unit_of_measurement": "A"}),
        })
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.shutdown_confirmed)
        await self.report(392)
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.report(393)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_pre_boundary_passive_current_cannot_clear_cooldown_latch(self):
        await self.passive_meter_trip()
        boundary = self.guard._wall_observed_off_wall
        self.assertIsNotNone(boundary)
        # Shorten cooldown only, retaining normal freshness, to distinguish a
        # fresh-but-out-of-order current packet from a simply expired one.
        with patch.object(power, "COOLDOWN_SECONDS", 45), patch.object(power, "SAFE_LOAD_SECONDS", 15):
            self.wall[0] = self.origin + 46
            self.hass.states.async_set(B.heater_current, ".05", {"unit_of_measurement": "A"},
                                       force_update=True, timestamp=boundary - 1)
            await self.hass.async_block_till_done()
            # Wall input is still high at this point. Repeated qualification
            # must not adopt the older meter packet as a new passive boundary.
            self.assertEqual(self.guard._wall_observed_off_wall, boundary)
            self.assertFalse(self.guard.shutdown_confirmed)
            for elapsed in (46, 61, 77):
                await self.report(elapsed, {B.input_power: (130, {"unit_of_measurement": "W"})},
                                  only=tuple(entity for entity in sorted(self.guard.entity_ids)
                                             if entity != B.heater_current))
                self.assertTrue(self.guard.diagnostics["latched"])
                self.assertTrue(self.guard.blocked)
                self.assertFalse(self.guard.shutdown_confirmed)
                self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
                self.assertEqual(self.calls, [])

    async def test_stale_passive_current_cannot_clear_cooldown_latch(self):
        await self.passive_meter_trip()
        for elapsed in (46, 106, 166, 226, 286, 332):
            await self.report(elapsed, {B.input_power: (130, {"unit_of_measurement": "W"})},
                              only=tuple(entity for entity in sorted(self.guard.entity_ids)
                                         if entity != B.heater_current))
            self.assertTrue(self.guard.diagnostics["latched"])
            self.assertEqual(self.calls, [])
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.shutdown_confirmed)
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")

    async def test_high_current_revokes_passive_off_and_requests_shutdown(self):
        await self.passive_meter_trip()
        self.ignore_switch = True
        await self.report(46, {
            B.input_power: (130, {"unit_of_measurement": "W"}),
            B.heater_current: (10, {"unit_of_measurement": "A"}),
        })
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard._shutdown_contradicted)
        self.assertFalse(self.guard.shutdown_confirmed)
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "unconfirmed")
        # Late contradictory current preserves the existing immediate
        # dedicated-cutoff path rather than trusting the passive Off report.
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.calls])


if __name__ == "__main__":
    unittest.main()
