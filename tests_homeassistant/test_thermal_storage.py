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

    async def test_backup_pauses_only_writer_and_restores_standalone_file(self):
        import shutil
        from custom_components.house_climate import backup
        from custom_components.house_climate.const import DOMAIN
        from types import SimpleNamespace
        self.hass.data[DOMAIN]=SimpleNamespace(thermal_storage=self.storage)
        self.storage.start()
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00+00:00","rooms":{}})
        for _ in range(100):
            if self.storage.diagnostics["committed_records"]: break
            await asyncio.sleep(.01)
        self.assertGreater(Path(str(self.storage.path)+"-wal").stat().st_size, 0)
        await backup.async_pre_backup(self.hass)
        self.assertTrue(self.storage.diagnostics["backup_paused"])
        snapshot=Path(self.temp.name)/"restored.sqlite"
        await self.hass.async_add_executor_job(shutil.copy2,self.storage.path,snapshot)
        self.storage.observe("shadow",{"timestamp":"2026-11-12T12:01:00+00:00","rooms":{}})
        await asyncio.sleep(.05)
        self.assertEqual(self.storage.diagnostics["committed_records"],1)
        restored=await self.hass.async_add_executor_job(read_history,snapshot,0,9999999999)
        self.assertEqual(len(restored["decisions"]),1)
        await backup.async_post_backup(self.hass)
        for _ in range(100):
            if self.storage.diagnostics["committed_records"]==2: break
            await asyncio.sleep(.01)
        self.assertEqual(self.storage.diagnostics["committed_records"],2)

    async def test_backup_fails_instead_of_claiming_consistent_dead_writer(self):
        from custom_components.house_climate import backup
        from custom_components.house_climate.const import DOMAIN
        from homeassistant.exceptions import HomeAssistantError
        from types import SimpleNamespace
        self.hass.data[DOMAIN]=SimpleNamespace(thermal_storage=self.storage)
        with self.assertRaises(HomeAssistantError):
            await backup.async_pre_backup(self.hass)

    async def test_cancelled_pre_backup_releases_pause(self):
        from unittest.mock import Mock
        self.storage._thread=Mock()
        self.storage._thread.is_alive.return_value=True
        with patch.object(self.hass, "async_add_executor_job", side_effect=[asyncio.CancelledError, asyncio.sleep(0, result=True)]):
            with self.assertRaises(asyncio.CancelledError):
                await self.storage.async_pre_backup()
        self.assertFalse(self.storage._backup_requested.is_set())
        self.storage._thread=None

    async def test_reload_during_backup_cannot_write_and_repeated_backup(self):
        from custom_components.house_climate import backup
        from custom_components.house_climate.const import DOMAIN
        from types import SimpleNamespace
        self.hass.data[DOMAIN]=SimpleNamespace(thermal_storage=self.storage)
        self.storage.start()
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00+00:00","rooms":{}})
        for _ in range(100):
            if self.storage.diagnostics["committed_records"]: break
            await asyncio.sleep(.01)
        await backup.async_pre_backup(self.hass)
        await backup.async_post_backup(self.hass)
        # Immediate second backup must obtain a fresh acknowledgement.
        await backup.async_pre_backup(self.hass)
        await self.storage.async_close()
        replacement=ThermalStorage(self.hass,config(),"entry")
        self.hass.data[DOMAIN]=SimpleNamespace(thermal_storage=replacement)
        replacement.start()
        replacement.observe("live",{"timestamp":"2026-11-12T12:01:00+00:00","rooms":{}})
        try:
            await asyncio.sleep(.25)
            self.assertEqual(replacement.diagnostics["committed_records"],0)
            await backup.async_post_backup(self.hass)
            for _ in range(100):
                if replacement.diagnostics["committed_records"]: break
                await asyncio.sleep(.01)
            self.assertEqual(replacement.diagnostics["committed_records"],1)
        finally:
            await replacement.async_close()

    async def test_prepare_timeout_releases_fence(self):
        from unittest.mock import AsyncMock, Mock
        from custom_components.house_climate import backup
        from custom_components.house_climate.const import DOMAIN
        from custom_components.house_climate.thermal_storage import BACKUP_FENCE
        from homeassistant.exceptions import HomeAssistantError
        from types import SimpleNamespace
        self.storage._thread=Mock()
        self.storage._thread.is_alive.return_value=True
        self.hass.data[DOMAIN]=SimpleNamespace(thermal_storage=self.storage)
        with patch.object(self.hass,"async_add_executor_job",AsyncMock(return_value=False)):
            with self.assertRaises(HomeAssistantError):
                await backup.async_pre_backup(self.hass)
        self.assertFalse(self.hass.data[BACKUP_FENCE].is_set())
        self.assertFalse(self.storage._backup_requested.is_set())
        self.storage._thread=None

    async def test_failed_checkpoint_cannot_acknowledge_backup(self):
        from unittest.mock import Mock
        store=Mock()
        store.db.execute.side_effect=OSError("busy")
        self.storage._backup_requested.clear()
        self.storage._pause_for_backup(store)
        self.assertEqual(self.storage._backup_error,"OSError")
        store.close.assert_not_called()

    async def test_cancel_after_ready_then_new_generation_is_safe(self):
        self.storage.start()
        self.storage.observe("live",{"timestamp":"2026-11-12T12:00:00+00:00","rooms":{}})
        for _ in range(100):
            if self.storage.diagnostics["committed_records"]: break
            await asyncio.sleep(.01)
        original=self.hass.async_add_executor_job
        calls=0
        def interrupted(function,*args):
            nonlocal calls
            calls+=1
            if calls==1:
                async def wait_then_cancel():
                    await original(function,*args)
                    raise asyncio.CancelledError
                return wait_then_cancel()
            return original(function,*args)
        with patch.object(self.hass,"async_add_executor_job",interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.storage.async_pre_backup()
        await self.storage.async_pre_backup()
        self.assertEqual(self.storage._backup_ready_generation,2)
        self.assertTrue(self.storage.diagnostics["backup_paused"])
        await self.storage.async_post_backup()
