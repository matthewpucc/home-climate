"""Capability history is stable evidence, not assumptions about device models."""
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.house_climate.capabilities import CapabilityEvidence
from tests_homeassistant import test_integration_reload as fixtures


class CapabilityEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpBase = fixtures.IntegrationReloadTests.asyncSetUp
    asyncTearDownBase = fixtures.IntegrationReloadTests.asyncTearDown
    asyncSetUpNative = fixtures.IntegrationReloadTests.asyncSetUpBase
    asyncTearDownNative = fixtures.IntegrationReloadTests.asyncTearDownBase
    handle = fixtures.IntegrationReloadTests.handle
    entry = fixtures.IntegrationReloadTests.entry
    register = fixtures.IntegrationReloadTests.register

    async def asyncSetUp(self):
        # The shared fixture names its native setup through these attributes.
        self.asyncSetUpBase = self.asyncSetUpNative
        self.asyncTearDownBase = self.asyncTearDownNative
        await fixtures.IntegrationReloadTests.asyncSetUp(self)
        self.evidence = CapabilityEvidence(self.hass, clock=self.clock)
        self.item = er.async_get(self.hass).async_get(self.head)

    async def asyncTearDown(self):
        await self.evidence.async_close()
        await fixtures.IntegrationReloadTests.asyncTearDown(self)

    def report(self, now, modes=("off", "heat", "cool"), *, mode="off", preset=None, operation=None):
        self.clock.return_value = now
        self.hass.states.async_set(self.head, mode, {
            "hvac_modes": list(modes), "preset_mode": preset, "operation_mode": operation,
        })
        return self.evidence.observe(self.item, self.owner, self.hass.states.get(self.head))

    def establish(self, modes=("off", "heat", "cool"), **kwargs):
        self.report(0, modes, **kwargs)
        self.report(60, modes, **kwargs)
        return self.report(120, modes, **kwargs)

    async def test_three_fresh_reports_and_elapsed_stability_are_both_required(self):
        self.assertIsNone(self.report(0)["expected"])
        state = self.hass.states.get(self.head)
        for now in (60, 120, 600):
            self.clock.return_value = now
            self.assertIsNone(self.evidence.observe(self.item, self.owner, state)["expected"])
        self.assertIsNone(self.report(601)["expected"])
        result = self.report(602)
        self.assertEqual(result["expected"], {"off", "heat", "cool"})
        self.assertTrue(result["stable"])

    async def test_short_burst_then_cached_polls_cannot_establish_a_baseline(self):
        for now in (0, 1, 2):
            self.assertIsNone(self.report(now)["expected"])
        state = self.hass.states.get(self.head)
        self.clock.return_value = 120
        self.assertIsNone(self.evidence.observe(self.item, self.owner, state)["expected"])
        self.assertIsNotNone(self.report(121)["expected"])

    async def test_loss_is_based_on_actual_known_modes_not_hardcoded_heat(self):
        result = self.establish(("off", "cool"))
        self.assertEqual(result["expected"], {"off", "cool"})
        self.assertEqual(self.report(180, ("off", "cool"))["missing"], set())
        self.assertEqual(self.report(240, ("off",))["missing"], {"cool"})

    async def test_established_baseline_is_not_downgraded_by_persistent_shrinkage(self):
        baseline = self.establish()["expected"]
        for now in (180, 240, 300, 10_000):
            result = self.report(now, ("off", "cool"))
            self.assertEqual(result["expected"], baseline)
            self.assertEqual(result["missing"], {"heat"})
            self.assertFalse(result["stable"])

    async def test_heat_cool_and_off_transitions_keep_the_same_context(self):
        first = self.establish(mode="cool")
        lost = self.report(180, ("off", "cool"), mode="off")
        self.assertEqual(first["identity"], lost["identity"])
        self.assertEqual(lost["missing"], {"heat"})

    async def test_preset_operation_and_registry_owner_changes_do_not_reuse_the_old_baseline(self):
        first = self.establish()
        preset = self.report(180, ("off", "cool"), preset="eco")
        operation = self.report(240, ("off", "cool"), operation="summer")
        self.assertNotEqual(first["identity"], preset["identity"])
        self.assertNotEqual(first["identity"], operation["identity"])
        self.assertIsNone(preset["expected"])
        self.assertIsNone(operation["expected"])
        other = self.entry("mitsubishi", "new_owner")
        changed = er.async_get(self.hass).async_update_entity(self.head, config_entry_id=other.entry_id)
        self.clock.return_value = 300
        result = self.evidence.observe(changed, other, self.hass.states.get(self.head))
        self.assertIsNone(result["expected"])
        self.assertNotEqual(first["identity"], result["identity"])

    async def test_unavailable_and_startup_metadata_cannot_establish_or_confirm_loss(self):
        self.establish()
        self.assertIsNone(self.report(180, ("off", "cool"), mode="unavailable"))
        self.owner._async_set_state(self.hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        self.assertIsNone(self.report(240, ("off", "cool")))

    async def test_restoration_requires_stable_fresh_reports_before_stable_success(self):
        self.establish()
        self.report(180, ("off", "cool"))
        self.assertFalse(self.report(240)["stable"])
        self.assertFalse(self.report(300)["stable"])
        self.assertTrue(self.report(360)["stable"])

    async def test_restored_startup_state_is_not_capability_evidence(self):
        self.hass.states.async_set(self.head, "off", {"hvac_modes": ["off", "heat", "cool"], "restored": True})
        for now in (0, 60, 120):
            self.clock.return_value = now
            self.assertIsNone(self.evidence.observe(self.item, self.owner, self.hass.states.get(self.head)))
        self.assertFalse(self.evidence.baselines)

    async def test_unavailable_gap_breaks_the_stable_restoration_window(self):
        self.establish()
        self.report(180, ("off", "cool"))
        self.report(240)
        self.report(300)
        self.report(359, mode="unavailable")
        self.assertFalse(self.report(360)["stable"])
        self.report(420)
        self.assertTrue(self.report(480)["stable"])

    async def test_context_round_trip_does_not_reuse_a_previous_candidate_window(self):
        self.establish()
        self.report(180, ("off", "cool"))
        self.report(240)
        self.report(300)
        self.report(359, preset="eco")
        self.assertFalse(self.report(360)["stable"])

    async def test_stable_superset_can_expand_a_baseline(self):
        self.establish(("off", "cool"))
        self.report(180)
        self.report(240)
        self.assertEqual(self.report(300)["expected"], {"off", "heat", "cool"})

    async def test_optional_modes_do_not_prevent_managed_mode_restoration(self):
        self.establish(("off", "heat", "cool", "dry"))
        self.report(180, ("off", "cool"))
        self.assertFalse(self.report(240, ("off", "heat", "cool"))["stable"])
        self.report(300, ("off", "heat", "cool"))
        restored = self.report(360, ("off", "heat", "cool"))
        self.assertTrue(restored["stable"])
        self.assertEqual(restored["missing"], set())

    async def test_storage_failure_falls_back_to_runtime_learning(self):
        with patch("custom_components.house_climate.capabilities.Store.async_load", AsyncMock(side_effect=OSError("disk unavailable"))):
            await self.evidence.async_load("capability_learning")
        self.assertIn("using runtime observations", self.evidence.storage_warning)
        self.evidence.store = None
        self.assertEqual(self.establish()["expected"], {"off", "heat", "cool"})

    async def test_saved_baseline_survives_a_new_tracker_without_exposing_device_ids_in_storage_keys(self):
        await self.evidence.async_load("capability_learning")
        learned = self.establish()
        await self.evidence._save_task
        replacement = CapabilityEvidence(self.hass, clock=self.clock)
        try:
            await replacement.async_load("capability_learning")
            state = self.hass.states.get(self.head)
            self.assertEqual(replacement.observe(self.item, self.owner, state)["expected"], learned["expected"])
            self.assertTrue(all(len(key) == 64 for key in replacement.baselines))
        finally:
            await replacement.async_close()


if __name__ == "__main__":
    unittest.main()
