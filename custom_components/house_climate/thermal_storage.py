"""Bounded off-loop SQLite recording; never participates in control decisions."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import threading
import time
import uuid

from .controller.thermal_history import ThermalHistory, digest, encode
from .controller import thermal_snapshot

_LOGGER = logging.getLogger(__name__)
_FIELDS = ("head_fan_trial", "rooms", "room_targets", "head_fan_investigation", "head_call_targets_f",
           "mode", "phase", "window_states", "outdoor_source_evidence", "fans",
           "inhibited_heads", "device_issues", "pending_head_commands", "warnings")


class ThermalStorage:
    """A single writer for the single supported Home Climate runtime.

    Queue overflow and unavailable storage are observable. Neither inhibits nor
    delays existing safety/control. Future adaptive trials must require health.
    """
    def __init__(self, hass, config, entry_id):
        self.hass = hass
        self.entry_id = entry_id
        self.session = uuid.uuid4().hex
        # Supervisor excludes direct .cache children from every config archive.
        # The mutable SQLite/WAL is never the backup authority.
        self.path = Path(hass.config.path(".cache", "house_climate_thermal.sqlite"))
        self.legacy_path = Path(hass.config.path(".storage", "house_climate_thermal.sqlite"))
        self.snapshot_path = Path(hass.config.path(".storage", "house_climate_thermal.snapshot"))
        self.regime = {"configuration_hash":digest(config.to_dict()), "rooms": {
            name:{"head":room.head,"probes":[asdict(p) for p in room.probes]}
            for name,room in config.zones.items()}}
        self._queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._snapshot_lock = hass.data.setdefault("house_climate_snapshot_lock", threading.Lock())
        self._storage_ready = threading.Event()
        self._snapshot_info = None
        self._snapshot_error = None
        self._snapshot_in_progress = False
        self._recovered_from_snapshot = False
        self._thread = None
        self._error = None
        self._last_commit = None
        self._dropped = 0
        self._committed = 0
        self._closed = False
        self._failed = False
        self._last_payload = None
        self._last_submitted = None

    @property
    def diagnostics(self):
        return {"status":"closed" if self._closed else "error" if self._error else "recording" if self._last_commit else "starting",
                "error":self._error,"last_commit":self._last_commit,
                "queue_depth":self._queue.qsize(),"dropped_records":self._dropped,
                "committed_records":self._committed,"schema_version":1,
                "backup_paused":False,
                "snapshot_in_progress":self._snapshot_in_progress,
                "snapshot_error":self._snapshot_error,
                "snapshot_age_seconds":max(0, time.time()-self._snapshot_info.created_at) if self._snapshot_info else None,
                "snapshot":asdict(self._snapshot_info) if self._snapshot_info else None,
                "recovered_from_snapshot":self._recovered_from_snapshot,
                "episodes":"planned room demand, not physical HVAC runtime",
                "placement_note":"Physical relocation requires a configuration identity change; not auto-detected"}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="home-climate-history", daemon=True)
        self._thread.start()

    def observe(self, event, data):
        if self._closed or self._stop.is_set():
            return
        if self._failed:
            self._dropped += 1
            return
        try:
            stamp = data.get("timestamp")
            now = datetime.fromisoformat(stamp) if isinstance(stamp,str) else datetime.now(timezone.utc)
            if now.tzinfo is None:
                raise ValueError("Naive history time")
            # Allowlist excludes ever-growing transition histories and arbitrary
            # error strings, private wiring and unrelated HA state.
            payload = {key:data[key] for key in _FIELDS if key in data}
            payload["event"] = event
            body = encode(payload)
            if len(body.encode()) > 262144:
                raise ValueError("History record too large")
            stamp = now.timestamp()
            if (body == self._last_payload and self._last_submitted is not None
                    and 0 <= stamp-self._last_submitted < 90):
                return
            payload = json.loads(body)
            payload["storage_dropped_records"] = self._dropped
            self._queue.put_nowait((stamp,payload))
            self._last_payload = body
            self._last_submitted = stamp
        except (ValueError, TypeError, OverflowError, queue.Full):
            self._dropped += 1

    def _run(self):
        store = ThermalHistory(self.path)
        cleanup_at = 0
        try:
            self._prepare_storage()
            if self._stop.is_set():
                return
            store.open()
            store.db.set_progress_handler(lambda: int(self._stop.is_set()), 1000)
            while not self._stop.is_set():
                try:
                    at,payload = self._queue.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    if self._stop.is_set():
                        self._dropped += 1
                        break
                    if time.monotonic() >= cleanup_at:
                        # Cleanup remains possible even when the write budget
                        # or free-space gate rejects new ingestion.
                        store.cleanup(time.time())
                        cleanup_at = time.monotonic()+60
                    store.append(self.entry_id,self.session,at,self.regime,payload)
                    self._last_commit = at
                    self._committed += 1
                    self._error = None

                except Exception as exc:
                    self._dropped += 1
                    error = type(exc).__name__
                    if self._error != error:
                        _LOGGER.warning("Thermal evidence storage failed; ordinary control is unaffected: %s", error)
                    self._error = error
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._failed = True
            self._error = type(exc).__name__
            _LOGGER.warning("Thermal evidence storage unavailable; ordinary control is unaffected: %s", type(exc).__name__)
        finally:
            self._storage_ready.set()
            store.close()

    def _acquire_snapshot_lock(self):
        deadline = time.monotonic() + 60
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._snapshot_lock.acquire(timeout=.1):
                return
        raise RuntimeError("Thermal snapshot refresh is busy or stopping")

    def _prepare_storage(self):
        self._acquire_snapshot_lock()
        try:
            self._prepare_storage_locked()
        finally:
            self._snapshot_lock.release()

    def _prepare_storage_locked(self):
        """Establish both working storage and a verified backup before recording.

        A stock Supervisor restore removes .cache, including with recorder DB
        excluded. Normal restart retains it. Never select stale legacy storage
        over an existing snapshot, or silently start empty after corrupt restore.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() and any(Path(str(self.path)+suffix).exists()
                                          for suffix in ("-wal", "-shm")):
            raise RuntimeError("Thermal working database is missing but sidecars remain")
        if self.path.exists():
            thermal_snapshot.validate_database(self.path, self._stop)
            had_snapshot = self.snapshot_path.exists()
            try:
                self._publish_snapshot()
            except Exception:
                # Already-migrated working history remains authoritative on an
                # ordinary restart. A backup fault must not disable recording.
                # A first-ever migration still requires a published artifact.
                if not had_snapshot or self.legacy_path.exists() or self._stop.is_set():
                    raise
        elif self.snapshot_path.exists():
            self._snapshot_info = thermal_snapshot.restore(
                self.snapshot_path, self.path, self._stop)
            self._recovered_from_snapshot = True
        elif self.legacy_path.exists():
            # Source may include committed WAL. SQLite backup captures both;
            # no copy2 of a live SQLite file and no legacy deletion on failure.
            self._snapshot_info = thermal_snapshot.publish(
                self.legacy_path, self.snapshot_path, self.path.parent, self._stop)
            thermal_snapshot.restore(self.snapshot_path, self.path, self._stop)
        else:
            if any(Path(str(self.legacy_path)+suffix).exists() for suffix in ("-wal", "-shm")):
                raise RuntimeError("Legacy thermal database is missing but sidecars remain")
            store = ThermalHistory(self.path)
            try:
                store.open()
            finally:
                store.close()
            self._publish_snapshot()
        thermal_snapshot.validate_database(self.path, self._stop)
        # Retain migration rollback evidence outside the archive. A crash while
        # moving these closed files cannot invalidate the verified snapshot.
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.legacy_path) + suffix)
            if source.exists():
                target = self.path.parent / ("house_climate_thermal.legacy" + suffix)
                if target.exists():
                    raise RuntimeError("Thermal migration rollback file already exists")
                source.replace(target)
        self._storage_ready.set()

    def _publish_snapshot(self):
        self._snapshot_in_progress = True
        try:
            self._snapshot_info = thermal_snapshot.publish(
                self.path, self.snapshot_path, self.path.parent, self._stop)
            self._snapshot_error = None
        except Exception as exc:
            self._snapshot_error = type(exc).__name__
            _LOGGER.warning("Thermal history snapshot failed; recording and control are unaffected: %s",
                            type(exc).__name__)
            raise
        finally:
            self._snapshot_in_progress = False

    async def async_pre_backup(self):
        """Refresh immutable evidence without pausing or taking a post-hook lease."""
        def prepare():
            if not self._thread or not self._thread.is_alive():
                raise RuntimeError("Thermal history writer is not running")
            if not self._storage_ready.wait(60):
                raise RuntimeError("Thermal history initialization has not completed")
            if self._closed or self._failed or self._stop.is_set():
                raise RuntimeError("Thermal history is unavailable for a consistent snapshot")
            self._acquire_snapshot_lock()
            try:
                if self._closed or self._stop.is_set():
                    raise RuntimeError("Thermal history is stopping")
                self._publish_snapshot()
            finally:
                self._snapshot_lock.release()
        await self.hass.async_add_executor_job(prepare)

    async def async_post_backup(self):
        """No lifetime is tied to Core/Supervisor's optional end notification."""

    async def async_close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._dropped += 1
            except queue.Empty:
                break
        if self._thread:
            await self.hass.async_add_executor_job(self._thread.join, 2)
            if self._thread.is_alive():
                self._error = "shutdown_timeout"
