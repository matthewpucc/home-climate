"""Shared-mode decisions must treat manual heads as observed, never writable."""
from datetime import datetime, timedelta, timezone
import unittest

from house_climate.executor import Executor
from house_climate.manual_head_direction import SCOPE_FIELD, trusted_mode
from house_climate.policy import Plan
from tests import test_controller as fixtures


class ManualHeadDirectionTests(unittest.TestCase):
    def setUp(self):
        self.config = fixtures.config()
        self.ha = fixtures.FakeHA(fixtures.fixture(self.config))
        self.executor = Executor(self.config, self.ha, "°F")
        self.manual = self.config.heads["guest"]
        self.sibling = self.config.heads["bonus"]
        self.plan = Plan(mode="heat", head_targets_f={"guest": 73, "bonus": 73})
        self.now = datetime.now(timezone.utc)
        self.boundary = self.now.timestamp() - 60
        self.ha.data[self.config.entity("status", "input_text")] = {
            "attributes": {SCOPE_FIELD: {self.manual: self.boundary}}}

    def report(self, entity, mode, *, age=1, action=None):
        value = self.ha.data[entity]
        value.update(state=mode, last_reported=(self.now - timedelta(seconds=age)).isoformat())
        if action:
            value["attributes"]["hvac_action"] = action
        else:
            value["attributes"].pop("hvac_action", None)

    def check(self, expected, now=0):
        self.executor.shared(self.plan, self.ha.states(), now)
        self.assertEqual(self.executor.phase, expected)
        self.assertFalse(any(data["entity_id"] == self.manual for _, _, data in self.ha.calls))

    def test_startup_and_shared_changeover_require_fresh_manual_off(self):
        self.report(self.manual, "heat")
        self.check("stopping")
        self.check("stopping", 300)
        self.assertFalse(any(c[1] == "set_temperature" for c in self.ha.calls))
        self.report(self.manual, "off")
        self.check("settling", 301)
        self.check("settling", 302)
        # A friendly-looking stale cached Off must break the whole settle.
        self.report(self.manual, "off", age=100)
        self.check("stopping", 800)
        self.report(self.manual, "off")
        self.check("settling", 801)
        self.assertFalse(any(c[1] == "set_temperature" for c in self.ha.calls))

    def test_incompatible_active_head_stops_only_managed(self):
        self.executor.phase, self.executor.direction = "running", "heat"
        self.report(self.sibling, "heat")
        self.report(self.manual, "cool")
        self.check("stopping")
        self.assertTrue(any(service == "set_hvac_mode" and row["entity_id"] == self.sibling
                            for _, service, row in self.ha.calls))

    def test_compatible_active_manual_allows_continuation_not_new_start(self):
        self.executor.phase, self.executor.direction = "running", "heat"
        self.report(self.manual, "heat")
        self.check("running")
        self.assertFalse(any(c[1] == "set_temperature" for c in self.ha.calls))
        self.report(self.sibling, "heat")
        self.check("running")
        self.assertTrue(any(service == "set_temperature" and row["entity_id"] == self.sibling
                            for _, service, row in self.ha.calls))

    def test_shutdown_and_reserve_split_never_write_or_use_manual_head(self):
        self.report(self.manual, "heat")
        self.executor.phase, self.executor.all_off_since = "settling", 0
        self.assertIsNone(self.executor.reserve_split_isolated_at(self.ha.states(), 600))
        self.executor.shutdown(self.ha.states())
        self.assertFalse(any(data["entity_id"] == self.manual for _, _, data in self.ha.calls))

    def test_off_is_not_inferred_from_ambiguous_modes_actions_or_missing_timezone(self):
        for mode, action in (("auto", "idle"), ("heat_cool", "idle"), ("off", "heating")):
            self.report(self.manual, mode, action=action)
            self.assertIsNone(trusted_mode(self.ha.data[self.manual], self.now.timestamp(), self.boundary))
        self.report(self.manual, "off")
        self.ha.data[self.manual]["last_reported"] = self.now.replace(tzinfo=None).isoformat()
        self.assertIsNone(trusted_mode(self.ha.data[self.manual], self.now.timestamp(), self.boundary))
