"""Read and exercise the store/claim/native service boundaries with synthetic HA."""
import asyncio
from unittest import IsolatedAsyncioTestCase

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.house_climate import handoff
from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from custom_components.house_climate.controls import NativeControls
from tests_homeassistant import test_handoff_retries as terminal
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class ManualHeadPersistenceTests(IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    def records(self, entity=None, identity="a" * 32):
        role, head = next(iter(self.cfg.heads.items()))
        return {role: {"entity": entity or head, "registry_id": identity, "reported_off_at": 123.0}}

    def store(self, entry_id):
        return Store(self.hass, 1, f"house_climate.{entry_id}.ownership")

    async def test_default_store_has_no_manual_disposition(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        self.assertFalse(runtime.controls.manual_head_entities)
        self.assertNotIn("manual_heads", await self.store("native").async_load())

    async def test_persisted_manual_head_cannot_be_reclaimed_or_receive_worker_off(self):
        entity, sibling, *_others = self.cfg.heads.values()
        records = self.records()
        store = self.store("manual")
        await store.async_save({"devices": [], "enabled_intent": False, "manual_heads": records})
        runtime, _ = await self.runtime(entry_id="manual")
        self.assertEqual(runtime.controls.manual_head_entities, {entity})
        await self.claim(runtime)
        self.assertNotIn(entity, runtime.controls.owned)
        self.assertIn(sibling, runtime.controls.owned)
        snapshot = await store.async_load()
        self.assertEqual(snapshot["manual_heads"], records)
        self.assertNotIn(entity, snapshot["devices"])
        self.assertTrue(snapshot.get("handoff") is None or entity not in snapshot["handoff"]["heads"])
        restarted = NativeControls(self.hass, runtime.config, True, "manual", lambda: None)
        await restarted.async_load()
        self.assertTrue(restarted.recovering)
        self.assertNotIn(entity, restarted.owned)
        self.assertIn(sibling, restarted.owned)
        self.assertEqual(restarted.manual_head_entities, {entity})
        with self.assertRaises(HAActivationDeferred):
            restarted.head_write_fence.check(entity, restarted.head_write_fence.capture(entity))
        self.calls.clear()
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(runtime.api.service, "climate", "set_hvac_mode",
                                    entity_id=entity, hvac_mode="off")
        with self.assertRaisesRegex(HAActivationDeferred, "blocks shared activation"):
            await asyncio.to_thread(runtime.api.service, "climate", "set_temperature",
                                    entity_id=sibling, hvac_mode="heat", temperature=71)
        self.assertEqual(self.calls, [])
        self.assertTrue(await runtime.controls._off(sibling))
        self.assertEqual({entry[2]["entity_id"] for entry in self.calls}, {sibling})

    async def test_registry_rename_and_changed_wiring_never_reclaim_either_candidate(self):
        registry = er.async_get(self.hass)
        renamed = registry.async_get_or_create("climate", "fixture", "physical")
        role, head = next(iter(self.cfg.heads.items()))
        records = self.records(entity="climate.retired_fixture", identity=renamed.id)
        await self.store("renamed").async_save({"devices": [], "manual_heads": records})
        runtime, _ = await self.runtime(entry_id="renamed")
        self.assertEqual(runtime.controls.manual_head_entities,
                         {"climate.retired_fixture", renamed.entity_id, head})
        self.assertEqual(runtime.controls.manual_heads[role].registry_id, renamed.id)
        await self.claim(runtime)
        self.assertNotIn(head, runtime.controls.owned)
        self.assertEqual((await self.store("renamed").async_load())["manual_heads"], records)

    async def test_overlapping_cleanup_and_malformed_records_are_not_accepted(self):
        head = next(iter(self.cfg.heads.values()))
        for index, (extra, diagnostic) in enumerate((
            ({"devices": [head], "manual_heads": self.records()}, "overlaps pending"),
            ({"manual_heads": []}, "Invalid saved manual"),
            ({"manual_heads": self.records(entity=self.cfg.heater_entity)}, "auxiliary heater"),
            ({"manual_heads": {"guest": {"entity": head, "registry_id": "fixture",
                                        "reported_off_at": True}}}, "Invalid saved manual"),
        )):
            with self.subTest(index=index):
                entry_id = f"rejected-{index}"
                await self.store(entry_id).async_save(extra)
                controls = NativeControls(self.hass, self.cfg, True, entry_id, lambda: None)
                with self.assertRaisesRegex(HAError, diagnostic):
                    await controls.async_load()
        self.assertEqual(self.calls, [])


class ManualHeadTerminalInterlockTest(IsolatedAsyncioTestCase):
    asyncSetUp = terminal.HandoffRetryTests.asyncSetUp
    asyncTearDown = terminal.HandoffRetryTests.asyncTearDown
    report = terminal.HandoffRetryTests.report
    reset = terminal.HandoffRetryTests.reset
    handle = terminal.HandoffRetryTests.handle

    async def test_terminal_preset_cannot_bypass_shared_manual_interlock(self):
        self.controls.manual_head_entities = frozenset({self.other})
        await handoff.step(self.controls)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.plan["requests"][self.head].get("dispatched"))
        self.assertIn("blocks shared activation", self.plan["problems"][self.head])
