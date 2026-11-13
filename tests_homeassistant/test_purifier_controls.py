"""Dashboard percentages persist and reach the normal planner/executor without reload."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.house_climate.button import ResumePurifierSpeed
from custom_components.house_climate.number import RoomPurifierSpeed
from custom_components.house_climate.purifier import RoomPurifierSpeeds
from custom_components.house_climate.controller.cadence import DecisionCadence
from custom_components.house_climate.controller.config import Config, Settings, FallbackPolicy
from custom_components.house_climate.controller.executor import Executor
from tests.test_controller import FakeHA
from tests.test_directed_airflow import directed_config, FAN
from tests.test_rooms import states, NOW


class PurifierControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        raw = directed_config()
        raw["rooms"]["beta"]["fans"] = [{"entity_id": "fan.synthetic_beta", "strategy": "always",
                                          "percentage": 30, "minimum_percentage": 20, "speed_override": 65}]
        raw["rooms"]["alpha"]["fans"][0]["minimum_percentage"] = 20
        self.cfg = Config.from_dict(raw)
        self.manager = await self.make_manager()
        self.runtime = SimpleNamespace(config=self.cfg, purifier_speeds=self.manager,
                                       stop_event=threading.Event())
        self.entry = SimpleNamespace(entry_id="synthetic_controller", runtime_data=self.runtime)
        self.number = RoomPurifierSpeed(self.entry, "alpha")
        self.resume = ResumePurifierSpeed(self.entry, "alpha")

    async def asyncTearDown(self):
        await self.manager.async_close()
        await self.hass.async_stop()
        self.directory.cleanup()

    async def make_manager(self, config=None):
        manager = RoomPurifierSpeeds(self.hass, config or self.cfg, "synthetic_controller", Mock(), threading.Event())
        await manager.async_load()
        return manager

    async def test_number_set_and_button_clear_persist_independently(self):
        self.assertIsNone(self.number.native_value)
        self.assertEqual(self.number.extra_state_attributes["speed_mode"], "automatic")
        self.assertEqual(self.manager.effective("beta"), 65)
        await self.number.async_set_native_value(70)
        self.assertEqual(self.number.native_value, 70)
        self.assertTrue(self.manager.wake_event.is_set())
        self.manager.notify.assert_called_once()
        await self.manager.async_set("beta", 50)
        restored = await self.make_manager()
        self.assertEqual((restored.effective("alpha"), restored.effective("beta")), (70, 50))
        await self.resume.async_press()
        restored = await self.make_manager()
        self.assertIsNone(restored.effective("alpha"))
        self.assertEqual(restored.effective("beta"), 50)
        # Clearing a configured override also returns that room to automatic.
        await self.manager.async_set("beta", None)
        self.assertIsNone((await self.make_manager()).effective("beta"))

    async def test_validation_failed_storage_and_closed_controls_are_atomic(self):
        await self.number.async_set_native_value(0)
        original = await self.manager.store.async_load()
        for value in (True, False, "70", None, -1, 101, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await self.number.async_set_native_value(value)
        with self.assertRaises(ValueError):
            await self.manager.async_set("unknown", 70)
        self.assertEqual(await self.manager.store.async_load(), original)
        self.manager.notify.reset_mock()
        with patch.object(self.manager.store, "async_save", new=AsyncMock(side_effect=OSError("disk full"))):
            with self.assertRaises(ServiceValidationError):
                await self.number.async_set_native_value(60)
        self.assertEqual(self.number.native_value, 0)
        self.manager.notify.assert_not_called()
        await self.manager.async_close()
        with self.assertRaises(ServiceValidationError):
            await self.number.async_set_native_value(60)

    async def test_concurrent_room_edits_preserve_both_and_config_changes_invalidate_only_one(self):
        await asyncio.gather(self.manager.async_set("alpha", 70), self.manager.async_set("beta", 0))
        self.assertEqual(dict(self.manager._snapshot), {"alpha": 70, "beta": 0})
        changed = replace(self.cfg, fan_controls={key: replace(fan, minimum_percentage=25)
                          if fan.room == "alpha" else fan for key, fan in self.cfg.fan_controls.items()})
        restored = await self.make_manager(changed)
        self.assertIsNone(restored.effective("alpha"))
        self.assertEqual(restored.effective("beta"), 0)
        self.assertNotIn("alpha", (await restored.store.async_load())["rooms"])
        self.assertIsNone((await self.make_manager()).effective("alpha"))

    async def test_new_recovery_plan_uses_override_but_saved_plan_is_not_rewritten(self):
        from custom_components.house_climate.handoff import arm
        controls = SimpleNamespace(config=replace(self.cfg, fallback=FallbackPolicy()), live=True,
                                   fan_controls_provider=self.manager.fan_controls)
        await self.manager.async_set("alpha", 70)
        plan = arm(controls, self.cfg.fans.values())
        self.assertEqual(plan["fan_minimum_percentages"][FAN], 70)
        await self.manager.async_set("alpha", 0)
        updated = arm(controls, self.cfg.fans.values())
        self.assertNotIn(FAN, updated.get("fan_minimum_percentages", {}))
        self.assertEqual(plan["fan_minimum_percentages"][FAN], 70)
        await self.manager.async_set("alpha", None)
        self.assertEqual(arm(controls, self.cfg.fans.values())["fan_minimum_percentages"][FAN], 20)

    async def test_invalid_persisted_values_cannot_become_worker_overrides(self):
        await self.manager.store.async_save({"rooms": {
            "alpha": {"percentage": True, "fingerprint": self.manager._fingerprint("alpha")},
            "beta": {"percentage": 101, "fingerprint": self.manager._fingerprint("beta")},
            "missing": {"percentage": 50, "fingerprint": "invalid"}}})
        restored = await self.make_manager()
        self.assertEqual(dict(restored._snapshot), {})
        self.assertEqual(restored.effective("beta"), 65)

    async def test_live_settings_replan_immediately_and_executor_obeys_zero_and_low_override(self):
        cadence = DecisionCadence(self.cfg, clock=lambda: 0)
        data = states(self.cfg, {"alpha": 73, "beta": 72, "gamma": 68})
        ha = FakeHA(data)
        executor = Executor(self.cfg, ha, "°F")
        for tick, value in enumerate((70, 0, 10, None, 100)):
            if value is None:
                await self.resume.async_press()
            else:
                await self.number.async_set_native_value(value)
            settings = self.manager.settings(Settings.read(data, self.cfg))
            # All ticks precede the ordinary comfort timer. The settings change
            # must invalidate its cached plan while retaining equipment state.
            now = NOW + timedelta(seconds=tick)
            plan = cadence.decide(data, now, settings, monotonic_now=tick)
            self.assertEqual(plan.fans[FAN], value if value is not None else 60)
            self.assertEqual(plan.fans["fan.synthetic_beta"], 65)
            executor.step(plan, ha.states(), tick * 15)
            self.assertEqual(ha.data[FAN]["state"], "off" if value == 0 else "on")
            if value != 0:  # HA fans may retain their last selected percentage while off.
                self.assertEqual(ha.data[FAN]["attributes"]["percentage"], value if value is not None else 20)
        self.assertEqual(self.cfg.fan_controls[FAN].speed_override, None)
        self.assertEqual(self.cfg.fan_controls["fan.synthetic_beta"].speed_override, 65)

    async def test_room_minimum_is_live_persistent_and_does_not_cap_demand(self):
        from custom_components.house_climate.number import RoomPurifierMinimum
        minimum = RoomPurifierMinimum(self.entry, "alpha")
        self.assertEqual(minimum.native_value, 20)
        cadence = DecisionCadence(self.cfg, clock=lambda: 0)
        data = states(self.cfg, {"alpha": 73, "beta": 72, "gamma": 68})
        for tick, value in enumerate((0, 75, 10)):
            await minimum.async_set_native_value(value)
            settings = self.manager.settings(Settings.read(data, self.cfg))
            plan = cadence.decide(data, NOW + timedelta(seconds=tick), settings, monotonic_now=tick)
            self.assertEqual(plan.fans[FAN], max(value, 60))
            self.assertEqual((await self.make_manager()).minimum("alpha"), value)
            self.assertEqual(self.manager.minimum("beta"), 20)
        await self.number.async_set_native_value(5)
        await minimum.async_set_native_value(80)
        settings = self.manager.settings(Settings.read(data, self.cfg))
        plan = cadence.decide(data, NOW + timedelta(seconds=4), settings, monotonic_now=4)
        self.assertEqual(plan.fans[FAN], 5)
        await self.resume.async_press()
        self.assertEqual(self.manager.minimum("alpha"), 80)
        self.assertEqual(self.manager.fan_controls()[FAN].requested_percentage(60), 80)

    async def test_minimum_storage_validation_and_old_override_migration(self):
        from custom_components.house_climate.number import RoomPurifierMinimum
        minimum = RoomPurifierMinimum(self.entry, "alpha")
        await self.number.async_set_native_value(10)
        restored = await self.make_manager()  # Old store shape contains only rooms.
        self.assertEqual(restored.effective("alpha"), 10)
        self.assertEqual(restored.minimum("alpha"), 20)
        await asyncio.gather(self.manager.async_set("alpha", 30), self.manager.async_set_minimum("beta", 40))
        restored = await self.make_manager()
        self.assertEqual(restored.effective("alpha"), 30)
        self.assertEqual(restored.minimum("beta"), 40)
        for value in (True, False, "70", None, -1, 101, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await minimum.async_set_native_value(value)
        with patch.object(self.manager.store, "async_save", new=AsyncMock(side_effect=OSError("disk full"))):
            with self.assertRaises(ServiceValidationError):
                await minimum.async_set_native_value(60)
        self.assertEqual(minimum.native_value, 20)
        await self.manager.async_close()
        with self.assertRaises(ServiceValidationError):
            await minimum.async_set_native_value(60)

    async def test_minimum_recovery_and_configuration_invalidation(self):
        from custom_components.house_climate.handoff import arm
        await self.manager.async_set_minimum("alpha", 70)
        controls = SimpleNamespace(config=replace(self.cfg, fallback=FallbackPolicy()), live=True,
                                   fan_controls_provider=self.manager.fan_controls)
        plan = arm(controls, self.cfg.fans.values())
        self.assertEqual(plan["fan_minimum_percentages"][FAN], 70)
        await self.manager.async_set_minimum("alpha", 0)
        self.assertEqual(plan["fan_minimum_percentages"][FAN], 70)
        changed = replace(self.cfg, fan_controls={key: replace(fan, minimum_percentage=25)
                          if fan.room == "alpha" else fan for key, fan in self.cfg.fan_controls.items()})
        restored = await self.make_manager(changed)
        self.assertEqual(restored.minimum("alpha"), 25)
