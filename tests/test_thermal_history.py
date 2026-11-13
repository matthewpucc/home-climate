import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from house_climate.thermal_history import ThermalHistory, Retention, DAY

REGIME = {"configuration_hash":"synthetic","rooms":{"room":{"head":"head","placement":"A"}}}


def snapshot(at, value=70, mode="cool"):
    return {"event":"live","rooms":{"room":{"temperature":value,"source_evidence":[{
        "entity_id":"sensor.synthetic","source":"probe","temperature_f":value,
        "observed_at":at,"reported_at":at,"timestamp_kind":"ha_report","freshness":"qualified"}]}},
        "room_targets":{"room":{"cool_requested":mode=="cool","heat_requested":mode=="heat"}}}


class ThermalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"history.sqlite"
        self.store = ThermalHistory(self.path)
        self.store.open()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def append(self, at, **kw):
        self.store.append("entry","session",at,REGIME,snapshot(at,**kw))

    def test_roundtrip_reopen_and_cached_reports_deduplicated(self):
        self.append(1000)
        self.store.append("entry","session",1010,REGIME,snapshot(1000))
        self.store.close()
        self.store.open()
        rows = self.store.lookback(900,1100)
        self.assertEqual(len(rows["observations"]),1)
        self.assertEqual(rows["episodes"][0]["decisions"],2)
        self.assertIsNone(rows["episodes"][0]["end"])

    def test_idempotent_decisions(self):
        self.append(1000)
        self.append(1000)
        self.assertEqual(self.store.lookback(900,1100)["episodes"][0]["decisions"],1)

    def test_restart_gap_mode_and_regime_split(self):
        self.append(1000)
        self.store.append("entry","new",1010,REGIME,snapshot(1010))
        self.store.append("entry","new",1400,REGIME,snapshot(1400))
        self.store.append("entry","new",1410,REGIME,snapshot(1410,mode="heat"))
        changed = {**REGIME,"placement":"B"}
        self.store.append("entry","new",1420,changed,snapshot(1420))
        rows=self.store.lookback(900,1500)["episodes"]
        self.assertEqual([r["end_reason"] for r in rows], ["restart","gap","demand_changed","regime_changed",None])
        self.assertEqual(rows[1]["end"],1010)

    def test_missing_and_stale_not_zero(self):
        data=snapshot(1000)
        data["rooms"]["room"]["source_evidence"][0]["freshness"]="change_only_unverified"
        self.store.append("entry","session",1000,REGIME,data)
        self.store.append("entry","session",1010,REGIME,{"event":"fault"})
        result=self.store.lookback(900,1100)
        self.assertEqual(result["observations"],[])
        self.assertIsNone(result["episodes"][-1]["minimum"])
        self.assertEqual(result["episodes"][-1]["demand"],"unknown")

    def test_atomic_rollup_before_delete_and_retry(self):
        self.append(1000,value=68)
        self.append(1060,value=72)
        with patch.object(self.store,"_merge_summary",side_effect=RuntimeError("fail")):
            with self.assertRaises(RuntimeError):
                self.store.cleanup(32*DAY)
        self.assertEqual(len(self.store.lookback(900,1200)["observations"]),2)
        self.store.cleanup(32*DAY)
        self.store.cleanup(32*DAY)
        result=self.store.lookback(900,1200)
        self.assertEqual(result["observations"],[])
        row=result["summaries"][0]
        self.assertEqual((row["n"],row["total"],row["minimum"],row["maximum"],row["longest_gap"]),(2,140,68,72,60))

    def test_hourly_sufficient_statistics_and_expiration(self):
        self.append(1000,value=60)
        self.append(1010,value=70)
        self.append(1300,value=80)
        self.store.cleanup(32*DAY)
        self.store.cleanup(367*DAY)
        result=self.store.lookback(0,3600)
        row=result["summaries"][0]
        self.assertEqual((row["width"],row["n"],row["total"],row["squares"]),(3600,3,210,14900))
        self.assertEqual(row["longest_gap"],290)
        self.store.cleanup(1097*DAY)
        self.assertEqual(self.store.lookback(0,3600)["summaries"],[])

    def test_cleanup_batch_bound_and_partial_bucket(self):
        for at in (1000,1300,1600): self.append(at)
        self.store.cleanup(32*DAY,batch_buckets=1)
        self.assertEqual(len(self.store.lookback(0,2000)["observations"]),2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0],3)

    def test_regimes_never_merged(self):
        self.append(1000)
        self.store.append("entry","session",1010,{**REGIME,"placement":"B"},snapshot(1010))
        self.store.cleanup(32*DAY)
        self.assertEqual(len(self.store.lookback(0,1500)["summaries"]),2)

    def test_future_schema_preserved(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db: db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError): self.store.open()
        with closing(sqlite3.connect(self.path)) as db: self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],999)

    def test_budget_does_not_delete_evidence(self):
        self.append(1000)
        self.store.retention=Retention(max_bytes=1)
        with self.assertRaises(OSError): self.append(1010)
        self.assertEqual(len(self.store.lookback(0,2000)["observations"]),1)

    def test_bounded_queries_and_validation(self):
        self.append(1000)
        self.append(1010)
        self.assertTrue(self.store.lookback(0,2000,limit=1)["observations_truncated"])
        for start,end in ((1,1),(float("nan"),10)):
            with self.assertRaises(ValueError): self.store.lookback(start,end)
        with self.assertRaises(ValueError): Retention(raw_days=366)
        with self.assertRaises(ValueError): self.append(float("nan"))

    def test_physical_sample_identity_survives_rereport_and_kind_change(self):
        data=snapshot(1000)
        evidence=data["rooms"]["room"]["source_evidence"][0]
        evidence["packet_received_at"]=1000
        self.store.append("entry","session",1000,REGIME,data)
        evidence.update(reported_at=1010,timestamp_kind="bluetooth_packet_receipt")
        self.store.append("entry","session",1010,REGIME,data)
        self.assertEqual(len(self.store.lookback(0,2000)["observations"]),1)

    def test_episode_context_survives_raw_expiry(self):
        data=snapshot(1000)
        data["head_fan_investigation"]={"heads":{"head":{"reported_mode":"auto"}}}
        self.store.append("entry","session",1000,REGIME,data)
        data["head_fan_investigation"]["heads"]["head"]["reported_mode"]="low"
        self.store.append("entry","session",1010,REGIME,data)
        self.store.cleanup(32*DAY)
        result=self.store.lookback(0,2000)
        self.assertEqual(result["decisions"],[])
        self.assertEqual([json.loads(r["context"])["fan"]["reported_mode"] for r in result["episodes"]],["auto","low"])
        self.assertEqual(result["episodes"][0]["end_reason"],"context_changed")

    def test_retiring_session_cannot_close_newer_episode(self):
        self.append(1000)
        self.store.append("entry","new",1100,REGIME,snapshot(1100))
        self.store.append("entry","session",1050,REGIME,snapshot(1050))
        rows=self.store.lookback(0,2000)["episodes"]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]["session"],"new")
        self.assertIsNone(rows[-1]["end"])

    def test_over_budget_database_can_reopen_and_summarize(self):
        self.append(1000)
        self.store.close()
        self.store=ThermalHistory(self.path,Retention(max_bytes=1))
        self.store.open()
        self.store.cleanup(32*DAY)
        self.assertEqual(len(self.store.lookback(0,2000)["summaries"]),1)

    def test_trial_journal_roundtrip_does_not_break_room_episode(self):
        self.append(1000)
        trial = {"trial_id":"synthetic-trial", "entity_id":"climate.test", "mode":"low", "stage":"request_reserved"}
        self.store.append("entry", "session", 1005, REGIME,
                          {"event":"fan_trial", "head_fan_trial":{"trial":trial,"physical_confirmation":"unknown"}})
        self.append(1010)
        rows = self.store.lookback(900,1100)
        self.assertEqual(len(rows["episodes"]),1)
        self.assertEqual(rows["episodes"][0]["decisions"],2)
        self.assertIn("synthetic-trial",str(rows["decisions"]))
