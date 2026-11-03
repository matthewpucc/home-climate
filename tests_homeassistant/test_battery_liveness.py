"""Unchanged SOC requires observed activity from its registered source device."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import math
from pathlib import Path
import tempfile
import threading
from types import MappingProxyType
import unittest
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate import power_guard as power
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.runtime import NativeAPI


B = example_bindings().power


class BatteryLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.registry = er.async_get(self.hass)
        self.owner = self.entry("bluetti", "battery-owner")
        self.device = self.make_device(self.owner, "battery")
        for entity in (B.battery, B.input_power, B.output_power):
            self.register(entity, self.owner, self.device.id)
        self.origin = round(datetime.now(timezone.utc).timestamp(), 3)
        self.wall = self.origin
        self.write(B.battery, "72", -3600, {"unit_of_measurement": "%"})
        for entity, value, unit in (
            (B.input_power, "130", "W"), (B.output_power, "150", "W"),
            (B.circuit_power, "140", "W"), (B.heater_current, "0.05", "A"),
            (B.wall_phase_a_voltage, "124", "V"),
            (B.wall_phase_b_voltage, "123", "V"),
        ):
            self.write(entity, value, 0, {"unit_of_measurement": unit})
        self.write(B.ac_output_switch, "on", 0)
        self.write(B.heater_switch, "on", 0)
        self.write(B.heater, "off", 0, {"hvac_action": "off"})
        self.guards = []
        self.guard = self.make_guard()
        self.guard.async_start()
        await self.hass.async_block_till_done()

    async def asyncTearDown(self):
        for guard in self.guards:
            await guard.async_close()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def entry(self, domain, entry_id):
        entry = ConfigEntry(
            version=1, minor_version=1, domain=domain, entry_id=entry_id,
            title="Battery source", data={}, options={}, source="user",
            unique_id=None, discovery_keys=MappingProxyType({}),
            state=ConfigEntryState.LOADED, subentries_data=None,
        )
        self.hass.config_entries._entries[entry_id] = entry
        return entry

    def make_device(self, owner, identifier):
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={(owner.domain, identifier)},
        )

    def register(self, entity, owner, device_id, *, platform=None):
        domain, object_id = entity.split(".", 1)
        item = self.registry.async_get_or_create(
            domain, platform or owner.domain, entity,
            suggested_object_id=object_id, config_entry=owner,
            device_id=device_id,
        )
        self.assertEqual(item.entity_id, entity)
        return item

    def make_guard(self):
        guard = power.HeaterPowerGuard(
            self.hass, B, live=False, entry_id="battery-test", notify=lambda: None,
            wall_clock=lambda: self.wall,
        )
        self.guards.append(guard)
        return guard

    def write(self, entity, value, elapsed, attrs=None, *, force=False):
        if attrs is None:
            old = self.hass.states.get(entity)
            attrs = dict(old.attributes) if old else {}
        self.hass.states.async_set(
            entity, value, attrs, force_update=force,
            timestamp=self.origin + elapsed,
        )

    async def report(self, entity, value, elapsed, attrs=None, *, force=False):
        self.wall = self.origin + elapsed
        self.write(entity, value, elapsed, attrs, force=force)
        await self.hass.async_block_till_done()
        return self.reading()

    def reading(self, guard=None):
        guard = guard or self.guard
        return guard._evaluate_power(guard._states(), B, self.wall)

    def assert_unverified(self, reading):
        self.assertEqual(reading.battery_evidence, "unverified")
        self.assertTrue(reading.telemetry_missing)
        self.assertIsNotNone(reading.issue)

    async def establish(self, entity=None):
        return await self.report(entity or B.input_power, "131", 1)

    async def test_startup_cached_numeric_values_establish_only_a_baseline(self):
        self.assert_unverified(self.reading())
        for _ in range(3):
            self.guard._observe()
            self.assert_unverified(self.reading())
        reading = await self.establish()
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_percent, 72)
        self.assertEqual(reading.battery_evidence, "same_device_activity")
        self.assertEqual(reading.battery_reported_at, self.origin - 3600)
        self.assertEqual(reading.battery_activity_at, self.origin + 1)
        self.assertEqual(reading.battery_activity_entity, B.input_power)
        self.assertEqual(self.hass.states.get(B.battery).last_reported.timestamp(),
                         self.origin - 3600)
        self.assertFalse(self.guard.blocked)
        self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        self.assertEqual(self.guard.diagnostics["battery_evidence"], "same_device_activity")
        self.assertEqual(self.guard.diagnostics["battery_reported_at"], self.origin - 3600)

    async def test_output_numeric_change_can_also_witness_unchanged_soc(self):
        reading = await self.establish(B.output_power)
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_evidence, "same_device_activity")
        self.assertEqual(reading.battery_activity_entity, B.output_power)

    async def test_low_unchanged_soc_still_enforces_the_reserve(self):
        # This retained SOC remains old; only a subsequent watt change is live.
        self.write(B.battery, "39", -3599)
        await self.hass.async_block_till_done()
        reading = await self.establish()
        self.assertEqual(reading.battery_percent, 39)
        self.assertEqual(reading.battery_evidence, "same_device_activity")
        self.assertIn("40%", reading.issue)
        self.assertFalse(reading.safe_to_rearm)
        self.assertTrue(self.guard.blocked)

    async def test_fresh_last_reported_soc_survives_old_last_updated(self):
        old = self.hass.states.get(B.battery).last_updated
        await self.report(B.battery, "72", 1)
        state = self.hass.states.get(B.battery)
        self.assertEqual(state.last_updated, old)
        self.assertEqual(state.last_reported.timestamp(), self.origin + 1)
        reading = self.reading()
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_evidence, "ha_report")
        self.assertEqual(reading.battery_reported_at, self.origin + 1)

    async def test_identical_forced_attribute_and_format_updates_do_not_renew(self):
        await self.establish()
        for elapsed, value, attrs, force in (
            (50, "131", None, False),
            (100, "131", None, True),
            (150, "131", {"unit_of_measurement": "W", "friendly_name": "Updated"}, False),
            (170, "131.0", {"unit_of_measurement": "W"}, False),
            (175, "0.131", {"unit_of_measurement": "kW"}, False),
        ):
            reading = await self.report(B.input_power, value, elapsed, attrs, force=force)
            self.assertEqual(reading.battery_activity_at, self.origin + 1)
        # Current output corroboration cannot extend the activity witness.
        reading = await self.report(B.output_power, "150", 182, force=True)
        self.assert_unverified(reading)

    async def test_numeric_changes_need_strictly_newer_source_timestamps(self):
        await self.establish()
        for now, reported, value in ((50, 1, "132"), (60, 49, "133")):
            self.wall = self.origin + now
            self.write(B.input_power, value, reported)
            await self.hass.async_block_till_done()
            self.assertEqual(self.reading().battery_activity_at, self.origin + 1)
        reading = await self.report(B.input_power, "134", 61)
        self.assertEqual(reading.battery_activity_at, self.origin + 61)

    async def test_identical_reports_cannot_create_the_first_witness(self):
        await self.report(B.input_power, "130", 1, force=True)
        await self.report(B.input_power, "130", 2,
                          {"unit_of_measurement": "W", "friendly_name": "Battery input"})
        await self.report(B.input_power, "130.0", 3)
        self.assert_unverified(self.reading())

    async def test_witness_expires_at_180_seconds_despite_cached_checks(self):
        await self.establish()
        await self.report(B.output_power, "150", 181, force=True)
        self.assertEqual(self.reading().battery_evidence, "same_device_activity")
        self.wall = self.origin + 181.01
        self.guard._observe()
        self.assert_unverified(self.reading())

    async def test_queued_activation_rechecks_expired_witness_before_service_dispatch(self):
        await self.establish()
        self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
        cfg = replace(Config.load(Path(__file__).resolve().parents[1] /
                                  "tests/fixtures/legacy.toml"), heater_entity=B.heater)
        api = NativeAPI(self.hass, True, threading.Event(), cfg)
        api.power_guard = self.guard
        calls = []

        async def handle(call):
            calls.append(call)

        self.hass.services.async_register("climate", "set_hvac_mode", handle)
        entered, release = asyncio.Event(), asyncio.Event()
        original = api._async_service_job

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        request = None
        with patch.object(api, "_async_service_job", side_effect=held):
            try:
                request = asyncio.create_task(asyncio.to_thread(
                    api.service, "climate", "set_hvac_mode", entity_id=B.heater,
                    hvac_mode="heat"))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(api.service_jobs({B.heater}))
                # HA records identical reports without state_changed events.
                # Output stays current while the input-earned witness expires;
                # only the actual queued dispatch rechecks this cached decision.
                self.wall = self.origin + 182
                self.write(B.output_power, "150", 182)
                self.write(B.input_power, "131", 182)
                self.write(B.heater_current, "0.05", 182)
                self.assertFalse(self.guard.blocked)
                release.set()
                with self.assertRaises(HAActivationDeferred):
                    await asyncio.wait_for(request, 2)
                self.assertEqual(calls, [])
                self.assertTrue(self.guard.blocked)
                self.assertEqual(self.hass.states.get(B.heater).state, "off")
                self.assertEqual(self.hass.states.get(B.battery).last_reported.timestamp(),
                                 self.origin - 3600)
                self.assertFalse(api.service_jobs({B.heater}))
            finally:
                release.set()
                if request is not None and not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async def test_activity_cannot_excuse_stale_output(self):
        await self.establish()
        reading = await self.report(B.input_power, "132", 181)
        self.assertIsNotNone(reading.issue)
        self.assertTrue(reading.telemetry_missing)
        self.assertIn(B.output_power, reading.issue)

    async def test_new_guard_cannot_reuse_previous_runtime_activity(self):
        await self.establish()
        await self.guard.async_close()
        replacement = self.make_guard()
        replacement.async_start()
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading(replacement))
        await self.report(B.input_power, "132", 2)
        self.assertEqual(self.reading(replacement).battery_evidence, "same_device_activity")

    async def test_input_unavailable_discards_input_witness(self):
        await self.establish()
        reading = await self.report(B.input_power, "unavailable", 2)
        self.assert_unverified(reading)
        # Reconnection starts a baseline; its first value is not new activity.
        self.assert_unverified(await self.report(B.input_power, "132", 3))
        self.assertEqual((await self.report(B.input_power, "133", 4)).battery_evidence,
                         "same_device_activity")

    async def test_input_unavailable_preserves_independent_output_witness(self):
        await self.establish(B.output_power)
        reading = await self.report(B.input_power, "unavailable", 2)
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_activity_entity, B.output_power)

    async def test_rapid_output_disconnect_reconnect_requires_a_new_baseline(self):
        await self.establish()
        self.wall = self.origin + 3
        self.write(B.output_power, "unavailable", 2)
        self.write(B.output_power, "151", 2.1)
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading())
        reading = await self.report(B.output_power, "152", 4)
        self.assertEqual(reading.battery_evidence, "same_device_activity")

    async def test_output_removal_and_readdition_cannot_retain_activity(self):
        await self.establish()
        self.wall = self.origin + 2
        self.hass.states.async_remove(B.output_power)
        self.write(B.output_power, "151", 2, {"unit_of_measurement": "W"})
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading())
        self.assertEqual((await self.report(B.output_power, "152", 3)).battery_evidence,
                         "same_device_activity")

    async def test_battery_unavailable_and_readdition_clear_existing_activity(self):
        await self.establish()
        self.wall = self.origin + 2
        self.write(B.battery, "unavailable", 2)
        self.write(B.battery, "72", -3500, {"unit_of_measurement": "%"})
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading())
        self.assertEqual((await self.report(B.input_power, "132", 3)).battery_evidence,
                         "same_device_activity")
        self.hass.states.async_remove(B.battery)
        self.write(B.battery, "72", -3400, {"unit_of_measurement": "%"})
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading())

    async def test_invalid_output_and_battery_reports_invalidate_witness(self):
        invalid = (
            (B.output_power, "NaN", {"unit_of_measurement": "W"}),
            (B.output_power, "151", {"unit_of_measurement": "V"}),
            (B.output_power, "151", {"unit_of_measurement": "W", "restored": True}),
            (B.output_power, "151", {"unit_of_measurement": "W", "assumed_state": True}),
            (B.battery, "unknown", {"unit_of_measurement": "%"}),
            (B.battery, "101", {"unit_of_measurement": "%"}),
            (B.battery, "72", {"unit_of_measurement": "%", "restored": True}),
            (B.battery, "72", {"unit_of_measurement": "%", "assumed_state": True}),
        )
        for index, (entity, value, attrs) in enumerate(invalid):
            with self.subTest(entity=entity, value=value, attrs=attrs):
                base = 10 + index * 10
                # Establish fresh numeric baselines and then new activity.
                await self.report(B.output_power, str(160 + index), base)
                self.write(B.battery, "72", -3500 + index,
                           {"unit_of_measurement": "%"})
                await self.hass.async_block_till_done()
                await self.report(B.output_power, str(170 + index), base + 1)
                self.assertEqual(self.reading().battery_evidence, "same_device_activity")
                self.assert_unverified(await self.report(entity, value, base + 2, attrs))
                # Restore the original old SOC and a numeric output in a burst.
                self.wall = self.origin + base + 3
                self.write(B.battery, "72", -3400 + index,
                           {"unit_of_measurement": "%"})
                self.write(B.output_power, str(180 + index), base + 3,
                           {"unit_of_measurement": "W"})
                await self.hass.async_block_till_done()
                self.assert_unverified(self.reading())

    async def test_source_config_entry_unload_resets_activity(self):
        await self.establish()
        self.owner._async_set_state(self.hass, ConfigEntryState.SETUP_RETRY, None)
        self.assert_unverified(self.reading())
        self.owner._async_set_state(self.hass, ConfigEntryState.LOADED, None)
        self.assert_unverified(await self.report(B.input_power, "132", 2))
        self.assertEqual((await self.report(B.input_power, "133", 3)).battery_evidence,
                         "same_device_activity")

    async def test_fresh_cached_reports_do_not_outlive_known_source_unload(self):
        await self.report(B.battery, "72", 1)
        for entity in (B.battery, B.output_power):
            with self.subTest(entity=entity):
                source = self.entry("bluetti", "source-" + entity.split(".", 1)[1])
                self.registry.async_update_entity(entity, config_entry_id=source.entry_id)
                self.assertIsNone(self.reading().issue)
                source._async_set_state(self.hass, ConfigEntryState.UNLOAD_IN_PROGRESS, None)
                reading = self.reading()
                self.assert_unverified(reading)
                self.assertIsNone(reading.battery_percent)
                self.assertIn(entity, reading.issue)
                self.assertIn("not loaded", reading.issue)
                self.assertEqual(reading.battery_reported_at, self.origin + 1)
                with self.assertRaises(HAActivationDeferred):
                    self.guard.check_activation(B.heater, {"hvac_mode": "heat"})
                self.registry.async_update_entity(entity, config_entry_id=self.owner.entry_id)

    async def test_registry_reference_to_missing_source_blocks_fresh_reports(self):
        await self.report(B.battery, "72", 1)
        for entity in (B.battery, B.output_power):
            with self.subTest(entity=entity):
                source = self.entry("bluetti", "missing-" + entity.split(".", 1)[1])
                self.registry.async_update_entity(entity, config_entry_id=source.entry_id)
                self.assertIsNone(self.reading().issue)
                del self.hass.config_entries._entries[source.entry_id]
                reading = self.reading()
                self.assert_unverified(reading)
                self.assertIn(entity, reading.issue)
                self.assertIn("not loaded", reading.issue)
                self.assertIsNone(reading.battery_percent)
                self.registry.async_update_entity(entity, config_entry_id=self.owner.entry_id)

    async def test_rapid_source_unload_reload_clears_activity_between_observations(self):
        await self.establish()
        self.assertFalse(self.guard.blocked)
        self.wall = self.origin + 2
        # Both lifecycle changes finish before the next scheduled state check.
        self.owner._async_set_state(self.hass, ConfigEntryState.UNLOAD_IN_PROGRESS, None)
        self.owner._async_set_state(self.hass, ConfigEntryState.LOADED, None)
        await self.hass.async_block_till_done()
        self.assert_unverified(self.reading())
        self.assertTrue(self.guard.blocked)
        self.assert_unverified(await self.report(B.input_power, "131", 3, force=True))
        reading = await self.report(B.input_power, "132", 4)
        self.assertEqual(reading.battery_evidence, "same_device_activity")
        self.assertEqual(reading.battery_activity_at, self.origin + 4)

    async def test_other_device_cannot_witness_same_named_battery(self):
        other = self.make_device(self.owner, "other-battery")
        self.registry.async_update_entity(B.input_power, device_id=other.id)
        self.assert_unverified(await self.establish())
        self.registry.async_update_entity(B.input_power, device_id=self.device.id)
        self.assert_unverified(await self.report(B.input_power, "132", 2))
        self.assertEqual((await self.report(B.input_power, "133", 3)).battery_evidence,
                         "same_device_activity")

    async def test_output_from_other_device_prevents_input_witness(self):
        other = self.make_device(self.owner, "other-battery")
        self.registry.async_update_entity(B.output_power, device_id=other.id)
        self.assert_unverified(await self.establish())

    async def test_missing_device_or_config_entry_cannot_grant_liveness(self):
        for field in ("device_id", "config_entry_id"):
            with self.subTest(field=field):
                original = getattr(self.registry.async_get(B.input_power), field)
                self.registry.async_update_entity(B.input_power, **{field: None})
                await self.report(B.input_power, "131", 1)
                self.assert_unverified(await self.report(B.input_power, "132", 2))
                self.registry.async_update_entity(B.input_power, **{field: original})

    async def test_same_device_with_different_entry_or_platform_is_not_trusted(self):
        other = self.entry("bluetti", "other-owner")
        self.registry.async_update_entity(B.input_power, config_entry_id=other.entry_id)
        self.assert_unverified(await self.establish())
        self.registry.async_update_entity_platform(
            B.input_power, "template", new_config_entry_id=self.owner.entry_id)
        self.assert_unverified(await self.report(B.input_power, "132", 2))

    async def test_evaluator_rejects_malformed_expired_future_or_unbound_witness(self):
        states = self.guard._states()
        invalid = (
            None, {}, {"entity_id": B.input_power},
            {"entity_id": B.battery, "reported_at": self.wall},
            {"entity_id": "sensor.unrelated", "reported_at": self.wall},
            {"entity_id": B.input_power, "reported_at": self.wall - 180.01},
            {"entity_id": B.input_power, "reported_at": self.wall + .01},
            {"entity_id": B.input_power, "reported_at": self.wall + 6},
            {"entity_id": B.input_power, "reported_at": True},
            {"entity_id": B.input_power, "reported_at": math.nan},
            {"entity_id": B.input_power, "reported_at": math.inf},
            {"entity_id": B.input_power, "reported_at": "now"},
        )
        for witness in invalid:
            with self.subTest(witness=witness):
                self.assert_unverified(power.evaluate_power(states, B, self.wall,
                                                            battery_activity=witness))

    async def test_witness_never_substitutes_for_valid_soc_and_original_timestamp(self):
        states = self.guard._states()
        witness = {"entity_id": B.input_power, "reported_at": self.wall}
        for value, attrs, reported in (
            ("NaN", {"unit_of_measurement": "%"}, self.origin - 3600),
            ("-1", {"unit_of_measurement": "%"}, self.origin - 3600),
            ("101", {"unit_of_measurement": "%"}, self.origin - 3600),
            ("72", {"unit_of_measurement": "W"}, self.origin - 3600),
            ("72", {"unit_of_measurement": "%", "restored": True}, self.origin - 3600),
            ("72", {"unit_of_measurement": "%", "assumed_state": True}, self.origin - 3600),
            ("72", {"unit_of_measurement": "%"}, None),
            ("72", {"unit_of_measurement": "%"}, self.origin + 6),
        ):
            with self.subTest(value=value, attrs=attrs, reported=reported):
                states[B.battery] = {
                    "state": value, "attributes": attrs,
                    "last_reported": (datetime.fromtimestamp(reported, timezone.utc).isoformat()
                                      if reported is not None else None),
                }
                self.assert_unverified(power.evaluate_power(states, B, self.wall,
                                                            battery_activity=witness))

    async def test_direct_evaluator_keeps_old_soc_timestamp_when_witnessed(self):
        states = self.guard._states()
        reading = power.evaluate_power(states, B, self.wall, battery_activity={
            "entity_id": B.input_power, "reported_at": self.wall})
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_reported_at, self.origin - 3600)
        self.assertEqual(reading.battery_evidence, "same_device_activity")
        self.assertEqual(states[B.battery].last_reported.timestamp(), self.origin - 3600)

    async def test_state_object_last_reported_is_the_soc_freshness_clock(self):
        states = self.guard._states()
        states[B.battery] = State(
            B.battery, "72", {"unit_of_measurement": "%"},
            last_reported=datetime.fromtimestamp(self.wall, timezone.utc),
            last_updated=datetime.fromtimestamp(self.origin - 3600, timezone.utc),
        )
        reading = power.evaluate_power(states, B, self.wall)
        self.assertIsNone(reading.issue)
        self.assertEqual(reading.battery_evidence, "ha_report")


if __name__ == "__main__":
    unittest.main()
