"""Ordinary purifier commands must not target the auxiliary's fan alias."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
import unittest
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.house_climate import configuration, handoff
from custom_components.house_climate.controller.config import Config, FallbackPolicy
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.controls import NativeControls
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class PurifierAliasGuardTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.NativeTests.asyncSetUp
    asyncTearDownBase = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    async def asyncSetUp(self):
        await self.asyncSetUpBase()
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.owner = ConfigEntry(
            version=1, minor_version=1, domain="fixture_bridge", entry_id="purifier_owner",
            title="Synthetic appliance bridge", data={}, options={}, source="user",
            unique_id=None, discovery_keys=MappingProxyType({}),
            state=ConfigEntryState.LOADED, subentries_data=None,
        )
        self.hass.config_entries._entries[self.owner.entry_id] = self.owner
        devices = dr.async_get(self.hass)
        self.heater_device = devices.async_get_or_create(
            config_entry_id=self.owner.entry_id,
            identifiers={("fixture_bridge", "synthetic_heater")},
        )
        self.purifier_device = devices.async_get_or_create(
            config_entry_id=self.owner.entry_id,
            identifiers={("fixture_bridge", "synthetic_purifier")},
        )
        self.heater = self.cfg.heater_entity
        self.fan = next(iter(self.cfg.fans.values()))
        self.register(self.heater, self.heater_device.id)
        self.register(self.fan, self.purifier_device.id)

    async def asyncTearDown(self):
        # Restore independent identities before the fixture reconciles ownership.
        registry = er.async_get(self.hass)
        for entity, device in ((self.heater, self.heater_device), (self.fan, self.purifier_device)):
            if registry.async_get(entity) is not None:
                registry.async_update_entity(entity, device_id=device.id)
        for runtime in self.runtimes:
            runtime.controls.handoff = None
        await self.asyncTearDownBase()

    def register(self, entity, device_id):
        state = self.hass.states.get(entity)
        self.hass.states.async_remove(entity)
        try:
            domain, object_id = entity.split(".", 1)
            item = er.async_get(self.hass).async_get_or_create(
                domain, self.owner.domain, f"synthetic_{object_id}",
                suggested_object_id=object_id, config_entry=self.owner, device_id=device_id,
            )
            self.assertEqual(item.entity_id, entity)
            return item
        finally:
            if state is not None:
                self.hass.states.async_set(entity, state.state, dict(state.attributes))

    def config(self, *, minimum=20, fallback=False):
        config = Config.from_dict(self.cfg.to_dict())
        controls = {
            key: replace(control, minimum_percentage=minimum)
            if control.entity_id == self.fan else control
            for key, control in config.fan_controls.items()
        }
        return replace(config, fan_controls=controls,
                       fallback=FallbackPolicy() if fallback else None)

    def issue(self, config=None, *, activation=True):
        return configuration.purifier_identity_issue(
            self.hass, config or self.config(), self.fan, activation=activation,
        )

    def alias(self):
        er.async_get(self.hass).async_update_entity(self.fan, device_id=self.heater_device.id)

    def fan_calls(self):
        return [call for call in self.calls if call[0] == "fan" and call[2]["entity_id"] == self.fan]

    async def ready(self, *, minimum=20, fallback=False):
        runtime, _ = await self.runtime(cfg=self.config(minimum=minimum, fallback=fallback))
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        self.calls.clear()
        return runtime

    async def restored_fan_handoff(self, minimums, *, legacy=False, remove_current_floors=False):
        config = self.config(minimum=0, fallback=True)
        config = replace(config, off_settle_seconds=0, head_minimum_off_seconds=0,
            fan_controls={key: replace(control, minimum_percentage=minimums.get(control.entity_id, 0))
                          for key, control in config.fan_controls.items()})
        original = NativeControls(self.hass, config, True, "purifier_saved", lambda: None)
        original.owned = tuple(minimums)
        original.handoff = handoff.arm(original, original.owned)
        if legacy:
            original.handoff.pop("fan_minimum_percentages", None)
        await original._save(original.owned, ())
        stored = await original.store.async_load()
        if remove_current_floors:
            config = replace(config, fan_controls={
                key: replace(control, minimum_percentage=0)
                for key, control in config.fan_controls.items()})
        restored = NativeControls(self.hass, config, True, "purifier_saved", lambda: None)
        await restored.async_load()
        self.assertFalse(restored.enabled)
        self.assertTrue(restored.recovering)
        return restored, stored

    async def test_setup_rejects_known_auxiliary_alias_even_without_a_floor(self):
        self.alias()
        for minimum in (0, 20):
            with self.subTest(minimum=minimum):
                with self.assertRaises(ValueError):
                    configuration.validate_actuators(self.hass, self.config(minimum=minimum))
                self.assertIsNotNone(self.issue(self.config(minimum=minimum), activation=False))
        self.assertEqual(self.calls, [])

    async def test_distinct_registry_devices_allow_explicit_floor_without_name_inference(self):
        # A misleading display name cannot turn a different registry device into an alias.
        er.async_get(self.hass).async_update_entity(self.fan, name="Synthetic auxiliary heater")
        configuration.validate_actuators(self.hass, self.config())
        self.assertIsNone(self.issue())
        runtime = await self.ready()
        await asyncio.to_thread(runtime.api.service, "fan", "turn_on", entity_id=self.fan, percentage=20)
        self.assertEqual(self.fan_calls()[-1][1:], ("turn_on", {"entity_id": self.fan, "percentage": 20}))

    async def test_missing_identity_inhibits_new_floor_but_keeps_legacy_contract(self):
        registry = er.async_get(self.hass)
        for entity, device in ((self.fan, self.purifier_device), (self.heater, self.heater_device)):
            with self.subTest(entity=entity):
                registry.async_update_entity(entity, device_id=None)
                self.assertIsNotNone(self.issue())
                self.assertIsNone(self.issue(self.config(minimum=0)))
                self.assertIsNone(self.issue(activation=False))
                configuration.validate_actuators(self.hass, self.config())
                registry.async_update_entity(entity, device_id=device.id)

    async def test_no_configured_auxiliary_needs_no_heater_identity(self):
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        cfg = replace(self.config(), heater_entity=None, auxiliary_room=None)
        self.assertIsNone(self.issue(cfg))

    async def test_positive_override_requires_identity_even_without_automatic_floor(self):
        cfg = self.config(minimum=0)
        cfg = replace(cfg, fan_controls={key: replace(fan, speed_override=70)
                      if fan.entity_id == self.fan else fan for key, fan in cfg.fan_controls.items()})
        runtime, _ = await self.runtime(cfg=cfg)
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        self.calls.clear()
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        with self.assertRaises(HAError):
            await asyncio.to_thread(runtime.api.service, "fan", "turn_on", entity_id=self.fan, percentage=70)
        self.assertEqual(self.fan_calls(), [])

    async def test_recovery_persists_override_baseline_independently_of_later_edits(self):
        cfg = self.config(fallback=True)
        cfg = replace(cfg, fan_controls={key: replace(fan, speed_override=70)
                      if fan.entity_id == self.fan else fan for key, fan in cfg.fan_controls.items()})
        controls = NativeControls(self.hass, cfg, True, "override_recovery", lambda: None)
        controls.owned = (self.fan,)
        controls.handoff = handoff.arm(controls, controls.owned)
        await controls._save(controls.owned, ())
        restored = NativeControls(self.hass, self.config(fallback=True), True, "override_recovery", lambda: None)
        await restored.async_load()
        self.assertEqual(restored.handoff["fan_minimum_percentages"], {self.fan: 70})
        self.assertEqual(restored.handoff["fan_percentage"], 40)
        self.assertEqual(self.calls, [])

    async def test_remapped_heater_keeps_persisted_manual_alias_protected(self):
        original = NativeControls(self.hass, self.config(), False, "manual_remap", lambda: None)
        await original.async_load()
        await original.async_set_auxiliary_manual(True)
        saved = await original.store.async_load()
        replacement = "climate.synthetic_replacement_auxiliary"
        self.hass.states.async_set(replacement, "off", {
            **self.hass.states.get(self.heater).attributes})
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.owner.entry_id,
            identifiers={("fixture_bridge", "replacement_auxiliary")})
        self.register(replacement, device.id)
        config = replace(self.config(), heater_entity=replacement)
        restored = NativeControls(self.hass, config, False, "manual_remap", lambda: None)
        await restored.async_load()
        self.assertTrue(restored.auxiliary_manual)
        self.assertEqual(restored.auxiliary_manual_entity, self.heater)
        self.alias()
        for activation in (True, False):
            with self.subTest(activation=activation):
                issue = configuration.purifier_identity_issue(
                    self.hass, config, self.fan, activation=activation, controls=restored)
                self.assertIsNotNone(issue)
                self.assertIn("alias", issue)
        self.assertEqual(await restored.store.async_load(), saved)
        self.assertEqual(self.calls, [])

    async def test_native_rechecks_late_alias_for_power_speed_and_off(self):
        runtime = await self.ready()
        self.alias()
        for service, data in (("turn_on", {"percentage": 20}),
                              ("set_percentage", {"percentage": 100}),
                              ("turn_off", {})):
            with self.subTest(service=service):
                with self.assertRaises(HAError):
                    await asyncio.to_thread(runtime.api.service, "fan", service, entity_id=self.fan, **data)
        self.assertEqual(self.fan_calls(), [])

    async def test_native_rejects_identity_loss_for_floor_but_allows_independent_off(self):
        runtime = await self.ready()
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        with self.assertRaises(HAError):
            await asyncio.to_thread(runtime.api.service, "fan", "turn_on", entity_id=self.fan, percentage=20)
        self.assertEqual(self.fan_calls(), [])
        await asyncio.to_thread(runtime.api.service, "fan", "turn_off", entity_id=self.fan)
        self.assertEqual(self.fan_calls()[-1][1], "turn_off")

    async def test_legacy_fan_without_registry_identity_remains_operable(self):
        runtime = await self.ready(minimum=0)
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        await asyncio.to_thread(runtime.api.service, "fan", "turn_on", entity_id=self.fan, percentage=60)
        self.assertEqual(self.fan_calls()[-1][1], "turn_on")

    async def test_remapping_after_native_job_was_queued_is_checked_before_dispatch(self):
        runtime = await self.ready()
        queued, release = asyncio.Event(), asyncio.Event()
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name == "Home Climate device service":
                async def delayed():
                    queued.set()
                    await release.wait()
                    return await coroutine
                return original(delayed(), name, **kwargs)
            return original(coroutine, name, **kwargs)

        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            command = asyncio.create_task(asyncio.to_thread(
                runtime.api.service, "fan", "turn_on", entity_id=self.fan, percentage=20,
            ))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                self.assertEqual(self.fan_calls(), [])
                self.alias()
                release.set()
                with self.assertRaises(HAError):
                    await command
            finally:
                release.set()
                await asyncio.gather(command, return_exceptions=True)
        self.assertEqual(self.fan_calls(), [])
        self.assertEqual(runtime.controls.inflight, 0)

    async def test_queued_fan_cleanup_cannot_stop_manually_owned_auxiliary_alias(self):
        runtime = await self.ready()
        controls = runtime.controls
        await controls.async_set_auxiliary_manual(True)
        await self.settle_watchdog(controls)
        state = self.hass.states.get(self.heater)
        self.hass.states.async_set(self.heater, "heat", {**state.attributes, "hvac_action": "heating"})
        self.calls.clear()
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name == "Home Climate watchdog device stop":
                self.alias()
            return original(coroutine, name, **kwargs)

        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            self.assertFalse(await controls._off(self.fan))
        self.assertEqual(self.fan_calls(), [])
        self.assertEqual(self.hass.states.get(self.heater).state, "heat")
        self.assertTrue(controls.auxiliary_manual)
        self.assertIn(self.fan, controls._fan_problems)

    async def test_recovery_handoff_inhibits_alias_and_unknown_floor_identity(self):
        runtime = await self.ready(fallback=True)
        controls = runtime.controls
        for device_id in (self.heater_device.id, None):
            with self.subTest(device_id=device_id):
                er.async_get(self.hass).async_update_entity(self.fan, device_id=device_id)
                controls.enabled = False
                controls.handoff = handoff.arm(controls, (self.fan,))
                controls.handoff["stage"] = "applying"
                await handoff.step(controls)
                self.assertEqual(self.fan_calls(), [])
                self.assertIn(self.fan, controls.handoff["problems"])
                self.assertFalse(controls.handoff["requests"].get(self.fan, {}).get("dispatched"))

    async def test_recovery_handoff_rechecks_identity_after_job_was_queued(self):
        runtime = await self.ready(fallback=True)
        controls = runtime.controls
        controls.enabled = False
        controls.handoff = handoff.arm(controls, (self.fan,))
        controls.handoff["stage"] = "applying"
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name == "Home Climate recovery setting":
                self.alias()
            return original(coroutine, name, **kwargs)

        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            await handoff.step(controls)
        self.assertEqual(self.fan_calls(), [])
        self.assertIn(self.fan, controls.handoff["problems"])
        self.assertFalse(controls.handoff["requests"][self.fan].get("dispatched"))

    async def test_persisted_handoff_retains_100_floor_and_ordinary_40_fallback_after_restart(self):
        ordinary = next(entity for entity in self.cfg.fans.values() if entity != self.fan)
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.owner.entry_id,
            identifiers={("fixture_bridge", "synthetic_second_purifier")},
        )
        self.register(ordinary, device.id)
        controls, stored = await self.restored_fan_handoff({self.fan: 100, ordinary: 20})
        minimums = {self.fan: 100, ordinary: 20}
        self.assertEqual(stored["handoff"]["fan_minimum_percentages"], minimums)
        self.assertEqual(controls.handoff["fan_minimum_percentages"], minimums)
        await handoff.step(controls)
        sent = {data["entity_id"]: data["percentage"] for domain, service, data in self.calls
                if domain == "fan" and service == "turn_on"}
        self.assertEqual(sent, {self.fan: 100, ordinary: 40})
        persisted = (await controls.store.async_load())["handoff"]
        self.assertEqual(persisted["fan_minimum_percentages"], minimums)
        self.assertTrue(all(persisted["requests"][entity]["dispatched"] for entity in minimums))
        await handoff.step(controls)
        self.assertIsNone(controls.handoff)
        self.assertNotIn("handoff", await controls.store.async_load())

    async def test_old_saved_handoff_without_minimum_map_keeps_legacy_dispatch(self):
        controls, stored = await self.restored_fan_handoff({self.fan: 0}, legacy=True)
        self.assertNotIn("fan_minimum_percentages", stored["handoff"])
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        await handoff.step(controls)
        self.assertEqual(self.fan_calls()[-1][1:], ("turn_on", {"entity_id": self.fan, "percentage": 40}))
        await handoff.step(controls)
        self.assertIsNone(controls.handoff)

    async def test_malformed_saved_minimum_maps_are_rejected(self):
        _, stored = await self.restored_fan_handoff({self.fan: 100})
        for malformed in (None, [], {"fan.synthetic_unmapped": 20}, {self.fan: -1},
                          {self.fan: 101}, {self.fan: True}, {self.fan: "20"},
                          {self.fan: float("nan")}, {self.fan: float("inf")}):
            with self.subTest(minimums=malformed):
                plan = deepcopy(stored["handoff"])
                plan["fan_minimum_percentages"] = malformed
                with self.assertRaises(ValueError):
                    handoff.validate(plan)
        self.assertEqual(self.calls, [])

    async def test_saved_floor_still_requires_identity_when_current_floor_was_removed(self):
        controls, _ = await self.restored_fan_handoff({self.fan: 100}, remove_current_floors=True)
        self.assertTrue(all(control.minimum_percentage == 0 for control in controls.config.fan_controls.values()))
        er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
        await handoff.step(controls)
        self.assertEqual(self.fan_calls(), [])
        self.assertIn(self.fan, controls.handoff["problems"])
        self.assertNotIn(self.fan, controls.handoff["requests"])

    async def test_saved_floor_rechecks_identity_at_dispatch_after_current_floor_was_removed(self):
        controls, _ = await self.restored_fan_handoff({self.fan: 100}, remove_current_floors=True)
        original = self.hass.async_create_background_task

        def create(coroutine, name, **kwargs):
            if name == "Home Climate recovery setting":
                er.async_get(self.hass).async_update_entity(self.fan, device_id=None)
            return original(coroutine, name, **kwargs)

        with patch.object(self.hass, "async_create_background_task", side_effect=create):
            await handoff.step(controls)
        self.assertEqual(self.fan_calls(), [])
        self.assertIn(self.fan, controls.handoff["problems"])
        self.assertFalse(controls.handoff["requests"][self.fan].get("dispatched"))

    async def test_99_percent_report_cannot_acknowledge_persisted_100_percent_floor(self):
        controls, _ = await self.restored_fan_handoff({self.fan: 100})
        await handoff.step(controls)
        self.assertEqual(self.fan_calls()[-1][2]["percentage"], 100)
        state = self.hass.states.get(self.fan)
        self.hass.states.async_set(self.fan, "on", {**state.attributes, "percentage": 99})
        await handoff.step(controls)
        self.assertIsNotNone(controls.handoff)
        self.assertNotIn(self.fan, controls.handoff["completed"])
        self.assertIn(self.fan, controls.owned)
        self.assertEqual((await controls.store.async_load())["handoff"]["fan_minimum_percentages"][self.fan], 100)
        self.hass.states.async_set(self.fan, "on", {**state.attributes, "percentage": 100})
        await handoff.step(controls)
        self.assertIsNone(controls.handoff)

    async def test_saved_floor_is_quantized_up_to_a_supported_integer_speed(self):
        controls, _ = await self.restored_fan_handoff({self.fan: 44})
        await handoff.step(controls)
        self.assertEqual(self.fan_calls()[-1][2]["percentage"], 50)
        self.assertGreaterEqual(self.hass.states.get(self.fan).attributes["percentage"], 44)

    async def test_saved_fallback_is_not_rounded_below_its_requested_speed(self):
        for fallback, step, expected in ((40, 100 / 3, 66), (45, 10, 50)):
            with self.subTest(fallback=fallback, step=step):
                controls, _ = await self.restored_fan_handoff({self.fan: 20})
                controls.handoff["fan_percentage"] = fallback
                await controls._save(controls.owned, ())
                state = self.hass.states.get(self.fan)
                self.hass.states.async_set(self.fan, state.state, {
                    **state.attributes, "percentage_step": step})
                await handoff.step(controls)
                self.assertEqual(self.fan_calls()[-1][2]["percentage"], expected)
                self.assertGreaterEqual(expected, fallback)
                await handoff.step(controls)
                self.assertIsNone(controls.handoff)

    async def test_below_fallback_report_cannot_acknowledge_a_lower_purifier_floor(self):
        controls, _ = await self.restored_fan_handoff({self.fan: 20})
        await handoff.step(controls)
        self.assertEqual(self.fan_calls()[-1][2]["percentage"], 40)
        state = self.hass.states.get(self.fan)
        self.hass.states.async_set(self.fan, "on", {**state.attributes, "percentage": 39})
        await handoff.step(controls)
        self.assertIsNotNone(controls.handoff)
        self.assertNotIn(self.fan, controls.handoff["completed"])
        self.assertIn(self.fan, controls.owned)
        self.hass.states.async_set(self.fan, "on", {**state.attributes, "percentage": 40})
        await handoff.step(controls)
        self.assertIsNone(controls.handoff)
