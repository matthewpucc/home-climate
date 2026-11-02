"""Seasonal selection survives restart without inventing persisted observations."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.house_climate.seasonal_state import SeasonalState


class SeasonalPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_real_storage_restores_profile_only(self):
        state = SeasonalState(self.hass, "fixture")
        await state.async_load()
        self.assertIsNone(state.profile)
        state.observe({"profile": "warm", "average_f": 65, "coverage_seconds": 1800})
        await state.async_close()
        restored = SeasonalState(self.hass, "fixture")
        await restored.async_load()
        self.assertEqual(restored.profile, "warm")
        self.assertEqual(await restored.store.async_load(), {"profile": "warm"})

    async def test_latest_transition_is_not_lost_during_save(self):
        state = SeasonalState(self.hass, "fixture")
        entered, release, saved = asyncio.Event(), asyncio.Event(), []

        async def save(data):
            saved.append(dict(data))
            if len(saved) == 1:
                entered.set()
                await release.wait()

        with patch.object(state.store, "async_save", side_effect=save):
            state.observe({"profile": "warm"})
            await entered.wait()
            state.observe({"profile": "cool"})
            release.set()
            await state.async_close()
        self.assertEqual(saved, [{"profile": "warm"}, {"profile": "cool"}])
        self.assertEqual(state._persisted, "cool")

    async def test_storage_failure_is_visible_and_next_observation_can_retry(self):
        state = SeasonalState(self.hass, "fixture")
        with self.assertLogs("custom_components.house_climate.seasonal_state", level="WARNING"), \
                patch.object(state.store, "async_save", side_effect=OSError("fixture failure")):
            state.observe({"profile": "warm"})
            await state.async_close()
        self.assertIn("Could not persist", state.error)
        self.assertIsNone(state._persisted)
        state.observe({"profile": "warm"})
        await state.async_close()
        self.assertIsNone(state.error)
        self.assertEqual((await state.store.async_load())["profile"], "warm")

    async def test_malformed_saved_profile_and_read_error_do_not_restore_temperature_history(self):
        state = SeasonalState(self.hass, "fixture")
        with patch.object(state.store, "async_load", return_value={"profile": "invalid", "average_f": 65}):
            await state.async_load()
        self.assertIsNone(state.profile)
        self.assertIn("invalid", state.error)
        with self.assertLogs("custom_components.house_climate.seasonal_state", level="WARNING"), \
                patch.object(state.store, "async_load", side_effect=OSError("fixture failure")):
            await state.async_load()
        self.assertIn("Could not load", state.error)
