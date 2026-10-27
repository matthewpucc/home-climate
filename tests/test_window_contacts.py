"""Contact continuity is evidence, never an excuse to discard temperature."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.policy import Policy
from house_climate.recovery_inputs import signature
from house_climate.windows import WindowContacts, contact_state
from tests.test_rooms import NOW, configuration, states


CONTACT = "binary_sensor.fixture_opening"
SECOND = "binary_sensor.fixture_second_opening"


class WindowContactTests(unittest.TestCase):
    def setUp(self):
        base = Config.from_dict(configuration())
        self.cfg = replace(base, zones={**base.zones, "alpha": replace(base.zones["alpha"], windows=(CONTACT,))})
        self.observer = WindowContacts(self.cfg)

    def observe(self, raw="on", seconds=0, **attrs):
        data = {CONTACT: {"state": raw, "attributes": attrs}}
        return self.observer.observe(data, NOW + timedelta(seconds=seconds), monotonic_now=seconds)["alpha"]

    def test_open_and_close_each_require_continuous_confirmation(self):
        first = self.observe()
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(first["pending_state"], "open")
        self.assertEqual(self.observe(seconds=119)["state"], "unknown")
        self.assertEqual(self.observe(seconds=120)["state"], "open")
        closing = self.observe("off", 121)
        self.assertEqual(closing["state"], "open")
        self.assertEqual(closing["pending_state"], "closed")
        self.assertEqual(self.observe("off", 240)["state"], "open")
        self.assertEqual(self.observe("off", 241)["state"], "closed")

    def test_contact_bounce_cannot_accumulate_open_time(self):
        self.observe("on", 0)
        self.observe("off", 100)
        self.observe("on", 110)
        self.assertEqual(self.observe("on", 120)["state"], "unknown")
        self.assertEqual(self.observe("on", 229)["state"], "unknown")
        self.assertEqual(self.observe("on", 230)["state"], "open")

    def test_close_bounce_preserves_confirmed_open(self):
        self.observe("on", 0)
        self.observe("on", 120)
        self.observe("off", 150)
        self.observe("on", 200)
        result = self.observe("on", 300)
        self.assertEqual(result["state"], "open")
        self.assertIsNone(result["pending_state"])

    def test_unknown_unavailable_restored_and_missing_are_never_closed(self):
        for unavailable in (None, "unknown", "unavailable", "invalid", False):
            with self.subTest(unavailable=unavailable):
                observer = WindowContacts(self.cfg)
                observer.observe({CONTACT: {"state": "on"}}, NOW, monotonic_now=0)
                observer.observe({CONTACT: {"state": "on"}}, NOW + timedelta(seconds=120), monotonic_now=120)
                result = observer.observe({CONTACT: {"state": unavailable}}, NOW + timedelta(seconds=121), monotonic_now=121)["alpha"]
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["last_confirmed_state"], "open")
                self.assertIn(CONTACT, result["contact_issues"])
        self.assertEqual(contact_state({"state": "off", "attributes": {"restored": True}}),
                         ("unknown", "restored"))
        self.assertEqual(contact_state(None), ("unknown", "missing"))

    def test_unknown_breaks_pending_continuity_before_recovery(self):
        self.observe("on", 0)
        self.observe("unavailable", 119)
        self.observe("on", 120)
        self.assertEqual(self.observe("on", 239)["state"], "unknown")
        self.assertEqual(self.observe("on", 240)["state"], "open")

    def test_unchanged_report_on_change_contact_does_not_expire(self):
        data = {CONTACT: {"state": "on", "last_reported": (NOW - timedelta(days=30)).isoformat()}}
        self.observer.observe(data, NOW, monotonic_now=0)
        self.assertEqual(self.observer.observe(data, NOW + timedelta(seconds=120), monotonic_now=120)["alpha"]["state"], "open")
        self.assertEqual(self.observer.observe(data, NOW + timedelta(days=3), monotonic_now=3 * 86400)["alpha"]["state"], "open")

    def test_one_known_open_proves_open_but_closed_requires_all_contacts(self):
        cfg = replace(self.cfg, zones={**self.cfg.zones,
                                      "alpha": replace(self.cfg.zones["alpha"], windows=(CONTACT, SECOND))})
        observer = WindowContacts(cfg)
        data = {CONTACT: {"state": "on"}, SECOND: {"state": "unavailable"}}
        observer.observe(data, NOW, monotonic_now=0)
        result = observer.observe(data, NOW + timedelta(seconds=120), monotonic_now=120)["alpha"]
        self.assertEqual(result["state"], "open")
        self.assertIn(SECOND, result["contact_issues"])
        data[CONTACT]["state"] = "off"
        self.assertEqual(observer.observe(data, NOW + timedelta(seconds=121), monotonic_now=121)["alpha"]["state"], "unknown")
        data[SECOND]["state"] = "off"
        observer.observe(data, NOW + timedelta(seconds=122), monotonic_now=122)
        self.assertEqual(observer.observe(data, NOW + timedelta(seconds=242), monotonic_now=242)["alpha"]["state"], "closed")

    def test_backwards_clock_cannot_shortcut_a_pending_confirmation(self):
        self.observe("on", 0)
        self.observe("on", 60)
        self.observe("on", 30)
        self.assertEqual(self.observe("on", 120)["state"], "unknown")
        self.assertEqual(self.observe("on", 150)["state"], "open")

    def test_forward_wall_clock_jump_does_not_confirm_pending_contact(self):
        data = {CONTACT: {"state": "on"}}
        observer = WindowContacts(self.cfg, clock=lambda: 0)
        observer.observe(data, NOW)
        jumped = NOW + timedelta(days=1)
        self.assertEqual(observer.observe(data, jumped, monotonic_now=1)["alpha"]["state"], "unknown")
        self.assertEqual(observer.observe(data, jumped, monotonic_now=119)["alpha"]["state"], "unknown")
        self.assertEqual(observer.observe(data, jumped, monotonic_now=120)["alpha"]["state"], "open")

    def test_missed_close_reopen_cannot_inherit_pending_or_confirmed_open(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                observer = WindowContacts(self.cfg)
                data = {CONTACT: {"state": "on", "last_changed": NOW.isoformat()}}
                observer.observe(data, NOW, monotonic_now=0)
                if confirmed:
                    observer.observe(data, NOW + timedelta(seconds=120), monotonic_now=120)
                data[CONTACT]["last_changed"] = (NOW + timedelta(seconds=200)).isoformat()
                result = observer.observe(data, NOW + timedelta(seconds=210), monotonic_now=210)["alpha"]
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["pending_state"], "open")
                self.assertEqual(observer.observe(data, NOW + timedelta(seconds=329), monotonic_now=329)["alpha"]["state"], "unknown")
                self.assertEqual(observer.observe(data, NOW + timedelta(seconds=330), monotonic_now=330)["alpha"]["state"], "open")

    def test_another_contact_bounce_does_not_erase_continuous_known_open(self):
        cfg = replace(self.cfg, zones={**self.cfg.zones,
                                      "alpha": replace(self.cfg.zones["alpha"], windows=(CONTACT, SECOND))})
        observer = WindowContacts(cfg)
        data = {CONTACT: {"state": "on", "last_changed": NOW.isoformat()},
                SECOND: {"state": "off", "last_changed": NOW.isoformat()}}
        observer.observe(data, NOW, monotonic_now=0)
        data[SECOND]["last_changed"] = (NOW + timedelta(seconds=100)).isoformat()
        self.assertEqual(observer.observe(data, NOW + timedelta(seconds=120), monotonic_now=120)["alpha"]["state"], "open")

    def test_snapshot_is_defensive_and_pending_diagnostics_do_not_churn(self):
        first = self.observe("on", 0)
        self.assertEqual(first, self.observe("on", 60))
        first["contacts"][CONTACT] = "closed"
        first["state"] = "closed"
        self.assertEqual(self.observe("on", 120)["state"], "open")

    def test_contact_state_fences_repair_but_report_noise_does_not(self):
        data = states(self.cfg)
        data[CONTACT] = {"state": "on", "last_reported": NOW.isoformat(), "attributes": {"device_class": "door"}}
        settings = Settings.read({}, self.cfg)
        before = signature(self.cfg, data, NOW, settings)
        data[CONTACT]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(signature(self.cfg, data, NOW, settings), before)
        for value in ("off", "unknown", "unavailable"):
            changed = deepcopy(data)
            changed[CONTACT]["state"] = value
            self.assertNotEqual(signature(self.cfg, changed, NOW, settings), before)
        changed = deepcopy(data)
        changed[CONTACT]["last_changed"] = NOW.isoformat()
        self.assertNotEqual(signature(self.cfg, changed, NOW, settings), before)
        data[CONTACT]["attributes"]["restored"] = True
        self.assertNotEqual(signature(self.cfg, data, NOW, settings), before)

    def test_unconfigured_windows_add_no_diagnostics(self):
        cfg = Config.from_dict(configuration())
        self.assertEqual(WindowContacts(cfg).observe({CONTACT: {"state": "on"}}, NOW), {})

    def test_open_window_diagnostics_do_not_apply_an_unapproved_control_rule(self):
        plain = Config.from_dict(configuration())
        baseline, configured = Policy(plain), Policy(self.cfg)
        for seconds in (0, 120, 300):
            now = NOW + timedelta(seconds=seconds)
            data = states(self.cfg, {"alpha": 70, "beta": 71, "gamma": 68}, now)
            data[CONTACT] = {"state": "on"}
            reference = baseline.decide(data, now, Settings.read({}, plain)).as_dict()
            observed = configured.decide(data, now, Settings.read({}, self.cfg)).as_dict()
            for result in (reference, observed):
                result.pop("window_states")
                result.pop("recovery_inputs")
            self.assertEqual(observed, reference)

    def test_window_never_suppresses_raw_shared_room_protection(self):
        cfg = replace(self.cfg, head_cool_call_offset_f=5)
        data = states(cfg, {"alpha": 78, "beta": 65, "gamma": 68})
        data[CONTACT] = {"state": "on"}
        cadence = DecisionCadence(cfg)
        cadence.decide(data, NOW, Settings.read({}, cfg), monotonic_now=0)
        now = NOW + timedelta(seconds=120)
        for room in cfg.zones.values():
            for probe in room.probes:
                data[probe.entity_id]["last_reported"] = now.isoformat()
        plan = cadence.decide(data, now, Settings.read({}, cfg), monotonic_now=120)
        self.assertEqual(plan.window_states["alpha"]["state"], "open")
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertEqual(plan.head_constraints["shared"]["cool"]["kind"], "house_limit")

    def test_missing_temperature_still_inhibits_head_with_window_data(self):
        data = states(self.cfg, {"alpha": 78})
        data[CONTACT] = {"state": "on"}
        for probe in self.cfg.zones["beta"].probes:
            data.pop(probe.entity_id)
        plan = DecisionCadence(self.cfg).decide(data, NOW, Settings.read({}, self.cfg), monotonic_now=0)
        self.assertNotIn("shared", plan.head_targets_f)
        self.assertNotIn("beta", plan.rooms)
        self.assertIn("alpha", plan.window_states)

    def test_maintenance_updates_contact_diagnostics_without_extra_comfort_decision(self):
        cfg = replace(self.cfg, decision_interval_seconds=900)
        policy = Policy(cfg)
        policy.decide = Mock(wraps=policy.decide)
        cadence = DecisionCadence(cfg, policy)
        data = states(cfg, {"alpha": 74, "gamma": 68})
        data[CONTACT] = {"state": "on"}
        settings = Settings.read({}, cfg)
        cadence.decide(data, NOW, settings, monotonic_now=0)
        plan = cadence.decide(data, NOW + timedelta(seconds=120), settings, monotonic_now=120)
        self.assertEqual(policy.decide.call_count, 1)
        self.assertEqual(plan.window_states["alpha"]["state"], "open")


if __name__ == "__main__":
    unittest.main()
