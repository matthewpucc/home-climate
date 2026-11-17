"""Snapshot correctness without any HA, network, or equipment dependency."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from house_climate import thermal_snapshot as snap
from house_climate.thermal_history import ThermalHistory


class ThermalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root/'.cache'
        self.cache.mkdir()
        self.path = self.cache/'history.sqlite'
        self.artifact = self.root/'.storage'/'thermal.snapshot'
        self.store = ThermalHistory(self.path)
        self.store.open()
        self.addCleanup(self.store.close)
        self.patch = patch.object(snap, 'ENVELOPE_BYTES', 1024*1024)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.append(100)

    def append(self, at):
        self.store.append('entry','session',at,{'rooms':{}},{'rooms':{},'event':'live'})

    def publish(self, **kwargs):
        return snap.publish(self.path,self.artifact,self.cache,**kwargs)

    def restore(self, name='restored.sqlite', **kwargs):
        dest = self.cache/name
        info = snap.restore(self.artifact,dest,**kwargs)
        return info,dest

    def rows(self,path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT at FROM decisions ORDER BY at').fetchall()

    def rewrite_header(self, transform):
        with self.artifact.open('r+b') as f:
            prefix=f.read(12); length=struct.unpack('>I',prefix[8:])[0]
            meta=json.loads(f.read(length)); transform(meta)
            data=json.dumps(meta,separators=(',',':')).encode()
            f.seek(0); f.write(snap.MAGIC+struct.pack('>I',len(data))+data)

    def test_online_roundtrip_includes_uncheckpointed_wal(self):
        self.assertTrue(Path(str(self.path)+'-wal').exists())
        info=self.publish()
        recovered,path=self.restore()
        self.assertEqual(recovered,info)
        self.assertEqual(info.last_commit,100)
        self.assertEqual(self.rows(path),[(100.0,)])
        self.append(110)
        self.assertEqual(self.rows(self.path),[(100.0,),(110.0,)])
        self.assertEqual(self.rows(path),[(100.0,)])
        self.assertEqual(self.artifact.stat().st_size,snap.ENVELOPE_BYTES)
        self.assertEqual(self.artifact.stat().st_mode & 0o777,0o600)

    def test_legacy_copy_includes_wal_and_leaves_source(self):
        dest=self.cache/'migrated.sqlite'
        snap.copy_online(self.path,dest)
        self.assertEqual(self.rows(dest),[(100.0,)])
        self.append(110)
        self.assertEqual(self.rows(self.path),[(100.0,),(110.0,)])
        self.assertEqual(self.rows(dest),[(100.0,)])

    def test_existing_database_and_sidecars_never_overwritten(self):
        self.publish()
        with self.assertRaises(FileExistsError):snap.restore(self.artifact,self.path)
        self.assertEqual(self.rows(self.path),[(100.0,)])
        for suffix in ('-wal','-shm'):
            dest=self.cache/f'blocked{suffix}.sqlite'
            Path(str(dest)+suffix).write_bytes(b'keep')
            with self.assertRaises(FileExistsError):snap.restore(self.artifact,dest)
            self.assertFalse(dest.exists())

    def test_stopped_publish_preserves_previous_artifact(self):
        self.publish(); before=self.artifact.read_bytes()
        stop=threading.Event();stop.set()
        with self.assertRaises(InterruptedError):self.publish(stop=stop)
        self.assertEqual(before,self.artifact.read_bytes())

    def test_stopped_restore_never_creates_destination(self):
        self.publish(); stop=threading.Event();stop.set()
        with self.assertRaises(InterruptedError):self.restore(stop=stop)
        self.assertFalse((self.cache/'restored.sqlite').exists())

    def test_deadline_during_backup_preserves_artifact_and_source(self):
        self.publish();before=self.artifact.read_bytes()
        with patch.object(snap.time,'monotonic',side_effect=[0,0,2]):
            with self.assertRaises(TimeoutError):self.publish(timeout=1)
        self.assertEqual(before,self.artifact.read_bytes())
        self.assertEqual(self.rows(self.path),[(100.0,)])

    def test_capacity_cannot_change_in_place(self):
        self.publish();before=self.artifact.read_bytes()
        with patch.object(snap,'ENVELOPE_BYTES',2*1024*1024):
            with self.assertRaises(snap.SnapshotError):self.publish()
        self.assertEqual(before,self.artifact.read_bytes())

    def test_overflow_keeps_previous_generation(self):
        self.publish();before=self.artifact.read_bytes()
        with patch.object(snap,'MAX_DATABASE_BYTES',1):
            with self.assertRaises(snap.SnapshotError):self.publish()
        self.assertEqual(before,self.artifact.read_bytes())

    def test_low_space_keeps_prior_artifact(self):
        self.publish();before=self.artifact.read_bytes()
        with patch.object(snap.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaises(OSError):self.publish()
        self.assertEqual(before,self.artifact.read_bytes())

    def test_low_restore_space_keeps_artifact_and_creates_no_database(self):
        self.publish();before=self.artifact.read_bytes()
        with patch.object(snap.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaises(OSError):self.restore()
        self.assertEqual(before,self.artifact.read_bytes())
        self.assertFalse((self.cache/'restored.sqlite').exists())
        self.assertFalse(list(self.cache.glob('.thermal-restore-*')))

    def test_failed_replace_keeps_previous_generation(self):
        self.publish();before=self.artifact.read_bytes(); self.append(110)
        with patch.object(snap.os,'replace',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.publish()
        self.assertEqual(before,self.artifact.read_bytes())
        self.assertFalse(list(self.cache.glob('.thermal-snapshot-*')))

    def test_open_old_inode_is_immutable_during_republication(self):
        self.publish()
        with self.artifact.open('rb') as f:
            before=f.read();self.append(110);self.publish();f.seek(0)
            self.assertEqual(f.read(),before)
        _,path=self.restore()
        self.assertEqual(self.rows(path),[(100.0,),(110.0,)])

    def test_checksum_rejects_corruption(self):
        self.publish()
        self.rewrite_header(lambda m:m.update(raw_sha256='0'*64))
        with self.assertRaises(snap.SnapshotError):self.restore()
        self.assertFalse((self.cache/'restored.sqlite').exists())

    def test_header_lengths_and_version_rejected(self):
        for changes in ({'raw_bytes':snap.MAX_DATABASE_BYTES+1},{'compressed_bytes':snap.ENVELOPE_BYTES},
                        {'version':2},{'generation':'invalid'},{'created_at':float('nan')}):
            with self.subTest(changes=changes):
                self.publish();self.rewrite_header(lambda m:m.update(changes))
                with self.assertRaises(snap.SnapshotError):self.restore()

    def test_truncation_rejected(self):
        self.publish()
        with self.artifact.open('r+b') as f:f.truncate(snap.ENVELOPE_BYTES-1)
        with self.assertRaises(snap.SnapshotError):self.restore()

    def test_valid_checksum_is_not_enough_for_sqlite_integrity(self):
        self.publish()
        payload=b'not sqlite';compressed=zlib.compress(payload)
        self.rewrite_header(lambda m:m.update(raw_bytes=len(payload),compressed_bytes=len(compressed),
                            raw_sha256=hashlib.sha256(payload).hexdigest(),
                            compressed_sha256=hashlib.sha256(compressed).hexdigest()))
        with self.artifact.open('r+b') as f:f.seek(snap.HEADER_BYTES);f.write(compressed)
        with self.assertRaises(sqlite3.DatabaseError):self.restore()
        self.assertFalse((self.cache/'restored.sqlite').exists())

    def test_decompression_cannot_exceed_declared_bound(self):
        self.publish();self.rewrite_header(lambda m:m.update(raw_bytes=1))
        with self.assertRaises(snap.SnapshotError):self.restore()

    def test_unknown_database_schema_preserved(self):
        self.publish();before=self.artifact.read_bytes()
        self.store.db.execute('PRAGMA user_version=999')
        with self.assertRaises(snap.SnapshotError):self.publish()
        self.assertEqual(before,self.artifact.read_bytes())
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],999)

    def test_securetar_stat_replace_open_keeps_complete_new_snapshot(self):
        try:
            from securetar import atomic_contents_add
        except ImportError:
            self.skipTest('securetar available in Home Assistant test environment')
        self.publish(); self.append(110)
        second=self.root/'next.snapshot';snap.publish(self.path,second,self.cache)
        tar=io.BytesIO();replaced=[]
        with tarfile.open(fileobj=tar,mode='w') as archive:
            original=archive.gettarinfo
            def swap_after_stat(name=None,arcname=None,fileobj=None):
                info=original(name,arcname,fileobj)
                if Path(name)==self.artifact and not replaced:
                    os.replace(second,self.artifact);replaced.append(True)
                return info
            archive.gettarinfo=swap_after_stat
            atomic_contents_add(archive,self.artifact.parent,lambda _:False,arcname='data/.storage')
        tar.seek(0)
        with tarfile.open(fileobj=tar,mode='r') as archive:
            blob=archive.extractfile('data/.storage/thermal.snapshot').read()
        self.assertEqual(len(blob),snap.ENVELOPE_BYTES)
        extracted=self.root/'extracted.snapshot';extracted.write_bytes(blob)
        dest=self.cache/'archive-restored.sqlite';snap.restore(extracted,dest)
        self.assertEqual(self.rows(dest),[(100.0,),(110.0,)])

    def test_stock_excludes_keep_snapshot_not_working_files(self):
        self.publish()
        try:
            from securetar import atomic_contents_add
        except ImportError:
            self.skipTest('securetar available in Home Assistant test environment')
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w') as archive:
            atomic_contents_add(archive,self.root,lambda p:p.full_match('data/.cache/*'),arcname='data')
        out.seek(0)
        with tarfile.open(fileobj=out,mode='r') as archive:
            names=archive.getnames()
        self.assertIn('data/.storage/thermal.snapshot',names)
        self.assertFalse(any(name.startswith('data/.cache/') for name in names))
