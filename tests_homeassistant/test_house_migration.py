"""House-policy adoption and the native electrical interlock's dispatch boundary."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from custom_components.house_climate import PLATFORMS, async_migrate_entry, async_unload_entry
from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.bindings import example_bindings
from custom_components.house_climate.controller.ha import HAActivationDeferred
from custom_components.house_climate.house_policy import house_configuration
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class NativeFixture:
    """Reuse setup helpers without inheriting their unrelated test methods."""

    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle


class HouseEntryMigrationTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self._actual_entries = self.hass.config_entries
        self.bindings = example_bindings()
        self.bindings_file = Path(self.directory.name, "house_climate_bindings.json")
        self.bindings_file.write_text(json.dumps(self.bindings.to_dict()))

    async def asyncTearDown(self):
        self.hass.config_entries = self._actual_entries
        await fixtures.NativeTests.asyncTearDown(self)

    def entry(self, version, *, file_based=False):
        if file_based:
            data = {"config_file": "house_climate.toml", "live": True}
            options = {"retained_option": "keep"}
        else:
            previous = house_configuration(bindings=self.bindings).to_dict()
            previous["rooms"]["sleeping_suite"] = previous["rooms"].pop("primary")
            previous["rooms"]["sleeping_suite"]["area_id"] = "upstairs_bedroom"
            previous["poll_seconds"] = 5
            data = {"configuration": previous, "live": False, "retained_data": "keep"}
            # Preserve the effective live opt-in, including options precedence.
            options = {"live": True, "retained_option": "keep"}
        entry = SimpleNamespace(entry_id="existing-controller", version=version,
                                data=data, options=options)
        updates = Mock()

        def update(actual, **changes):
            updates(actual, **changes)
            for key, value in changes.items():
                setattr(actual, key, value)

        self.hass.config_entries = Mock()
        self.hass.config_entries.async_update_entry.side_effect = update
        return entry, updates

    async def test_v2_keeps_entity_identity_and_opt_in_and_migrates_holds_before_entry(self):
        entry, updates = self.entry(2)
        old_data, old_options = deepcopy(entry.data), deepcopy(entry.options)
        observed = []

        async def migrate_holds(hass, entry_id, previous, fixed):
            observed.append((previous, fixed))
            self.assertIs(hass, self.hass)
            self.assertEqual(entry_id, "existing-controller")
            self.assertEqual(entry.version, 2)
            self.assertEqual(entry.data, old_data)
            self.assertEqual(entry.options, old_options)
            updates.assert_not_called()
            self.assertIn("sleeping_suite", previous.zones)
            self.assertIn("sleeping_suite", fixed.zones)
            self.assertEqual(fixed.zones["sleeping_suite"].area_id, "upstairs_bedroom")
            self.assertEqual(previous.poll_seconds, 5)
            self.assertEqual(fixed.poll_seconds, 15)

        with patch("custom_components.house_climate.comfort.async_migrate_house_holds",
                   side_effect=migrate_holds):
            self.assertTrue(await async_migrate_entry(self.hass, entry))

        self.assertEqual(len(observed), 1)
        self.assertEqual(entry.entry_id, "existing-controller")
        self.assertEqual(entry.version, 3)
        self.assertEqual(entry.data["policy"], "house")
        self.assertEqual(entry.options["policy"], "house")
        self.assertEqual(entry.data["configuration"], old_data["configuration"])
        self.assertEqual(entry.data["retained_data"], "keep")
        self.assertEqual(entry.options["retained_option"], "keep")
        self.assertFalse(entry.data["live"])
        self.assertTrue(entry.options["live"])
        updates.assert_called_once()

    async def test_v1_file_entry_adopts_house_without_losing_original_configuration_identity(self):
        entry, updates = self.entry(1, file_based=True)
        old_data, old_options = deepcopy(entry.data), deepcopy(entry.options)
        migrate = AsyncMock()
        with patch("custom_components.house_climate.comfort.async_migrate_house_holds", migrate):
            self.assertTrue(await async_migrate_entry(self.hass, entry))

        migrate.assert_awaited_once()
        previous, fixed = migrate.await_args.args[2:]
        self.assertEqual(entry.data["configuration"], previous.to_dict())
        self.assertEqual(entry.entry_id, "existing-controller")
        self.assertEqual(entry.data["config_file"], old_data["config_file"])
        self.assertTrue(entry.data["live"])
        self.assertEqual(entry.options["retained_option"], old_options["retained_option"])
        self.assertFalse(fixed.legacy)
        self.assertEqual(entry.version, 3)
        updates.assert_called_once()

    async def test_latest_options_room_identity_and_hold_survive_a_minimal_options_save(self):
        from custom_components.house_climate.climate import RoomClimate
        from custom_components.house_climate.comfort import RoomComfort
        from custom_components.house_climate.config_flow import HouseClimateOptionsFlow
        from custom_components.house_climate.configuration import entry_configuration
        from tests_homeassistant.test_config_navigation import EditorManager

        entry, _ = self.entry(2)
        stale = house_configuration(bindings=self.bindings).to_dict()
        stale["rooms"]["primary"]["area_id"] = "obsolete_bedroom_area"
        current = deepcopy(stale)
        room_id = "room_01hzz99"
        current["rooms"][room_id] = current["rooms"].pop("primary")
        current["rooms"][room_id]["area_id"] = "current_bedroom_area"
        current["rooms"][room_id]["sensors"].append({
            "entity_id": "sensor.sleep_equipment_old", "source": "old_equipment"})
        entry.data["configuration"] = stale
        entry.options["configuration"] = current
        previous = Config.from_dict(current)
        now = datetime(2026, 10, 26, 5, 15, tzinfo=timezone.utc)
        original = RoomComfort(self.hass, previous, entry.entry_id, lambda: None)
        restored = None
        manager = None
        with (patch.object(RoomComfort, "_now", new=staticmethod(lambda: now)),
              patch("custom_components.house_climate.comfort.async_track_point_in_utc_time",
                    return_value=Mock())):
            try:
                await original.async_load()
                await original.async_set_target(room_id, 68.5)
                original_expiry = original.effective(room_id, now)["expires_at"]
                self.assertTrue(await async_migrate_entry(self.hass, entry))
                self.assertEqual(entry.data["configuration"], previous.to_dict())

                # Exercise the actual one-field flow, whose result replaces all
                # legacy options rather than retaining their configuration blob.
                with patch.object(HouseClimateOptionsFlow, "config_entry",
                                  new_callable=PropertyMock, return_value=entry):
                    manager = EditorManager(self.hass, HouseClimateOptionsFlow())
                    form = await manager.async_init(DOMAIN, context={"source": "user"})
                    result = await manager.async_configure(form["flow_id"], {"live": True})
                self.assertEqual(result["data"], {"policy": "house", "live": True})
                entry.options = result["data"]
                reloaded = entry_configuration(self.directory.name, {**entry.data, **entry.options})
                self.assertIn(room_id, reloaded.zones)
                self.assertNotIn("primary", reloaded.zones)
                self.assertEqual(reloaded.zones[room_id].area_id, "current_bedroom_area")
                thermostat = RoomClimate(SimpleNamespace(
                    entry_id=entry.entry_id, runtime_data=SimpleNamespace(config=reloaded)), room_id)
                self.assertEqual(thermostat.unique_id, f"{entry.entry_id}_climate_{room_id}")

                restored = RoomComfort(self.hass, reloaded, entry.entry_id, lambda: None)
                await restored.async_load()
                hold = restored.effective(room_id, now)
                self.assertTrue(hold["hold"])
                self.assertEqual(hold["target_f"], 68.5)
                self.assertEqual(hold["expires_at"], original_expiry)
                saved = await restored.store.async_load()
                self.assertEqual(set(saved["rooms"]), {room_id})
                self.assertEqual(saved["rooms"][room_id]["fingerprint"],
                                 restored._fingerprint(room_id))
                self.assertEqual(self.calls, [])
            finally:
                if manager is not None:
                    for pending in manager.async_progress():
                        manager.async_abort(pending["flow_id"])
                if restored is not None:
                    await restored.async_close()
                await original.async_close()

    async def test_failed_hold_write_keeps_both_old_entry_versions_retryable(self):
        for version in (1, 2):
            with self.subTest(version=version):
                entry, updates = self.entry(version, file_based=version == 1)
                before = deepcopy(vars(entry))
                with patch("custom_components.house_climate.comfort.async_migrate_house_holds",
                           new=AsyncMock(side_effect=OSError("comfort storage unavailable"))):
                    with self.assertRaisesRegex(OSError, "comfort storage unavailable"):
                        await async_migrate_entry(self.hass, entry)
                self.assertEqual(vars(entry), before)
                updates.assert_not_called()

    async def test_current_version_is_idempotent_and_future_version_is_rejected(self):
        for version, accepted in ((3, True), (4, False)):
            with self.subTest(version=version):
                entry, updates = self.entry(version)
                before = deepcopy(vars(entry))
                with (patch("custom_components.house_climate.entry_configuration") as load,
                      patch("custom_components.house_climate.comfort.async_migrate_house_holds",
                            new=AsyncMock()) as migrate):
                    self.assertIs(await async_migrate_entry(self.hass, entry), accepted)
                self.assertEqual(vars(entry), before)
                load.assert_not_called()
                migrate.assert_not_awaited()
                updates.assert_not_called()

    async def test_missing_private_bindings_cannot_partially_migrate_or_guess_devices(self):
        self.bindings_file.unlink()
        for version in (1, 2):
            with self.subTest(version=version):
                entry, updates = self.entry(version, file_based=version == 1)
                before = deepcopy(vars(entry))
                with patch("custom_components.house_climate.comfort.async_migrate_house_holds",
                           new=AsyncMock()) as migrate:
                    with self.assertRaises((OSError, ValueError)):
                        await async_migrate_entry(self.hass, entry)
                self.assertEqual(vars(entry), before)
                updates.assert_not_called()
                migrate.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_invalid_private_bindings_cannot_replace_an_existing_controller(self):
        # Valid JSON with no real device mapping must not become guessed defaults.
        self.bindings_file.write_text(json.dumps({"heads": {}, "rooms": {}, "fans": {},
                                                "heater": None, "power": {}}))
        entry, updates = self.entry(2)
        before = deepcopy(vars(entry))
        with patch("custom_components.house_climate.comfort.async_migrate_house_holds",
                   new=AsyncMock()) as migrate:
            with self.assertRaises((ValueError, TypeError, KeyError)):
                await async_migrate_entry(self.hass, entry)
        self.assertEqual(vars(entry), before)
        updates.assert_not_called()
        migrate.assert_not_awaited()
        self.assertEqual(self.calls, [])


class DispatchGuard:
    def __init__(self, heater):
        self.heater = heater
        self.blocked = False
        self.power_cut_confirmed = False
        self.checked = []

    def check_activation(self, entity, data=None):
        self.checked.append(entity)
        if entity == self.heater and self.blocked:
            raise HAActivationDeferred("Electrical guard blocks auxiliary heating")


class HousePowerBridgeTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def guarded_runtime(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        guard = DispatchGuard(runtime.config.heater_entity)
        runtime.api.power_guard = guard
        runtime.controls.power_guard = guard
        return runtime, guard

    async def test_heater_is_rechecked_when_a_previously_queued_command_dispatches(self):
        runtime, guard = await self.guarded_runtime()
        self.assertFalse(guard.blocked)
        submit = asyncio.run_coroutine_threadsafe
        queued = []

        def become_overloaded(coroutine, loop):
            # The worker already built its command from an unblocked plan.
            # The actual HA-loop call must consult the new guard state.
            queued.append(coroutine)
            guard.blocked = True
            return submit(coroutine, loop)

        with patch("custom_components.house_climate.runtime.asyncio.run_coroutine_threadsafe",
                   side_effect=become_overloaded):
            with self.assertRaisesRegex(HAActivationDeferred, "Electrical guard"):
                await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                        entity_id=runtime.config.heater_entity,
                                        hvac_mode="heat", temperature=86)
        self.assertEqual(len(queued), 1)
        self.assertIn(runtime.config.heater_entity, guard.checked)
        self.assertFalse(any(service == "set_temperature" for _, service, _ in self.calls))
        self.assertEqual(self.hass.states.get(runtime.config.heater_entity).state, "off")
        self.assertEqual(runtime.controls.inflight, 0)
        self.assertEqual(runtime.controls.auxiliary_inflight, 0)

    async def test_electrical_trip_does_not_block_heater_off_or_unrelated_head(self):
        runtime, guard = await self.guarded_runtime()
        guard.blocked = True
        heater = runtime.config.heater_entity
        old = self.hass.states.get(heater)
        self.hass.states.async_set(heater, "heat", dict(old.attributes))
        await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                entity_id=heater, hvac_mode="off")
        self.assertEqual(self.hass.states.get(heater).state, "off")
        other = runtime.config.heads["guest"]
        await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                entity_id=other, hvac_mode="cool", temperature=72)
        self.assertEqual(self.hass.states.get(other).state, "cool")
        self.assertTrue(runtime.controls.enabled)
        self.assertIn(other, guard.checked)

    async def test_worker_overlay_inhibits_only_auxiliary_when_power_guard_is_blocked(self):
        runtime, guard = await self.guarded_runtime()
        interlock = runtime.config.entity("auxiliary_probe_safe", "binary_sensor")
        before = await asyncio.to_thread(runtime.api.states)
        self.assertEqual(before[interlock]["state"], "on")
        guard.blocked = True
        blocked = await asyncio.to_thread(runtime.api.states)
        self.assertEqual(blocked[interlock]["state"], "off")
        self.assertEqual(blocked[runtime.config.entity("enabled")]["state"], "on")
        for entity in runtime.config.heads.values():
            self.assertEqual(blocked[entity], before[entity])
        guard.blocked = False
        recovered = await asyncio.to_thread(runtime.api.states)
        self.assertEqual(recovered[interlock]["state"], "on")

    def isolated_cleanup(self, *, heater=None):
        cfg = Config.from_dict(self.cfg.to_dict())
        if heater is not None:
            cfg = replace(cfg, heater_entity=heater)
        controls = NativeControls(self.hass, cfg, True, "isolated-cut", lambda: None)
        guard = DispatchGuard(cfg.heater_entity)
        guard.blocked = True
        controls.power_guard = guard
        controls.owned = (cfg.heater_entity, cfg.heads["guest"])
        controls.recovering = True
        controls._auxiliary_heat_possible = True
        controls.auxiliary_heartbeat_blocked = True
        controls.store.async_save = AsyncMock()
        self.hass.states.async_set(cfg.heater_entity, "unavailable")
        return controls, guard

    async def test_confirmed_heater_only_power_cut_allows_cleanup_without_dyson_reply(self):
        from custom_components.house_climate.power_guard import HeaterPowerGuard

        bindings = example_bindings().power
        controls, _ = self.isolated_cleanup(heater=bindings.heater)
        guard = HeaterPowerGuard(self.hass, bindings=bindings, live=True, entry_id="real-switch-proof",
                                 notify=lambda: None)
        controls.power_guard = guard
        guard._cut_owned = True
        guard._cut_request_wall = time.time()
        # An accepted cut request with no observed state change is insufficient.
        self.hass.states.async_set(bindings.heater_switch, "on")
        self.assertFalse(guard.power_cut_confirmed)
        await controls._clear_if_off()
        self.assertTrue(controls.owned)
        controls.store.async_save.assert_not_awaited()

        self.hass.states.async_set(bindings.heater_switch, "off")
        self.assertTrue(guard.power_cut_confirmed)
        self.assertTrue(controls._is_off(controls.config.heater_entity))
        await controls._clear_if_off()
        self.assertEqual(controls.owned, ())
        self.assertFalse(controls.recovering)
        controls.store.async_save.assert_awaited_once()
        self.assertEqual(self.calls, [])
        # A later power-On revokes isolation; an old successful cut is not proof.
        self.hass.states.async_set(bindings.heater_switch, "on")
        self.assertFalse(guard.power_cut_confirmed)
        self.assertFalse(controls._is_off(controls.config.heater_entity))

    async def test_unacknowledged_cut_cannot_release_and_proof_never_covers_other_heads(self):
        controls, guard = self.isolated_cleanup()
        self.assertFalse(controls._is_off(controls.config.heater_entity))
        await controls._clear_if_off()
        self.assertTrue(controls.owned)
        controls.store.async_save.assert_not_awaited()

        guard.power_cut_confirmed = True
        head = controls.config.heads["guest"]
        state = self.hass.states.get(head)
        self.hass.states.async_set(head, "cool", dict(state.attributes))
        self.assertFalse(controls._is_off(head))
        await controls._clear_if_off()
        self.assertTrue(controls.owned)
        controls.store.async_save.assert_not_awaited()

        self.hass.states.async_set(head, "off", dict(state.attributes))
        await controls._clear_if_off()
        self.assertEqual(controls.owned, ())
        controls.store.async_save.assert_awaited_once()

    async def test_power_isolation_does_not_release_ownership_with_activation_in_flight(self):
        controls, guard = self.isolated_cleanup()
        guard.power_cut_confirmed = True
        controls.inflight = 1
        await controls._clear_if_off()
        self.assertTrue(controls.owned)
        controls.inflight = 0
        controls.auxiliary_inflight = 1
        await controls._clear_if_off()
        self.assertTrue(controls.owned)
        controls.store.async_save.assert_not_awaited()
        controls.auxiliary_inflight = 0
        await controls._clear_if_off()
        self.assertEqual(controls.owned, ())

    async def test_unload_keeps_guard_observer_until_late_power_on_is_compensated(self):
        from homeassistant.config_entries import ConfigEntries
        from custom_components.house_climate.power_guard import HeaterPowerGuard

        bindings = example_bindings().power
        heater_attrs = dict(self.hass.states.get(self.cfg.heater_entity).attributes)
        self.hass.states.async_set(bindings.heater, "off", heater_attrs)
        cfg = replace(Config.from_dict(self.cfg.to_dict()), heater_entity=bindings.heater)
        runtime, _ = await self.runtime(cfg=cfg, entry_id="late-outlet-unload")
        guard = HeaterPowerGuard(self.hass, bindings=bindings, live=True, entry_id="late-outlet-unload",
                                 notify=lambda: None)
        runtime.power_guard = runtime.api.power_guard = guard
        runtime.controls.power_guard = guard
        guard._cut_owned = True
        guard._cut_request_wall = time.time() - 1
        self.hass.states.async_set(bindings.heater_switch, "off")
        self.assertTrue(guard.power_cut_confirmed)
        guard_observer_removed = Mock()
        guard._unsubscribers.append(guard_observer_removed)
        climate_observer_removed = Mock()
        runtime._remove_climate_listener = climate_observer_removed

        restore_started, release_restore, off_reported = (asyncio.Event() for _ in range(3))
        off_calls = []

        async def delayed_on(call):
            restore_started.set()
            try:
                await release_restore.wait()
            except asyncio.CancelledError:
                # Integrations may still complete after cancellation was requested.
                await release_restore.wait()
            self.hass.states.async_set(bindings.heater_switch, "on")

        async def confirmed_off(call):
            off_calls.append(dict(call.data))
            self.hass.states.async_set(bindings.heater_switch, "off")
            off_reported.set()

        self.hass.services.async_register("switch", "turn_on", delayed_on)
        self.hass.services.async_register("switch", "turn_off", confirmed_off)
        self.hass.services.async_register(DOMAIN, "generate_package", AsyncMock())
        self.hass.data[DOMAIN] = runtime
        entry = SimpleNamespace(entry_id="late-outlet-unload", runtime_data=runtime)
        old_manager = self.hass.config_entries
        self.hass.config_entries = ConfigEntries(self.hass, {})
        service_timeout = patch("custom_components.house_climate.power_guard.SERVICE_TIMEOUT_SECONDS", .05)
        service_timeout.start()
        pending_on = guard._service("switch", "turn_on", bindings.heater_switch)
        try:
            await asyncio.wait_for(restore_started.wait(), 1)
            self.assertEqual(self.hass.states.get(bindings.heater_switch).state, "off")
            self.assertFalse(guard.power_cut_confirmed)
            self.assertTrue(all(self.hass.states.get(entity).state == "off"
                                for entity in runtime.config.climate_entities))

            with self.assertLogs("custom_components.house_climate.runtime", level="WARNING"):
                self.assertFalse(await runtime.async_stop())
            await asyncio.wait_for(off_reported.wait(), 1)
            self.assertFalse(guard._closed)
            self.assertIn(pending_on, guard._positive_tasks)
            guard_observer_removed.assert_not_called()
            climate_observer_removed.assert_not_called()

            with patch.object(self.hass.config_entries, "async_unload_platforms",
                              new=AsyncMock(return_value=True)) as unload_platforms:
                with self.assertLogs("custom_components.house_climate.runtime", level="WARNING"):
                    self.assertFalse(await async_unload_entry(self.hass, entry))
                unload_platforms.assert_not_awaited()
                self.assertIs(self.hass.data[DOMAIN], runtime)
                guard_observer_removed.assert_not_called()
                climate_observer_removed.assert_not_called()

                # The older Off cannot supersede this later actual power-On.
                first_off_count = len(off_calls)
                off_reported.clear()
                release_restore.set()
                await asyncio.wait_for(asyncio.shield(pending_on), 1)
                await asyncio.wait_for(off_reported.wait(), 1)
                await asyncio.wait_for(asyncio.shield(guard._compensating_task), 1)
                self.assertGreater(len(off_calls), first_off_count)
                self.assertEqual(self.hass.states.get(bindings.heater_switch).state, "off")
                self.assertTrue(guard.power_cut_confirmed)

                self.assertTrue(await async_unload_entry(self.hass, entry))
                unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
                self.assertNotIn(DOMAIN, self.hass.data)
                self.assertTrue(guard._closed)
                guard_observer_removed.assert_called_once_with()
                climate_observer_removed.assert_called_once_with()
                self.assertIsNone(runtime._remove_climate_listener)
        finally:
            release_restore.set()
            try:
                await asyncio.wait_for(asyncio.shield(pending_on), 1)
                await asyncio.sleep(0)
                if guard._compensating_task is not None:
                    await asyncio.wait_for(asyncio.shield(guard._compensating_task), 1)
                await guard.async_close()
            finally:
                self.hass.data.pop(DOMAIN, None)
                self.hass.config_entries = old_manager
                service_timeout.stop()

