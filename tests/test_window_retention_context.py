"""An acknowledged biased run cannot retain an older contact-relief context."""
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from house_climate.config import Settings
from house_climate.executor import Executor
from house_climate.policy import Policy
from tests.test_controller import FakeHA, NIGHT, config, fixture


CONTACT = "binary_sensor.fixture_retention_window"
OTHER_CONTACT = "binary_sensor.fixture_retention_other_window"


class WindowRetentionContextTests(unittest.TestCase):
    def setUp(self):
        base = config()
        self.cfg = replace(base, head_minimum_run_seconds=300,
            head_minimum_off_seconds=0, off_settle_seconds=180,
            head_heat_call_offset_f=5, head_cool_call_offset_f=5,
            zones={**base.zones,
                   "guest": replace(base.zones["guest"], windows=(CONTACT,)),
                   "office": replace(base.zones["office"], windows=(OTHER_CONTACT,))})
        self.ha = FakeHA(fixture(self.cfg))
        self.ha.data[CONTACT] = {"state": "on", "last_reported": NIGHT.isoformat()}
        self.ha.data[OTHER_CONTACT] = {"state": "on", "last_reported": NIGHT.isoformat()}
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.executor.phase, self.executor.direction = "running", "cool"
        self.guest = self.cfg.heads["guest"]
        self.bonus = self.cfg.heads["bonus"]

    def request(self, seconds, confirmed=(), *, mode="cool", heads=None, serialized=False):
        now = NIGHT + timedelta(seconds=seconds)
        for room in self.cfg.zones.values():
            for probe in room.probes:
                self.ha.data[probe.entity_id]["last_reported"] = now.isoformat()
        data = self.ha.states()
        plan = Policy(self.cfg).decide(data, now, Settings.read(data, self.cfg))
        plan.mode = mode
        plan.head_targets_f = dict({"guest": 72} if heads is None else heads)
        plan.head_call_targets_f = {key: target - 5 for key, target in plan.head_targets_f.items()}
        plan.heater_on = plan.auxiliary_fan_only = False
        plan.fans = {}
        # Contact debounce is tested separately. Here, feed the executor the
        # producer's already-confirmed context, including its serialized form.
        plan.protection_context["confirmed_open_rooms"] = tuple(confirmed)
        if serialized:
            plan.protection_context = json.loads(json.dumps(plan.protection_context))
        return plan

    def step(self, seconds, confirmed=(), *, mode="cool", heads=None, serialized=False):
        plan = self.request(seconds, confirmed, mode=mode, heads=heads, serialized=serialized)
        self.executor.step(plan, self.ha.states(), seconds)
        return plan

    def start(self, confirmed=(), *, heads=None):
        self.step(0, confirmed, heads=heads)
        self.step(15, confirmed, heads=heads)
        entity = self.guest if heads is None else self.cfg.heads[next(iter(heads))]
        self.assertEqual(self.ha.data[entity]["state"], "cool")
        self.assertEqual(self.ha.data[entity]["attributes"]["temperature"], 67)
        self.assertEqual(self.executor.head_cycles[entity].run_since, 15)
        self.ha.calls.clear()

    def assert_stopped(self, entity):
        self.assertEqual(self.ha.data[entity]["state"], "off")
        self.assertNotIn(entity, self.executor.cycle_holds)
        self.assertTrue(any(service == "set_hvac_mode" and data.get("entity_id") == entity
                            and data.get("hvac_mode") == "off"
                            for _domain, service, data in self.ha.calls))

    def test_new_confirmed_opening_cannot_retain_older_biased_run(self):
        self.start()
        plan = self.step(30, ("guest",), mode="off", heads={}, serialized=True)
        self.assertEqual(plan.head_targets_f, {})
        self.assert_stopped(self.guest)

    def test_confirmed_closure_cannot_retain_open_window_run_authorization(self):
        self.start(("guest",))
        plan = self.step(30, (), mode="off", heads={}, serialized=True)
        self.assertEqual(plan.head_targets_f, {})
        self.assert_stopped(self.guest)

    def test_other_attached_room_contact_change_fences_shared_head_retention(self):
        self.start(("office",), heads={"bonus": 72})
        self.step(30, (), mode="off", heads={})
        self.assert_stopped(self.bonus)

    def test_unchanged_empty_or_open_context_preserves_existing_minimum_run(self):
        for confirmed in ((), ("guest",)):
            with self.subTest(confirmed=confirmed):
                self.setUp()
                self.start(confirmed)
                plan = self.step(30, confirmed, mode="off", heads={}, serialized=True)
                self.assertEqual(plan.head_targets_f, {"guest": 72})
                self.assertEqual(self.ha.data[self.guest]["state"], "cool")
                hold = self.executor.cycle_holds[self.guest]
                self.assertEqual((hold["kind"], hold["remaining_seconds"]), ("minimum_run", 285))
                self.assertFalse(any(service == "set_hvac_mode" and data.get("entity_id") == self.guest
                                     for _domain, service, data in self.ha.calls))

    def test_unrelated_room_opening_or_closure_does_not_invalidate_another_head(self):
        for before, after in (((), ("office",)), (("office",), ())):
            with self.subTest(before=before, after=after):
                self.setUp()
                self.start(before)
                plan = self.step(30, after, mode="off", heads={})
                self.assertEqual(plan.head_targets_f, {"guest": 72})
                self.assertEqual(self.ha.data[self.guest]["state"], "cool")
                self.assertEqual(self.executor.cycle_holds[self.guest]["remaining_seconds"], 285)

    def test_fresh_eligible_request_with_changed_context_keeps_its_normal_run(self):
        self.start()
        plan = self.step(30, ("guest",), serialized=True)
        self.assertEqual(plan.head_targets_f, {"guest": 72})
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.executor.head_cycles[self.guest].run_since, 15)
        self.assertNotIn(self.guest, self.executor.cycle_holds)
        self.assertFalse(any(service == "set_hvac_mode" and data.get("entity_id") == self.guest
                             for _domain, service, data in self.ha.calls))

    def test_report_noise_with_same_confirmed_context_preserves_run_clock(self):
        self.start(("guest",))
        for seconds in (30, 45):
            self.ha.data[CONTACT]["last_reported"] = (NIGHT + timedelta(seconds=seconds)).isoformat()
            plan = self.step(seconds, ("guest",), mode="off", heads={}, serialized=True)
            self.assertEqual(plan.head_targets_f, {"guest": 72})
            self.assertEqual(self.ha.data[self.guest]["state"], "cool")
            self.assertEqual(self.executor.head_cycles[self.guest].run_since, 15)
            self.assertEqual(self.executor.cycle_holds[self.guest]["remaining_seconds"], 315 - seconds)

    def test_legacy_absent_context_matches_explicit_empty_context(self):
        self.start()
        plan = self.request(30, (), mode="off", heads={})
        plan.protection_context.pop("confirmed_open_rooms")
        self.executor.step(plan, self.ha.states(), 30)
        self.assertEqual(plan.head_targets_f, {"guest": 72})
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")


if __name__ == "__main__":
    unittest.main()
