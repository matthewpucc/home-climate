"""Private SQLite thermal evidence, independent of Recorder and actuator control.

All methods are synchronous and must run on a dedicated worker, never HA's loop.
Episodes describe *planned room demand*, not confirmed physical HVAC operation.
Rollups describe distinct source reports, not interpolated time-weighted coverage.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time

DAY = 86400
SCHEMA = 1


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True)
class Retention:
    raw_days: int = 30
    five_minute_days: int = 365
    hourly_days: int = 1095
    episode_days: int = 365
    max_bytes: int = 1024 * 1024 * 1024
    min_free_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        values = (self.raw_days, self.five_minute_days, self.hourly_days,
                  self.episode_days, self.max_bytes, self.min_free_bytes)
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in values):
            raise ValueError("Retention values must be positive integers")
        if not self.raw_days <= self.five_minute_days <= self.hourly_days:
            raise ValueError("Summary retention must cover detailed retention")


class ThermalHistory:
    def __init__(self, path, retention=None):
        self.path = Path(path)
        self.retention = retention or Retention()
        self.db = None

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._check_space()
        db = sqlite3.connect(self.path, timeout=1)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA):
                raise ValueError("Unsupported thermal history schema; database preserved")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS regimes (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS decisions (entry TEXT, session TEXT, at REAL, regime TEXT, payload TEXT NOT NULL, PRIMARY KEY(entry,session,at))")
                db.execute("CREATE TABLE IF NOT EXISTS observations (entry TEXT, room TEXT, regime TEXT, source TEXT, identity TEXT, at REAL, received REAL, value REAL, payload TEXT NOT NULL, PRIMARY KEY(entry,room,regime,source,identity))")
                db.execute("CREATE INDEX IF NOT EXISTS observations_time ON observations(at)")
                db.execute("CREATE TABLE IF NOT EXISTS summaries (entry TEXT, room TEXT, regime TEXT, source TEXT, width INTEGER, bucket INTEGER, n INTEGER, total REAL, squares REAL, minimum REAL, maximum REAL, first REAL, last REAL, longest_gap REAL, PRIMARY KEY(entry,room,regime,source,width,bucket))")
                db.execute("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, entry TEXT, session TEXT, room TEXT, regime TEXT, demand TEXT, start REAL, last REAL, end REAL, end_reason TEXT, decisions INTEGER, minimum REAL, maximum REAL, context TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS episodes_open ON episodes(entry,room,end)")
                db.execute("CREATE INDEX IF NOT EXISTS summaries_time ON summaries(bucket)")
                db.execute("CREATE INDEX IF NOT EXISTS decisions_time ON decisions(at)")
                db.execute("CREATE INDEX IF NOT EXISTS episodes_time ON episodes(start)")
                db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
                db.execute(f"PRAGMA user_version={SCHEMA}")
            self.db = db
        except BaseException:
            db.close()
            raise

    def _check_space(self):
        if shutil.disk_usage(self.path.parent).free < self.retention.min_free_bytes:
            raise OSError("Insufficient free space for thermal history")
        if self.db is not None:
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            wal = Path(str(self.path)+"-wal")
            size = (pages-free)*page_size + (wal.stat().st_size if wal.exists() else 0)
        else:
            size = sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists())
        if size >= self.retention.max_bytes:
            raise OSError("Thermal history storage budget reached")

    def append(self, entry, session, at, regime_metadata, payload):
        if not finite(at) or at < 0:
            raise ValueError("Invalid decision timestamp")
        body = encode(payload)
        if len(body.encode()) > 262144:
            raise ValueError("Thermal decision exceeds bounded record size")
        self._check_space()
        regime = digest(regime_metadata)
        db = self.db
        with db:
            db.execute("INSERT OR IGNORE INTO regimes VALUES (?,?)", (regime, encode(regime_metadata)))
            inserted = db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)", (entry,session,at,regime,body)).rowcount
            if not inserted:
                return
            latest = db.execute("SELECT max(at) FROM decisions WHERE entry=?", (entry,)).fetchone()[0]
            if latest > at:
                # Preserve out-of-order evidence as a decision, but do not let a
                # late retiring writer or clock rollback rewrite newer episodes.
                return
            # A new session never bridges downtime into a completed episode.
            db.execute("UPDATE episodes SET end=last,end_reason='restart' WHERE entry=? AND session!=? AND end IS NULL", (entry,session))
            rooms = payload.get("rooms", {})
            for room in regime_metadata["rooms"]:
                reading = rooms.get(room) or {}
                value = reading.get("temperature")
                value = value if finite(value) else None
                targets = payload.get("room_targets", {}).get(room, {})
                demand = ("heat" if targets.get("heat_requested") else "cool" if targets.get("cool_requested") else "idle")
                if not reading or payload.get("event") not in ("live", "shadow"):
                    demand = "unknown"
                head = regime_metadata["rooms"][room]["head"]
                fan = payload.get("head_fan_investigation", {}).get("heads", {}).get(head, {})
                context = encode({"event":payload.get("event"),"mode":payload.get("mode"),
                    "phase":payload.get("phase"),"target":targets,
                    "fan":{k:fan.get(k) for k in ("entity_id","reported_mode","physical_confirmation","reason")},
                    "head_call_target":payload.get("head_call_targets_f", {}).get(head),
                    "windows":payload.get("window_states", {}),
                    "source_quality":[{k:e.get(k) for k in ("source","entity_id","freshness","timestamp_kind")}
                        for e in reading.get("source_evidence", ()) if isinstance(e,dict)]})
                previous = db.execute("SELECT id,last,regime,demand,context FROM episodes WHERE entry=? AND room=? AND end IS NULL", (entry,room)).fetchone()
                if previous and (previous[2] != regime or previous[3] != demand or previous[4] != context or at - previous[1] > 180 or at <= previous[1]):
                    reason = "regime_changed" if previous[2] != regime else "gap" if at - previous[1] > 180 else "clock_changed" if at <= previous[1] else "demand_changed" if previous[3] != demand else "context_changed"
                    db.execute("UPDATE episodes SET end=last,end_reason=? WHERE id=?", (reason,previous[0]))
                    previous = None
                if previous:
                    db.execute("UPDATE episodes SET last=?,decisions=decisions+1,minimum=CASE WHEN ? IS NULL THEN minimum WHEN minimum IS NULL THEN ? ELSE min(minimum,?) END,maximum=CASE WHEN ? IS NULL THEN maximum WHEN maximum IS NULL THEN ? ELSE max(maximum,?) END WHERE id=?", (at,value,value,value,value,value,value,previous[0]))
                else:
                    db.execute("INSERT INTO episodes(entry,session,room,regime,demand,start,last,decisions,minimum,maximum,context) VALUES (?,?,?,?,?,?,?,1,?,?,?)", (entry,session,room,regime,demand,at,at,value,value,context))
                for evidence in reading.get("source_evidence", ()):
                    if not isinstance(evidence, dict) or evidence.get("freshness") != "qualified":
                        continue
                    stamp_kind = next((key for key in ("measurement_at", "packet_received_at", "observed_at")
                                       if finite(evidence.get(key))), None)
                    observed = evidence.get(stamp_kind) if stamp_kind else None
                    temperature = evidence.get("temperature_f")
                    if not finite(observed) or not finite(temperature) or not 0 <= at-observed <= 180:
                        continue
                    source = str(evidence.get("source", ""))
                    identity = digest({"entity_id":evidence.get("entity_id"), "at":observed,
                                       "kind":stamp_kind})
                    db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)", (entry,room,regime,source,identity,observed,at,temperature,encode(evidence)))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_commit',?)", (str(at),))

    def cleanup(self, now, *, batch_buckets=100):
        """Bounded atomic rollup+delete of closed buckets; never remove unrolled detail."""
        if not finite(now) or batch_buckets < 1 or batch_buckets > 1000:
            raise ValueError("Invalid cleanup bounds")
        db = self.db
        raw_before = int((now-self.retention.raw_days*DAY)//300)*300
        five_before = int((now-self.retention.five_minute_days*DAY)//3600)*3600
        hourly_before = int((now-self.retention.hourly_days*DAY)//3600)*3600
        with db:
            keys = db.execute("SELECT entry,room,regime,source,CAST(at/300 AS INTEGER)*300 FROM observations WHERE at<? GROUP BY 1,2,3,4,5 LIMIT ?", (raw_before,batch_buckets)).fetchall()
            for entry,room,regime,source,bucket in keys:
                rows = db.execute("SELECT at,value FROM observations WHERE entry=? AND room=? AND regime=? AND source=? AND at>=? AND at<? ORDER BY at", (entry,room,regime,source,bucket,bucket+300)).fetchall()
                stamps, values = zip(*rows)
                gap = max((b-a for a,b in zip(stamps,stamps[1:])), default=0)
                self._merge_summary((entry,room,regime,source,300,bucket), (len(values),sum(values),sum(v*v for v in values),min(values),max(values),min(stamps),max(stamps),gap))
                db.execute("DELETE FROM observations WHERE entry=? AND room=? AND regime=? AND source=? AND at>=? AND at<?", (entry,room,regime,source,bucket,bucket+300))
            keys = db.execute("SELECT entry,room,regime,source,CAST(bucket/3600 AS INTEGER)*3600 FROM summaries WHERE width=300 AND bucket<? GROUP BY 1,2,3,4,5 LIMIT ?", (five_before,batch_buckets)).fetchall()
            for entry,room,regime,source,bucket in keys:
                rows = db.execute("SELECT n,total,squares,minimum,maximum,first,last,longest_gap FROM summaries WHERE entry=? AND room=? AND regime=? AND source=? AND width=300 AND bucket>=? AND bucket<? ORDER BY bucket", (entry,room,regime,source,bucket,bucket+3600)).fetchall()
                gap = max([r[7] for r in rows] + [b[5]-a[6] for a,b in zip(rows,rows[1:])])
                self._merge_summary((entry,room,regime,source,3600,bucket), (sum(r[0] for r in rows),sum(r[1] for r in rows),sum(r[2] for r in rows),min(r[3] for r in rows),max(r[4] for r in rows),min(r[5] for r in rows),max(r[6] for r in rows),gap))
                db.execute("DELETE FROM summaries WHERE entry=? AND room=? AND regime=? AND source=? AND width=300 AND bucket>=? AND bucket<?", (entry,room,regime,source,bucket,bucket+3600))
            # Decisions are contextual snapshots, not losslessly summarized.
            # Only prune a bounded batch after source rollups for that period finish.
            if not db.execute("SELECT 1 FROM observations WHERE at<? LIMIT 1", (raw_before,)).fetchone():
                db.execute("DELETE FROM decisions WHERE rowid IN (SELECT rowid FROM decisions WHERE at<? LIMIT 1000)", (raw_before,))
            db.execute("DELETE FROM episodes WHERE id IN (SELECT id FROM episodes WHERE end IS NOT NULL AND end<? LIMIT 1000)", (now-self.retention.episode_days*DAY,))
            db.execute("DELETE FROM summaries WHERE rowid IN (SELECT rowid FROM summaries WHERE width=3600 AND bucket<? LIMIT 1000)", (hourly_before,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_cleanup',?)", (str(now),))
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def _merge_summary(self, key, values):
        # A late observation is rejected by ingestion's freshness gate; existing
        # summaries may still be revisited during bounded cleanup retries.
        old = self.db.execute("SELECT n,total,squares,minimum,maximum,first,last,longest_gap FROM summaries WHERE entry=? AND room=? AND regime=? AND source=? AND width=? AND bucket=?", key).fetchone()
        if old:
            n,total,squares,lo,hi,first,last,gap = values
            gap = max(gap,old[7],max(0,first-old[6],old[5]-last))
            values = (n+old[0],total+old[1],squares+old[2],min(lo,old[3]),max(hi,old[4]),min(first,old[5]),max(last,old[6]),gap)
        self.db.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*key,*values))

    def lookback(self, start, end, *, limit=200):
        if not finite(start) or not finite(end) or end <= start or not 1 <= limit <= 1000:
            raise ValueError("Invalid lookback bounds")
        result = {"schema":SCHEMA,"start":start,"end":end,"semantics":"distinct source-report statistics; not time-weighted coverage or physical HVAC runtime"}
        response_bytes = 0
        for table,where,args in (
            ("decisions","at>=? AND at<?",(start,end)),
            ("observations","at>=? AND at<?",(start,end)),
            ("summaries","bucket<? AND bucket+width>?",(end,start)),
            ("episodes","start<? AND COALESCE(end,last)>=?",(end,start))):
            row_limit = min(limit,32) if table == "decisions" else limit
            cursor = self.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid LIMIT ?", (*args,row_limit+1))
            names = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
            result[table] = []
            for row in rows[:row_limit]:
                item = dict(zip(names,row))
                size = len(encode(item).encode())
                if response_bytes + size > 2*1024*1024:
                    break
                result[table].append(item)
                response_bytes += size
            result[table+"_truncated"] = len(rows)>len(result[table])
        result["regimes"] = {}
        regime_ids = {row["regime"] for table in ("decisions","observations","summaries","episodes")
                      for row in result[table]}
        for regime in sorted(regime_ids):
            row = self.db.execute("SELECT metadata FROM regimes WHERE id=?", (regime,)).fetchone()
            if row and response_bytes+len(row[0].encode()) <= 2*1024*1024:
                result["regimes"][regime] = json.loads(row[0])
                response_bytes += len(row[0].encode())
        result["regimes_truncated"] = len(result["regimes"]) < len(regime_ids)
        result["metadata"] = dict(self.db.execute("SELECT key,value FROM metadata"))
        result["availability"] = {"raw_available":bool(result["observations"]),
                                  "summaries_available":bool(result["summaries"]),
                                  "coverage_complete":False}
        result["limitations"] = "Returned rows are bounded; gaps, missing edge coverage and deleted detail are not reconstructed."
        return result

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


def read_history(path, start, end, limit=200):
    """Bounded read-only transaction; does not create a missing database."""
    store = ThermalHistory(path)
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=1)
    deadline = time.monotonic()+5
    db.set_progress_handler(lambda: int(time.monotonic()>deadline), 1000)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
            raise ValueError("Unsupported thermal history schema")
        db.execute("BEGIN")
        store.db = db
        return store.lookback(start,end,limit=limit)
    finally:
        db.close()
