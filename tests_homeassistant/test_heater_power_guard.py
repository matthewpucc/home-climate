"""Dedicated watt-meter behavior against real HA states, services and storage."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.ha import HAActivationDeferred


class NativeHeaterWattTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.b = replace(example_bindings().power, heater_current=None,
                         heater_power="sensor.fixture_heater_watts")
        self.origin = round(datetime.now(timezone.utc).timestamp(), 3)
        self.wall = self.origin
        self.calls = []
        self.ignore_climate = False
        self.ignore_switch = False
        self.climate_reports_idle = False
        self.report_meter_after_off = True
        self.delayed_on = False
        self.on_started = asyncio.Event()
        self.release_on = asyncio.Event()
        self.guard = power.HeaterPowerGuard(
            self.hass, self.b, live=True, entry_id="watt-fixture",
            notify=lambda: None, wall_clock=lambda: self.wall,
            clock=lambda: self.wall - self.origin)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", .025),
                         patch.object(power, "RESTORE_ACK_SECONDS", .04),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", .025)]
        for p in self.patchers:
            p.start()
        for domain, services in (("climate", ("set_hvac_mode",)),
                                 ("switch", ("turn_on", "turn_off"))):
            for service in services:
                self.hass.services.async_register(domain, service, self.handle)
        for entity, value, unit in (
            (self.b.circuit_power, 140, "W"), (self.b.input_power, 130, "W"),
            (self.b.output_power, 150, "W"), (self.b.battery, 90, "%"),
            (self.b.wall_phase_a_voltage, 124, "V"),
            (self.b.wall_phase_b_voltage, 123, "V"),
            (self.b.heater_power, 5.5, "W"),
        ):
            self.put(entity, value, {"unit_of_measurement": unit})
        self.put(self.b.heater, "off", {"hvac_action": "off"})
        self.put(self.b.heater_switch, "on")

    async def asyncTearDown(self):
        self.ignore_climate = self.ignore_switch = self.delayed_on = False
        self.report_meter_after_off = True
        self.release_on.set()
        await self.hass.async_block_till_done()
        await self.guard.async_close()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()
        for p in self.patchers:
            p.stop()

    def put(self, entity, value, attrs=None, *, timestamp=None, force=False):
        self.hass.states.async_set(entity, str(value), attrs or {},
                                   timestamp=self.wall if timestamp is None else timestamp,
                                   force_update=force)

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "climate":
            if not self.ignore_climate:
                self.put(self.b.heater, "heat" if self.climate_reports_idle else "off",
                         {"hvac_action": "idle" if self.climate_reports_idle else "off"},
                         timestamp=self.wall + .001, force=True)
                if self.report_meter_after_off:
                    self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"},
                             timestamp=self.wall + .002)
            return
        if self.ignore_switch:
            return
        if call.service == "turn_on" and self.delayed_on:
            self.on_started.set()
            await self.release_on.wait()
        desired = "on" if call.service == "turn_on" else "off"
        self.put(self.b.heater_switch, desired, timestamp=self.wall + .001, force=True)
        if desired == "off":
            self.put(self.b.heater, "unavailable", timestamp=self.wall + .001)

    async def start(self, *, listen=False, saved=None):
        if saved is None:
            await self.guard.async_load()
        else:
            self.store = Mock(async_load=AsyncMock(return_value=saved), async_save=AsyncMock())
            with patch("homeassistant.helpers.storage.Store", return_value=self.store):
                await self.guard.async_load()
        if listen:
            self.guard.async_start()
            await self.hass.async_block_till_done()
        else:
            self.guard._observe()
            await self.hass.async_block_till_done()

    async def refresh(self, elapsed, *, omit=(), changes=None):
        """Write a coherent snapshot before an explicit observation."""
        self.wall = self.origin + elapsed
        for entity in self.guard.entity_ids:
            if entity in omit:
                continue
            previous = self.hass.states.get(entity)
            if previous is None:
                continue
            value, attrs = (changes or {}).get(entity, (previous.state, previous.attributes))
            self.put(entity, value, attrs, force=True)
        self.guard._observe()
        await self.hass.async_block_till_done()

    def store_claim(self, *, owned=False, identity=None):
        return {"latched": True, "switch_cut_owned": owned, "restore_attempts": 0,
                "trip_wall": self.origin - 600,
                "binding_identity": self.guard._binding_identity() if identity is None else identity}

    async def test_fresh_native_off_and_post_request_watts_confirm_electrical_stop(self):
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
        await self.start()
        self.assertTrue(await self.guard.async_request_heater_off())
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertEqual(self.guard.diagnostics["heater_meter_kind"], "power")
        self.assertTrue(self.guard.diagnostics["heater_power_configured"])
        self.assertEqual([call[:2] for call in self.calls], [("climate", "set_hvac_mode")])

    async def test_cached_low_watts_are_not_post_command_proof(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        await self.start()
        # Native acknowledgement is bounded independently of meter updates.
        self.assertTrue(await asyncio.wait_for(self.guard.async_request_heater_off(), .25))
        self.assertEqual(self.guard.diagnostics["shutdown_confirmation"], "state_only")
        self.assertIsNone(self.guard._heater_meter_stopped())
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])
        self.wall += .01
        self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"})
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard.diagnostics["electrical_stop_confirmed"])
        self.assertNotIn("turn_off", [call[1] for call in self.calls])

    async def test_thermostat_idle_is_not_native_off_acknowledgement(self):
        self.climate_reports_idle = True
        self.put(self.b.heater, "heat", {"hvac_action": "idle"})
        await self.start()
        await self.guard.async_request_heater_off()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.calls])
        self.assertTrue(self.guard.power_cut_confirmed)

    async def test_late_high_watts_after_off_invokes_dedicated_cutoff(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        await self.start(listen=True)
        self.assertTrue(await self.guard.async_request_heater_off())
        self.wall += .01
        self.put(self.b.heater_power, 200, {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assertTrue(self.guard._shutdown_contradicted)
        self.assertIn(("switch", "turn_off"), [call[:2] for call in self.calls])
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")

    async def test_unchanged_watt_report_refreshes_last_reported_not_last_updated(self):
        await self.start()
        before = self.hass.states.get(self.b.heater_power).last_updated
        await self.refresh(181, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.blocked)
        self.put(self.b.heater_power, 5.5, {"unit_of_measurement": "W"})
        self.assertEqual(self.hass.states.get(self.b.heater_power).last_updated, before)
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported, before)
        self.guard._observe()
        await self.hass.async_block_till_done()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_stale_zero_watts_inhibit_activation_without_startup_trip(self):
        self.put(self.b.heater_power, 0, {"unit_of_measurement": "W"})
        await self.start()
        await self.refresh(181, omit=(self.b.heater_power,))
        with self.assertRaises(HAActivationDeferred):
            self.guard.check_activation(self.b.heater)
        self.assertIn("reading not current", self.guard.diagnostics["reason"])
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.calls, [])

    async def test_stale_high_watts_stop_running_heat_without_inventing_zero(self):
        self.put(self.b.heater_power, 1300, {"unit_of_measurement": "W"})
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.report_meter_after_off = False
        await self.start()
        await self.refresh(181, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertIn(("climate", "set_hvac_mode"), [call[:2] for call in self.calls])
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_threshold_and_untrusted_watts_never_become_stop_proof(self):
        await self.start()
        self.guard._off_request_wall = self.origin - 1
        for value, attrs, expected in (
            (99.999, {"unit_of_measurement": "W"}, True),
            (100, {"unit_of_measurement": "W"}, False),
            (0, {"unit_of_measurement": "W", "restored": True}, None),
            (0, {"unit_of_measurement": "W", "assumed_state": True}, None),
        ):
            with self.subTest(value=value, attrs=attrs):
                self.wall += .01
                self.put(self.b.heater_power, value, attrs)
                self.assertIs(self.guard._heater_meter_stopped(), expected)
        self.assertEqual(self.calls, [])

    async def test_manual_recovery_quarantines_changed_meter_then_cooldown_clears(self):
        old = example_bindings().power
        old_identity = {key: getattr(old, key) for key in
                        ("heater", "heater_switch", "heater_current", "emergency_ac_switch")}
        await self.start(saved=self.store_claim(owned=True, identity=old_identity))
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertTrue(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertFalse(self.guard.switch_cut_owned)
        await self.refresh(1)
        self.assertTrue(await self.guard.async_acknowledge_emergency())
        self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
        self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(62)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.calls, [])

    async def test_legacy_current_identity_accepts_absent_or_null_watt_key(self):
        old = example_bindings().power
        self.put(old.heater_current, .05, {"unit_of_measurement": "A"})
        identity = {key: getattr(old, key) for key in
                    ("heater", "heater_switch", "heater_current", "emergency_ac_switch")}
        for stored_identity in (identity, {**identity, "heater_power": None}):
            with self.subTest(stored_identity=stored_identity):
                await self.guard.async_close()
                self.guard = power.HeaterPowerGuard(
                    self.hass, old, live=True, entry_id="watt-fixture",
                    notify=lambda: None, wall_clock=lambda: self.wall,
                    clock=lambda: self.wall - self.origin)
                store = Mock(async_load=AsyncMock(return_value=self.store_claim(identity=stored_identity)),
                             async_save=AsyncMock())
                with patch("homeassistant.helpers.storage.Store", return_value=store):
                    await self.guard.async_load()
                self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
                self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
                self.assertEqual(self.guard.diagnostics["heater_meter_kind"], "current")
        self.assertEqual(self.calls, [])

    async def test_pre_recovery_low_watts_cannot_acknowledge_changed_wiring(self):
        identity = {**self.guard._binding_identity(), "heater_power": "sensor.retired_meter"}
        await self.start(saved=self.store_claim(identity=identity))
        await self.refresh(1, omit=(self.b.heater_power,))
        with self.assertRaisesRegex(RuntimeError, "Fresh low heater"):
            await self.guard.async_acknowledge_emergency()
        self.assertTrue(self.guard.diagnostics["manual_recovery_required"])
        self.assertEqual(self.calls, [])

    async def test_ordinary_cooldown_needs_post_off_watts_before_safe_interval(self):
        self.report_meter_after_off = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        # The persisted trip has aged out, but runtime Off proof must still
        # come from this request, not the cached pre-request low meter value.
        await self.start(saved=self.store_claim())
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertTrue(self.guard.shutdown_confirmed)
        self.assertIsNone(self.guard._heater_meter_stopped())
        await self.refresh(61, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertIsNone(self.guard._heater_meter_stopped())
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
        # The first trustworthy post-Off watt report starts a NEW safe-load
        # interval; the preceding 61 seconds cannot be credited to recovery.
        await self.refresh(62)
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(121)
        self.assertTrue(self.guard.diagnostics["latched"])
        await self.refresh(122)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertNotIn("turn_on", [call[1] for call in self.calls])

    async def test_relay_off_with_expired_meter_does_not_automatically_restore(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(181, omit=(self.b.heater_power,))
        await self.refresh(242, omit=(self.b.heater_power,))
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertNotIn("turn_on", [call[1] for call in self.calls])

    async def test_owned_restore_requires_new_low_watts_after_power_on_and_off_request(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(61)
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "on")
        self.assertEqual(self.hass.states.get(self.b.heater).state, "off")
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertIn(("switch", "turn_on"), [call[:2] for call in self.calls])
        self.assertGreater(self.hass.states.get(self.b.heater_power).last_reported.timestamp(),
                           self.origin + 61)

    async def test_cached_low_watts_cannot_complete_owned_restore(self):
        self.report_meter_after_off = False
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        await self.refresh(61)
        self.assertIn(("switch", "turn_on"), [call[:2] for call in self.calls])
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertTrue(self.guard.diagnostics["latched"])
        self.assertFalse(self.guard.diagnostics["electrical_stop_confirmed"])

    async def test_late_power_on_is_compensated_and_never_left_energized(self):
        self.put(self.b.heater_switch, "off")
        await self.start(saved=self.store_claim(owned=True))
        self.delayed_on = True
        self.wall = self.origin + 61
        task = self.hass.async_create_task(self.guard._restore(), eager_start=False)
        await asyncio.wait_for(self.on_started.wait(), .5)
        await asyncio.sleep(.04)
        self.assertTrue(self.guard.diagnostics["power_restore_inflight"])
        self.assertFalse(await self.guard.async_close())
        self.release_on.set()
        await self.hass.async_block_till_done()
        await asyncio.gather(task, return_exceptions=True)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.b.heater_switch).state, "off")
        self.assertFalse(self.guard.diagnostics["power_restore_inflight"])
        self.assertTrue(self.guard.blocked)

    async def test_preview_never_issues_watt_trip_or_restoration_commands(self):
        self.guard.live = False
        self.put(self.b.heater, "heat", {"hvac_action": "heating"})
        self.put(self.b.battery, 40, {"unit_of_measurement": "%"})
        await self.start(listen=True)
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.calls, [])

