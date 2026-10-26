"""Adopting the house policy keeps genuine holds without renewing expired intent."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.house_climate.comfort import RoomComfort, async_migrate_house_holds
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.house_policy import house_configuration
from custom_components.house_climate.controller.bindings import example_bindings


NOW = datetime(2026, 10, 26, 5, 15, tzinfo=timezone.utc)  # 10:15pm Pacific


class HouseHoldMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace(config=SimpleNamespace(time_zone="America/Los_Angeles"))
        self.bindings = example_bindings()
        raw = house_configuration(bindings=self.bindings).to_dict()
        raw["rooms"]["primary"]["sensors"].append({
            "entity_id": "sensor.sleep_equipment_temperature", "source": "old_head"})
        raw["rooms"]["primary"]["schedules"][0]["end"] = "07:00:00"
        raw["rooms"]["primary"]["schedules"][1]["start"] = "07:00:00"
        raw["rooms"]["guest"]["profile"]["target_f"] = 71.5
        self.previous = Config.from_dict(raw)
        self.fixed = house_configuration(raw, self.bindings)
        self.store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
        self.enterContext(patch("custom_components.house_climate.comfort.Store", return_value=self.store))
        self.old = RoomComfort(self.hass, self.previous, "existing_entry", lambda: None)
        self.new = RoomComfort(self.hass, self.fixed, "existing_entry", lambda: None)

    def seed(self, room="primary", low=67.5, high=69.5, target=68.5):
        hold = self.old._new_hold(room, low, high, target, NOW)
        self.store.async_load.return_value = self.old._data({room: hold})
        return deepcopy(self.store.async_load.return_value)

    async def migrate(self):
        await async_migrate_house_holds(self.hass, "existing_entry", self.previous, self.fixed, now=NOW)
        return self.store.async_save.call_args.args[0]

    async def test_valid_hold_survives_probe_change_and_expires_at_new_schedule_boundary(self):
        original = self.seed()
        saved = await self.migrate()
        value = saved["rooms"]["primary"]
        self.assertEqual((value["low_f"], value["high_f"], value["target_f"]), (67.5, 69.5, 68.5))
        self.assertEqual(value["fingerprint"], self.new._fingerprint("primary"))
        self.assertNotEqual(value["fingerprint"], original["rooms"]["primary"]["fingerprint"])
        self.assertEqual(value["expires_at"], "2026-10-26T12:00:00+00:00")
        self.assertEqual(self.store.async_load.return_value, original)

    async def test_existing_earlier_expiration_is_never_extended(self):
        self.seed()
        expiry = NOW + timedelta(minutes=10)
        self.store.async_load.return_value["rooms"]["primary"]["expires_at"] = expiry.isoformat()
        value = (await self.migrate())["rooms"]["primary"]
        self.assertEqual(value["expires_at"], expiry.isoformat())

    async def test_a_room_without_schedule_retains_its_nonexpiring_manual_hold(self):
        self.seed(room="guest", low=70, high=73, target=72)
        saved = await self.migrate()
        self.assertIsNone(saved["rooms"]["guest"]["expires_at"])
        self.assertEqual(saved["rooms"]["guest"]["target_f"], 72)

    async def test_expired_corrupt_and_out_of_bounds_holds_are_not_revived(self):
        for bad in ("expired", "fingerprint", "unsafe", "nan"):
            with self.subTest(bad=bad):
                self.seed()
                value = self.store.async_load.return_value["rooms"]["primary"]
                if bad == "expired":
                    value["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
                elif bad == "fingerprint":
                    value["fingerprint"] = "unrelated-configuration"
                elif bad == "unsafe":
                    value.update(high_f=80, target_f=78)
                else:
                    value["target_f"] = float("nan")
                saved = await self.migrate()
                self.assertEqual(saved, {"rooms": {}})

    async def test_repeating_migration_does_not_renew_or_rewrite_a_migrated_hold(self):
        self.seed()
        saved = await self.migrate()
        self.store.async_save.reset_mock()
        self.store.async_load.return_value = deepcopy(saved)
        await async_migrate_house_holds(self.hass, "existing_entry", self.previous, self.fixed,
                                        now=NOW + timedelta(minutes=1))
        self.store.async_save.assert_not_awaited()

    async def test_failed_storage_write_is_reported_before_entry_can_be_migrated(self):
        self.seed()
        self.store.async_save.side_effect = OSError("storage unavailable")
        with self.assertRaisesRegex(OSError, "storage unavailable"):
            await async_migrate_house_holds(self.hass, "existing_entry", self.previous, self.fixed, now=NOW)


if __name__ == "__main__":
    unittest.main()
