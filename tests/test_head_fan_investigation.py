import copy
from datetime import datetime, timezone
import unittest

from house_climate.head_fan_investigation import experiment_window, fan_evidence
from house_climate.policy import Policy
from house_climate.config import Settings
from tests.test_ambient_stopping import configuration, states


class WindowTests(unittest.TestCase):
    def check(self, instant, expected, **kwargs):
        result = experiment_window(datetime.fromisoformat(instant), "America/Los_Angeles", **kwargs)
        self.assertEqual(result["eligible"], expected)
        return result

    def test_boundaries_and_restore_margin(self):
        self.check("2026-11-12T16:59:59+00:00", False)
        self.check("2026-11-12T17:00:00+00:00", True)
        self.check("2026-11-13T06:00:00+00:00", False)
        self.check("2026-11-13T05:54:59+00:00", True)
        self.check("2026-11-13T05:55:00+00:00", False)
        self.check("2026-11-13T05:40:00+00:00", False, trial_seconds=900)
        self.check("2026-11-13T05:39:59+00:00", True, trial_seconds=900)

    def test_dst_uses_local_time(self):
        self.check("2026-07-12T16:00:00+00:00", True)
        self.check("2026-07-13T05:00:00+00:00", False)
        for instant in ("2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"):
            self.check(instant, False)

    def test_invalid_inputs_fail_closed(self):
        for value in (float("nan"), float("inf"), -1, True, "bad"):
            self.check("2026-11-12T18:00:00+00:00", False, trial_seconds=value)
        for now in (None, "bad", datetime(2026, 11, 12, 12)):
            self.assertFalse(experiment_window(now, "UTC")["eligible"])
        self.assertFalse(experiment_window(datetime.now(timezone.utc), "No/Such_Zone")["eligible"])


class EvidenceTests(unittest.TestCase):
    def test_report_never_means_physical_confirmation(self):
        state = {"state": "cool", "attributes": {"fan_modes": ["auto", "low"], "fan_mode": "low"}}
        result = fan_evidence(state)
        self.assertEqual(result["reported_mode"], "low")
        self.assertEqual(result["physical_confirmation"], "unknown")
        for change in ({"state": "unavailable"}, {"attributes": {"fan_modes": "auto"}},
                       {"attributes": {"fan_modes": ["auto", None]}},
                       {"attributes": {"fan_modes": ["auto"], "restored": True}}):
            self.assertEqual(fan_evidence({**state, **change})["advertised_modes"], [])
        for malformed in (None, "bad", {"attributes": "bad"}, {"attributes": []}):
            self.assertEqual(fan_evidence(malformed)["advertised_modes"], [])
        state["attributes"]["fan_mode"] = "unsupported"
        self.assertIsNone(fan_evidence(state)["reported_mode"])

    def test_plan_exposes_shared_head_evidence_without_requests(self):
        cfg = configuration()
        data = states(cfg)
        before = copy.deepcopy(data)
        now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        # Keep snapshots current; this test does not infer physical polling.
        for state in data.values():
            state["last_reported"] = now.isoformat()
        before = copy.deepcopy(data)
        plan = Policy(cfg).decide(data, now, Settings.read({}, cfg))
        detail = plan.head_fan_investigation
        self.assertEqual(detail["stage"], "observation_only")
        self.assertFalse(detail["live_authorized"])
        self.assertEqual(set(detail["heads"]), set(cfg.heads))
        self.assertEqual(data, before)
        self.assertNotIn("head_fan_requests", plan.as_dict())
        self.assertFalse(detail["heads"]["gradient"]["joint_evaluation_required"])
        cfg.zones["healthy"] = cfg.zones["gradient"]
        plan = Policy(cfg).decide(data, now, Settings.read({}, cfg))
        self.assertTrue(plan.head_fan_investigation["heads"]["gradient"]["joint_evaluation_required"])


if __name__ == "__main__":
    unittest.main()
