import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from custom_components.house_climate.thermal_storage import ThermalStorage
from custom_components.house_climate.controller.config import Config
from custom_components.house_climate.controller.thermal_history import read_history


def config():
    return Config.from_dict({"schema_version":2,"timezone":"Etc/UTC","heads":{"head":"climate.test"},
        "rooms":{"room":{"head":"head","sensors":[{"entity_id":"sensor.test","source":"probe"}],"profile":{"target_f":70}}}})


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.hass=HomeAssistant(self.temp.name)
        self.storage=ThermalStorage(self.hass,config(),"entry")

    async def asyncTearDown(self):
        await self.storage.async_close()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_persists_off_loop_and_closes(self):
        self.storage.start()
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00+00:00","rooms":{},"private_token":"not_saved"})
        for _ in range(100):
            if self.storage.diagnostics["committed_records"]: break
            await asyncio.sleep(.01)
        await self.storage.async_close()
        result=await self.hass.async_add_executor_job(read_history,self.storage.path,0,9999999999)
        self.assertEqual(len(result["episodes"]),1)
        self.assertEqual(self.storage.diagnostics["committed_records"],1)
        self.assertNotIn("not_saved",Path(self.storage.path).read_bytes().decode(errors="ignore"))

    async def test_full_queue_is_nonblocking_and_counted(self):
        for index in range(140): self.storage.observe("live",{"mode":str(index)})
        self.assertEqual(self.storage.diagnostics["queue_depth"],128)
        self.assertEqual(self.storage.diagnostics["dropped_records"],12)

    async def test_open_failure_isolated(self):
        with patch("custom_components.house_climate.thermal_storage.ThermalHistory.open",side_effect=OSError):
            self.storage.start()
            await self.hass.async_add_executor_job(self.storage._thread.join, 2)
            await self.storage.async_close()
        self.assertEqual(self.storage.diagnostics["error"],"OSError")

    async def test_frozen_payload_and_naive_time_rejected(self):
        data={"rooms":{"room":{"temperature":70}}}
        self.storage.observe("live",data)
        data["rooms"]["room"]["temperature"]=99
        self.assertEqual(self.storage._queue.get()[1]["rooms"]["room"]["temperature"],70)
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00"})
        self.assertEqual(self.storage.diagnostics["dropped_records"],1)

    async def test_failed_writer_rejects_instead_of_queueing(self):
        with patch("custom_components.house_climate.thermal_storage.ThermalHistory.open",side_effect=OSError):
            self.storage.start()
            await self.hass.async_add_executor_job(self.storage._thread.join,2)
        self.storage.observe("live",{})
        self.assertEqual(self.storage.diagnostics["queue_depth"],0)
        self.assertEqual(self.storage.diagnostics["dropped_records"],1)

    async def test_shutdown_join_is_bounded_and_queue_is_discarded(self):
        from unittest.mock import Mock
        self.storage._thread=Mock()
        self.storage._thread.is_alive.return_value=True
        self.storage.observe("live",{})
        await self.storage.async_close()
        self.storage._thread.join.assert_called_once_with(2)
        self.assertEqual(self.storage.diagnostics["dropped_records"],1)
        self.assertEqual(self.storage.diagnostics["error"],"shutdown_timeout")

    async def test_unchanged_maintenance_snapshots_do_not_bloat_db(self):
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00+00:00","mode":"cool"})
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:15+00:00","mode":"cool"})
        self.storage.observe("live",{"timestamp":"2026-11-12T12:01:30+00:00","mode":"cool"})
        self.assertEqual(self.storage.diagnostics["queue_depth"],2)
        self.assertEqual(self.storage.diagnostics["dropped_records"],0)

    async def test_read_only_query_does_not_create_missing_database(self):
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            await self.hass.async_add_executor_job(read_history,self.storage.path,0,10)
        self.assertFalse(self.storage.path.exists())


from tests_homeassistant import test_relay_commissioning_service as service_fixture


class QueryServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = service_fixture.RelayCommissioningServiceTests.asyncSetUp
    asyncTearDown = service_fixture.RelayCommissioningServiceTests.asyncTearDown

    async def test_query_registered_readonly_and_missing_storage_fails(self):
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.house_climate.const import DOMAIN
        self.assertTrue(self.hass.services.has_service(DOMAIN,"thermal_history_query"))
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call(DOMAIN,"thermal_history_query",{"start":0,"end":100},blocking=True,return_response=True)
        self.guard.async_commission_relay.assert_not_awaited()

    async def test_query_real_db_response_invalid_window_and_stale_runtime(self):
        from types import SimpleNamespace
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.house_climate.const import DOMAIN
        from custom_components.house_climate.controller.thermal_history import ThermalHistory
        path=Path(self.directory.name)/"evidence.sqlite"
        def create():
            store=ThermalHistory(path)
            store.open()
            store.close()
        await self.hass.async_add_executor_job(create)
        self.runtime.thermal_storage=SimpleNamespace(path=path)
        result=await self.hass.services.async_call(DOMAIN,"thermal_history_query",{"start":0,"end":100},blocking=True,return_response=True)
        self.assertEqual(result["observations"],[])
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call(DOMAIN,"thermal_history_query",{"start":100,"end":0},blocking=True,return_response=True)
        self.hass.data[DOMAIN]=object()
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call(DOMAIN,"thermal_history_query",{"start":0,"end":100},blocking=True,return_response=True)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = StorageTests.asyncSetUp
    asyncTearDown = StorageTests.asyncTearDown

    async def ready(self):
        self.storage.start()
        await self.hass.async_add_executor_job(self.storage._storage_ready.wait, 10)
        self.assertFalse(self.storage._failed, self.storage.diagnostics)

    async def record(self, mode):
        before = self.storage._committed
        self.storage.observe("live", {"mode":mode})
        for _ in range(200):
            if self.storage._committed > before:
                return
            await asyncio.sleep(.01)
        self.fail(str(self.storage.diagnostics))

    async def test_snapshot_restores_and_missing_post_does_not_pause(self):
        from custom_components.house_climate.controller import thermal_snapshot
        await self.ready()
        await self.record("heat")
        await self.storage.async_pre_backup()
        snapshot_commit = self.storage._snapshot_info.last_commit
        await self.record("cool")  # deliberately never call post
        restored = Path(self.temp.name) / "restored.sqlite"
        await self.hass.async_add_executor_job(thermal_snapshot.restore, self.storage.snapshot_path, restored)
        result = await self.hass.async_add_executor_job(read_history, restored, 0, 9999999999)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertIsNotNone(snapshot_commit)
        await self.storage.async_pre_backup()  # no stale latch on retry
        self.assertFalse(self.storage.diagnostics["backup_paused"])

    async def test_dead_writer_rejects(self):
        with self.assertRaises(RuntimeError):
            await self.storage.async_pre_backup()

    async def test_refresh_failure_preserves_snapshot_and_live_recording(self):
        await self.ready()
        old = self.storage.snapshot_path.stat()
        with patch("custom_components.house_climate.thermal_storage.thermal_snapshot.publish", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                await self.storage.async_pre_backup()
        self.assertEqual(self.storage.snapshot_path.stat().st_ino, old.st_ino)
        await self.record("heat")
        await self.storage.async_pre_backup()
        self.assertIsNone(self.storage.diagnostics["snapshot_error"])

    async def test_reload_keeps_fresh_cache_without_post(self):
        await self.ready()
        await self.storage.async_pre_backup()
        await self.record("heat")
        await self.storage.async_close()
        replacement = ThermalStorage(self.hass, config(), "entry")
        self.storage = replacement
        await self.ready()
        result = await self.hass.async_add_executor_job(read_history, replacement.path, 0, 9999999999)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertFalse(replacement.diagnostics["recovered_from_snapshot"])

    async def test_restore_with_absent_cache_uses_snapshot(self):
        import shutil
        await self.ready()
        await self.record("heat")
        await self.storage.async_pre_backup()
        await self.record("cool")
        await self.storage.async_close()
        shutil.rmtree(self.storage.path.parent)  # stock restore removes cache
        self.storage = ThermalStorage(self.hass, config(), "entry")
        await self.ready()
        result = await self.hass.async_add_executor_job(read_history, self.storage.path, 0, 9999999999)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertTrue(self.storage.diagnostics["recovered_from_snapshot"])

    async def test_legacy_migration_keeps_committed_wal_and_rollback_copy(self):
        from custom_components.house_climate.controller.thermal_history import ThermalHistory
        legacy = ThermalHistory(self.storage.legacy_path)
        legacy.open()
        legacy.append("entry", "legacy", 10, {"rooms":{}}, {"mode":"heat"})
        # Keep source connection open to prove online backup includes its WAL.
        await self.ready()
        legacy.close()
        self.assertFalse(self.storage.legacy_path.exists())
        self.assertTrue((self.storage.path.parent / "house_climate_thermal.legacy").exists())
        result = await self.hass.async_add_executor_job(read_history, self.storage.path, 0, 100)
        self.assertEqual(len(result["decisions"]), 1)

    async def test_migration_publish_failure_preserves_legacy(self):
        from custom_components.house_climate.controller.thermal_history import ThermalHistory
        legacy = ThermalHistory(self.storage.legacy_path)
        legacy.open()
        legacy.append("entry", "legacy", 10, {"rooms":{}}, {"mode":"heat"})
        legacy.close()
        with patch("custom_components.house_climate.thermal_storage.thermal_snapshot.publish", side_effect=OSError("full")):
            self.storage.start()
            await self.hass.async_add_executor_job(self.storage._thread.join, 10)
        self.assertTrue(self.storage._failed)
        self.assertTrue(self.storage.legacy_path.exists())
        self.assertFalse(self.storage.path.exists())

    async def test_corrupt_restored_snapshot_never_initializes_empty_history(self):
        self.storage.snapshot_path.parent.mkdir(parents=True)
        self.storage.snapshot_path.write_bytes(b"invalid")
        self.storage.start()
        await self.hass.async_add_executor_job(self.storage._thread.join, 10)
        self.assertTrue(self.storage._failed)
        self.assertFalse(self.storage.path.exists())
        self.assertEqual(self.storage.snapshot_path.read_bytes(), b"invalid")

    async def test_cancelled_pre_refresh_does_not_freeze_writer(self):
        await self.ready()
        entered, release = threading.Event(), threading.Event()
        original = self.storage._publish_snapshot
        def slow():
            entered.set()
            release.wait(5)
            original()
        with patch.object(self.storage, "_publish_snapshot", slow):
            task = asyncio.create_task(self.storage.async_pre_backup())
            await self.hass.async_add_executor_job(entered.wait, 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.record("heat")
            release.set()
            # Next pre waits for the detached executor to release only its
            # snapshot lock, never a writer/control lease.
            await self.storage.async_pre_backup()
        self.assertFalse(self.storage.diagnostics["backup_paused"])

    async def test_restart_snapshot_failure_keeps_valid_working_history_recording(self):
        await self.ready()
        await self.record("heat")
        await self.storage.async_close()
        self.storage = ThermalStorage(self.hass, config(), "entry")
        with patch("custom_components.house_climate.thermal_storage.thermal_snapshot.publish", side_effect=OSError("full")):
            await self.ready()
            await self.record("cool")
        self.assertEqual(self.storage.diagnostics["snapshot_error"], "OSError")
        result = await self.hass.async_add_executor_job(read_history, self.storage.path, 0, 9999999999)
        self.assertEqual(len(result["decisions"]), 2)

    async def test_orphan_working_wal_never_initializes_empty_history(self):
        self.storage.path.parent.mkdir(parents=True)
        wal = Path(str(self.storage.path)+"-wal")
        wal.write_bytes(b"preserve orphan recovery evidence")
        self.storage.start()
        await self.hass.async_add_executor_job(self.storage._thread.join, 10)
        self.assertTrue(self.storage._failed)
        self.assertFalse(self.storage.path.exists())
        self.assertEqual(wal.read_bytes(), b"preserve orphan recovery evidence")
