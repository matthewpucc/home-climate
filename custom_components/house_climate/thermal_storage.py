"""Bounded off-loop SQLite recording; never participates in control decisions."""
from __future__ import annotations

import asyncio
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

BACKUP_FENCE = "house_climate_history_backup_fence"
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
        self._backup_fence = hass.data.setdefault(BACKUP_FENCE, threading.Event())
        self.entry_id = entry_id
        self.session = uuid.uuid4().hex
        self.path = Path(hass.config.path(".storage", "house_climate_thermal.sqlite"))
        self.regime = {"configuration_hash":digest(config.to_dict()), "rooms": {
            name:{"head":room.head,"probes":[asdict(p) for p in room.probes]}
            for name,room in config.zones.items()}}
        self._queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._backup_state_lock = threading.Lock()
        self._backup_active = threading.Event()
        self._backup_generation = 0
        self._backup_ready_generation = 0
        self._backup_requested = threading.Event()
        self._backup_ready = threading.Event()
        self._backup_error = None
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
                "backup_paused":self._backup_ready.is_set() and self._backup_error is None,
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
            while self._backup_fence.is_set() and not self._stop.is_set():
                self._stop.wait(.1)
            if self._stop.is_set():
                return
            store.open()
            store.db.set_progress_handler(lambda: int(self._stop.is_set()), 1000)
            while not self._stop.is_set():
                with self._backup_state_lock:
                    pause = self._backup_requested.is_set()
                    if pause:
                        self._backup_active.set()
                if pause:
                    try:
                        self._pause_for_backup(store)
                    finally:
                        self._backup_active.clear()
                    if self._stop.is_set():
                        break
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
            store.close()

    def _pause_for_backup(self, store):
        try:
            busy, _, _ = store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise OSError("Thermal database reader prevented checkpoint")
            store.close()
            self._backup_error = None
        except Exception as exc:
            self._backup_error = type(exc).__name__
        finally:
            with self._backup_state_lock:
                self._backup_ready_generation = self._backup_generation
                self._backup_ready.set()
        while True:
            with self._backup_state_lock:
                if not self._backup_requested.is_set() or self._stop.is_set():
                    self._backup_ready.clear()
                    self._backup_ready_generation = 0
                    break
                # A new pre-backup may reuse this safely closed pause, but
                # must receive its own generation's acknowledgement.
                self._backup_ready_generation = self._backup_generation
            self._stop.wait(.1)
        if store.db is None and not self._stop.is_set():
            store.open()
            store.db.set_progress_handler(lambda: int(self._stop.is_set()), 1000)

    async def async_pre_backup(self):
        if self._closed or self._failed or not self._thread or not self._thread.is_alive():
            raise RuntimeError("Thermal history writer is unavailable for a consistent backup")
        with self._backup_state_lock:
            self._backup_generation += 1
            generation = self._backup_generation
            self._backup_requested.set()
        def prepared():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self._backup_state_lock:
                    if self._backup_ready.is_set() and self._backup_ready_generation == generation:
                        return True
                time.sleep(.01)
            return False
        try:
            ready = await self.hass.async_add_executor_job(prepared)
            if not ready or self._backup_error:
                raise RuntimeError("Could not prepare a consistent thermal history backup")
        except BaseException:
            await asyncio.shield(self.async_post_backup())
            raise

    async def async_post_backup(self):
        with self._backup_state_lock:
            self._backup_requested.clear()
        def released():
            deadline = time.monotonic() + 5
            while self._backup_active.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            return not self._backup_active.is_set()
        if not await self.hass.async_add_executor_job(released):
            raise RuntimeError("Thermal backup writer did not acknowledge release")

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
