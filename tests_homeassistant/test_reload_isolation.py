"""One failed head must not reload a bridge under healthy room calls."""
import unittest

from homeassistant.config_entries import ConfigEntryState

from tests_homeassistant import test_integration_reload as fixtures


class ReloadIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.IntegrationReloadTests.asyncSetUpBase
    asyncTearDownBase = fixtures.IntegrationReloadTests.asyncTearDownBase
    handle = fixtures.IntegrationReloadTests.handle
    until = fixtures.IntegrationReloadTests.until
    entry = fixtures.IntegrationReloadTests.entry
    register = fixtures.IntegrationReloadTests.register
    status = fixtures.IntegrationReloadTests.status
    failed_for = fixtures.IntegrationReloadTests.failed_for

    async def asyncSetUp(self):
        await fixtures.IntegrationReloadTests.asyncSetUp(self)
        self.head = self.cfg.heads["bonus"]
        self.primary = self.cfg.heads["primary"]
        self.register(self.head, self.owner)
        self.register(self.primary, self.owner)
        self.hass.states.async_set(self.head, "cool", {
            "hvac_modes": ["off", "heat", "cool"], "temperature": 64,
        })
        self.primary_state("cool")

    async def asyncTearDown(self):
        try:
            self.assertEqual(self.calls, [])
        finally:
            await fixtures.IntegrationReloadTests.asyncTearDown(self)

    def primary_state(self, state):
        self.hass.states.async_set(self.primary, state, {
            "hvac_modes": ["off", "heat", "cool"], "temperature": 63,
        })

    def assert_deferred_without_budget(self):
        self.reload.assert_not_awaited()
        self.assertEqual(self.manager._attempts.get(self.owner.entry_id, 0), 0)
        self.assertNotIn(self.owner.entry_id, self.manager._next_retry)
        detail = self.manager.diagnostics["reload_details"][self.owner.entry_id]
        self.assertIn("Reload deferred while healthy heads are active", detail["reason"])
        self.assertEqual(detail["healthy_active_heads"], [self.primary])
        self.assertEqual(detail["healthy_active_rooms"], ["Primary"])
        self.assertIn(self.primary, detail["reason"])
        self.assertIn("Primary", detail["reason"])
        self.assertIsNone(detail["next_retry_at"])

    async def test_healthy_shared_head_defers_without_consuming_attempts_or_cooldown(self):
        status = await self.failed_for()
        self.assert_deferred_without_budget()
        self.clock.return_value = 10_000
        self.manager.observe(status)
        self.assert_deferred_without_budget()
        self.assertIsNone(self.manager._task)

    async def test_healthy_heating_head_also_defers_shared_bridge_reload(self):
        self.primary_state("heat")
        await self.failed_for()
        self.assert_deferred_without_budget()

    async def test_healthy_head_off_allows_previously_deferred_reload_immediately(self):
        status = await self.failed_for()
        self.assert_deferred_without_budget()
        self.primary_state("off")
        self.clock.return_value = 121
        self.manager.observe(status)
        await self.manager._task
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)
        self.assertGreater(self.manager._next_retry[self.owner.entry_id], 121)

    async def test_active_head_on_unrelated_bridge_does_not_block_failed_owner(self):
        self.primary_state("off")
        guest = self.cfg.heads["guest"]
        other = self.entry("mitsubishi", "other_owner", ConfigEntryState.LOADED)
        self.register(guest, other)
        self.hass.states.async_set(guest, "cool", {"hvac_modes": ["off", "cool"]})
        await self.failed_for()
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def test_heads_in_recovery_do_not_count_as_healthy_running_heads(self):
        await self.failed_for(120, self.head, self.primary)
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        self.assertEqual(self.manager._attempts[self.owner.entry_id], 1)

    async def test_unavailable_other_head_does_not_prevent_recovery(self):
        self.primary_state("unavailable")
        await self.failed_for()
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def test_head_starting_after_task_queued_defers_without_charging_attempt(self):
        self.primary_state("off")
        status = self.status(self.head)
        self.manager.observe(status)
        self.clock.return_value = 120
        self.manager.observe(status)
        task = self.manager._task
        self.assertIsNotNone(task)
        # No await occurs between queueing and this state change, so the task
        # must recheck current HA states when it begins dispatch.
        self.primary_state("cool")
        await task
        self.assert_deferred_without_budget()
        self.assertIsNone(self.manager._active_entry)
        self.primary_state("off")
        self.clock.return_value = 121
        self.manager.observe(status)
        await self.manager._task
        self.reload.assert_awaited_once_with(self.owner.entry_id)

    async def test_explicit_manual_reload_remains_available_with_healthy_head_active(self):
        self.runtime.live = False
        self.runtime.controls.enabled = False
        self.assertTrue(await self.manager.async_reload_entity(self.head))
        self.reload.assert_awaited_once_with(self.owner.entry_id)
        self.assertEqual(self.hass.states.get(self.primary).state, "cool")
        self.assertEqual(self.manager._attempts.get(self.owner.entry_id, 0), 0)


if __name__ == "__main__":
    unittest.main()
