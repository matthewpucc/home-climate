"""Immutable fixed-size backup envelopes for the private thermal SQLite store.

Stock HA archives stat a pathname before opening it. Every published inode must
therefore have the SAME length, even when a concurrent publication replaces it.
Only staging files are mutable. This module never pauses the live writer.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import shutil
import struct
import tempfile
import time
import uuid
import zlib

from .thermal_history import SCHEMA, Retention

# Never change this capacity in place: use a new artifact name/format instead.
ENVELOPE_BYTES = 1024 ** 3 + 1024 ** 2
MAX_DATABASE_BYTES = 1024 ** 3
HEADER_BYTES = 4096
CHUNK_BYTES = 1024 ** 2
MAGIC = b'HCTHSNP1'
TABLES = {'regimes', 'decisions', 'observations', 'summaries', 'episodes', 'metadata'}


@dataclass(frozen=True)
class SnapshotInfo:
    generation: str
    created_at: float
    raw_bytes: int
    compressed_bytes: int
    last_commit: float | None


class SnapshotError(ValueError):
    """An artifact cannot be safely published or restored."""


def _check(stop, deadline):
    if stop is not None and stop.is_set():
        raise InterruptedError('Thermal snapshot stopped')
    if time.monotonic() >= deadline:
        raise TimeoutError('Thermal snapshot deadline exceeded')


def _deadline(timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Snapshot timeout must be positive and finite')
    return time.monotonic() + timeout


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)


def _progress(db, stop, deadline):
    db.set_progress_handler(lambda: int((stop is not None and stop.is_set())
                                        or time.monotonic() >= deadline), 1000)


def _validate_database(path, stop, deadline):
    _check(stop, deadline)
    with closing(_readonly(path)) as db:
        _progress(db, stop, deadline)
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
            raise SnapshotError('Unsupported thermal history schema')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES <= tables:
            raise SnapshotError('Thermal history tables are missing')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise SnapshotError('Thermal snapshot SQLite integrity check failed')
        row = db.execute("SELECT MAX(at) FROM decisions").fetchone()
        last = float(row[0]) if row[0] is not None else None
        if last is not None and (not math.isfinite(last) or last < 0):
            raise SnapshotError('Invalid thermal history last commit')
    _check(stop, deadline)
    return last


def validate_database(path, stop=None, *, timeout=30):
    """Read-only validation, including any legacy WAL; never create a database."""
    _validate_database(Path(path), stop, _deadline(timeout))


def _clone(source, target, stop, deadline):
    _check(stop, deadline)
    with closing(_readonly(source)) as src, closing(sqlite3.connect(target, timeout=.1)) as dst:
        _progress(dst, stop, deadline)
        pages = src.execute("PRAGMA page_count").fetchone()[0]
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        # Online backup and VACUUM may briefly need two full database copies.
        if shutil.disk_usage(target.parent).free < 2 * pages * page_size + Retention().min_free_bytes:
            raise OSError("Insufficient free space for a thermal snapshot")
        src.backup(dst, pages=128, sleep=.01,
                   progress=lambda *_: _check(stop, deadline))
        dst.execute('PRAGMA journal_mode=DELETE')
        dst.execute('PRAGMA trusted_schema=OFF')
        dst.execute('VACUUM')
    _check(stop, deadline)
    if target.stat().st_size > MAX_DATABASE_BYTES:
        raise SnapshotError('Thermal snapshot exceeds the uncompressed budget')
    return _validate_database(target, stop, deadline)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fresh_destination(path):
    if any(os.path.lexists(p) for p in (path, Path(str(path)+'-wal'), Path(str(path)+'-shm'))):
        raise FileExistsError('Refusing to overwrite an existing thermal database or sidecar')


def _install_database(staged, destination):
    # No replacement: a racing writer/startup must win rather than lose its data.
    _fresh_destination(destination)
    os.chmod(staged, 0o600)
    with staged.open('rb') as f:
        os.fsync(f.fileno())
    os.link(staged, destination)
    _fsync_dir(destination.parent)


def copy_online(source, dest, stop=None, *, timeout=30):
    """Migrate a live/legacy database including WAL without modifying its source."""
    dest = Path(dest)
    _fresh_destination(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = _deadline(timeout)
    with tempfile.TemporaryDirectory(prefix='.thermal-copy-', dir=dest.parent) as tmp:
        staged = Path(tmp)/'history.sqlite'
        _clone(Path(source), staged, stop, deadline)
        _check(stop, deadline)
        _install_database(staged, dest)


def publish(source, snapshot, staging_dir, stop=None, *, timeout=30):
    """Publish a compact, compressed, immutable SQLite snapshot atomically.

    The caller must serialize publications. staging_dir must be backup-excluded
    and on the same filesystem as snapshot. Failures before replacement preserve
    the prior artifact; directory-fsync failure may follow a valid replacement.
    """
    snapshot, staging_dir = Path(snapshot), Path(staging_dir)
    deadline = _deadline(timeout)
    _check(stop, deadline)
    if os.path.lexists(snapshot) and (snapshot.is_symlink() or snapshot.stat().st_size != ENVELOPE_BYTES):
        raise SnapshotError('Existing snapshot has a different fixed capacity or is a symlink')
    staging_dir.mkdir(parents=True, exist_ok=True)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.thermal-snapshot-', dir=staging_dir) as tmp:
        staged_db, envelope = Path(tmp)/'history.sqlite', Path(tmp)/'snapshot'
        last_commit = _clone(Path(source), staged_db, stop, deadline)
        raw_hash, compressed_hash = hashlib.sha256(), hashlib.sha256()
        compressor = zlib.compressobj(level=6)
        raw_size = compressed_size = 0
        with staged_db.open('rb') as src, envelope.open('w+b') as out:
            os.chmod(envelope, 0o600)
            out.seek(HEADER_BYTES)
            def write(data):
                nonlocal compressed_size
                compressed_size += len(data)
                if compressed_size > ENVELOPE_BYTES - HEADER_BYTES:
                    raise SnapshotError('Thermal snapshot exceeds fixed envelope capacity')
                compressed_hash.update(data)
                out.write(data)
            while chunk := src.read(CHUNK_BYTES):
                _check(stop, deadline)
                raw_size += len(chunk)
                raw_hash.update(chunk)
                write(compressor.compress(chunk))
            write(compressor.flush())
            info = SnapshotInfo(uuid.uuid4().hex, time.time(), raw_size, compressed_size, last_commit)
            metadata = {**info.__dict__, 'version':1, 'codec':'zlib', 'schema':SCHEMA,
                        'raw_sha256':raw_hash.hexdigest(), 'compressed_sha256':compressed_hash.hexdigest()}
            header = json.dumps(metadata, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            if len(header) > HEADER_BYTES - 12:
                raise SnapshotError('Snapshot header overflow')
            out.seek(0)
            out.write(MAGIC + struct.pack('>I', len(header)) + header)
            out.truncate(ENVELOPE_BYTES)  # Sparse padding; stable logical tar member size.
            out.flush()
            os.fsync(out.fileno())
        _check(stop, deadline)
        os.replace(envelope, snapshot)
        _fsync_dir(snapshot.parent)
        return info


def _read_header(stream):
    if os.fstat(stream.fileno()).st_size != ENVELOPE_BYTES:
        raise SnapshotError('Snapshot envelope has an unexpected fixed size')
    prefix = stream.read(12)
    if len(prefix) != 12 or prefix[:8] != MAGIC:
        raise SnapshotError('Unsupported thermal snapshot format')
    length = struct.unpack('>I', prefix[8:])[0]
    if not 0 < length <= HEADER_BYTES - 12:
        raise SnapshotError('Invalid snapshot header length')
    try:
        meta = json.loads(stream.read(length))
        if meta['version'] != 1 or meta['codec'] != 'zlib' or meta['schema'] != SCHEMA:
            raise SnapshotError('Unsupported snapshot version, codec or schema')
        for field, bound in (('raw_bytes', MAX_DATABASE_BYTES), ('compressed_bytes', ENVELOPE_BYTES-HEADER_BYTES)):
            if type(meta[field]) is not int or not 0 < meta[field] <= bound:
                raise SnapshotError('Invalid snapshot payload length')
        for field in ('raw_sha256', 'compressed_sha256'):
            if not isinstance(meta[field], str) or len(meta[field]) != 64:
                raise SnapshotError('Invalid snapshot digest')
            bytes.fromhex(meta[field])
        if not isinstance(meta['generation'], str) or uuid.UUID(meta['generation']).hex != meta['generation']:
            raise SnapshotError('Invalid snapshot generation')
        for field in ('created_at', 'last_commit'):
            value = meta[field]
            if field == 'last_commit' and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise SnapshotError('Invalid snapshot timestamp')
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotError('Invalid thermal snapshot metadata') from error
    stream.seek(HEADER_BYTES)
    return meta


def restore(snapshot, dest, stop=None, *, timeout=30):
    """Validate and extract one immutable inode; never overwrite surviving history."""
    snapshot, dest = Path(snapshot), Path(dest)
    _fresh_destination(dest)
    deadline = _deadline(timeout)
    _check(stop, deadline)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with snapshot.open('rb') as src, tempfile.TemporaryDirectory(prefix='.thermal-restore-', dir=dest.parent) as tmp:
        meta = _read_header(src)
        if shutil.disk_usage(dest.parent).free < meta['raw_bytes'] + Retention().min_free_bytes:
            raise OSError('Insufficient free space to restore thermal history')
        staged = Path(tmp)/'history.sqlite'
        raw_hash, compressed_hash = hashlib.sha256(), hashlib.sha256()
        decoder = zlib.decompressobj()
        left, raw_size = meta['compressed_bytes'], 0
        with staged.open('wb') as out:
            os.chmod(staged, 0o600)
            while left:
                _check(stop, deadline)
                chunk = src.read(min(CHUNK_BYTES, left))
                if not chunk:
                    raise SnapshotError('Truncated compressed payload')
                left -= len(chunk)
                compressed_hash.update(chunk)
                while chunk:
                    _check(stop, deadline)
                    decoded = decoder.decompress(chunk, min(CHUNK_BYTES, meta['raw_bytes']-raw_size+1))
                    raw_size += len(decoded)
                    if raw_size > meta['raw_bytes'] or decoder.unused_data:
                        raise SnapshotError('Snapshot decompression exceeds declared payload')
                    raw_hash.update(decoded)
                    out.write(decoded)
                    chunk = decoder.unconsumed_tail
            if not decoder.eof or raw_size != meta['raw_bytes']:
                raise SnapshotError('Incomplete snapshot payload')
            if raw_hash.hexdigest() != meta['raw_sha256'] or compressed_hash.hexdigest() != meta['compressed_sha256']:
                raise SnapshotError('Snapshot payload digest mismatch')
            out.flush()
            os.fsync(out.fileno())
        last_commit = _validate_database(staged, stop, deadline)
        if last_commit != meta['last_commit']:
            raise SnapshotError('Snapshot metadata disagrees with SQLite history')
        _check(stop, deadline)
        _install_database(staged, dest)
        return SnapshotInfo(**{field:meta[field] for field in SnapshotInfo.__dataclass_fields__})
