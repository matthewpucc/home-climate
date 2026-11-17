"""Unmodified HA 2026.11 aggregation cannot strand the snapshot writer."""
from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock

from homeassistant.components.backup.manager import BackupManager
from homeassistant.components.backup.models import BackupManagerError
from custom_components.house_climate import backup
from custom_components.house_climate.const import DOMAIN
from tests_homeassistant import test_thermal_storage as fixtures


class BackupManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StorageTests.asyncSetUp
    asyncTearDown = fixtures.StorageTests.asyncTearDown
    ready = fixtures.BackupTests.ready
    record = fixtures.BackupTests.record

    async def test_failed_peer_cannot_poison_retry_or_stop_recording(self):
        await self.ready()
        self.hass.data[DOMAIN] = SimpleNamespace(thermal_storage=self.storage)
        other = SimpleNamespace(async_pre_backup=AsyncMock(
            side_effect=RuntimeError("synthetic peer preparation failed")),
            async_post_backup=AsyncMock())
        manager = SimpleNamespace(hass=self.hass, platforms={DOMAIN:backup, "synthetic":other})
        with self.assertRaisesRegex(BackupManagerError, "synthetic peer"):
            await BackupManager.async_pre_backup_actions(manager)
        other.async_post_backup.assert_not_called()
        await self.record("after failed aggregate")
        other.async_pre_backup.side_effect = None
        await BackupManager.async_pre_backup_actions(manager)
        await self.record("while archive can still run")
        await BackupManager.async_pre_backup_actions(manager)
        # Late/duplicate posts change neither recording nor authority.
        await BackupManager.async_post_backup_actions(manager)
        await BackupManager.async_post_backup_actions(manager)
        await self.record("after duplicate post")
        self.assertFalse(self.storage.diagnostics["backup_paused"])

    async def test_stock_archive_and_restore_discards_newer_cache(self):
        """Real securetar + installed stock Core exclusions/restore clearing.

        Supervisor 2026.11.0 uses the same .cache/* filter and clears config,
        preserving only recorder files for exclude_database partial restores.
        Small envelope keeps this regression cheap; production size benchmark is
        documented separately, and fixed-size stat/open races are unit-tested.
        """
        from pathlib import Path
        import shutil
        import tarfile
        import tempfile
        from unittest.mock import patch
        from securetar import atomic_contents_add
        from homeassistant.backup_restore import _clear_configuration_directory, KEEP_DATABASE
        from homeassistant.components.backup.const import EXCLUDE_FROM_BACKUP
        from custom_components.house_climate.controller import thermal_snapshot
        from custom_components.house_climate.controller.thermal_history import read_history
        from custom_components.house_climate.thermal_storage import ThermalStorage
        from tests_homeassistant.test_thermal_storage import config
        with patch.object(thermal_snapshot, "ENVELOPE_BYTES", 4*1024*1024):
            await self.ready()
            await self.record("archive generation")
            await self.storage.async_pre_backup()
            with tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp)/"homeassistant.tar"
                def archive():
                    with tarfile.open(archive_path, "w:gz") as tar:
                        atomic_contents_add(tar, Path(self.temp.name),
                            lambda p:any(p.full_match(f"data/{pattern}") for pattern in EXCLUDE_FROM_BACKUP),
                            arcname="data")
                await self.hass.async_add_executor_job(archive)
                with tarfile.open(archive_path) as tar:
                    names = tar.getnames()
                    self.assertIn("data/.storage/house_climate_thermal.snapshot", names)
                    self.assertFalse(any(n.startswith("data/.cache/") for n in names))
                await self.record("newer working history, must not survive restore")
                await self.storage.async_close()
                root = Path(self.temp.name)
                recorder = root/"home-assistant_v2.db"
                recorder.write_bytes(b"preserve recorder only")
                # Exercise exclude-database restore with pre-existing newer cache.
                _clear_configuration_directory(root, KEEP_DATABASE)
                self.assertTrue(recorder.exists())
                self.assertFalse(self.storage.path.exists())
                with tarfile.open(archive_path) as tar:
                    tar.extractall(Path(tmp)/"unpacked", filter="data")
                shutil.copytree(Path(tmp)/"unpacked/data", root, dirs_exist_ok=True)
                self.storage = ThermalStorage(self.hass, config(), "entry")
                await self.ready()
                result = await self.hass.async_add_executor_job(read_history, self.storage.path, 0, 9999999999)
                self.assertEqual(len(result["decisions"]), 1)
                self.assertEqual(json.loads(result["decisions"][0]["payload"])["mode"], "archive generation")
