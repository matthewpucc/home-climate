"""Confirmed contact context is shared separately from measured demand effects."""
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock

from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Probe, Settings
from house_climate.protection import ProtectionTemperatures
from tests.test_rooms import NOW, configuration, states


CONTACT = "binary_sensor.fixture_context_window"
SECOND = "binary_sensor.fixture_context_second_window"
OUTDOOR = "sensor.fixture_context_outdoor"


class ConfirmedOpenContextTests(unittest.TestCase):
    def setUp(self):
        self.prepare()

    def prepare(self):
        raw = configuration()
        for room in raw["rooms"].values():
            room["profile"] = {"target_f": 72, "half_band_f": 1, "priority": 1}
            room["schedules"] = []
            room["fans"] = []
        cfg = Config.from_dict(raw)
        self.cfg = replace(cfg, protection=ProtectionTemperatures(qualification_seconds=0),
            outdoor_probes=(Probe(OUTDOOR, "fixture_context_weather"),),
            zones={**cfg.zones, "alpha": replace(cfg.zones["alpha"], windows=(CONTACT,))})
        self.clock = 0
        self.cadence = DecisionCadence(self.cfg, clock=lambda: self.clock)
        self.settings = Settings.read({}, self.cfg)
        self.raw_contact = None
        self.contact_changed = NOW.isoformat()

    def tick(self, seconds, raw="on", *, missing=False, attributes=None,
             old_timestamp=False, changed=None, extra=None):
        self.clock = seconds
        now = NOW + timedelta(seconds=seconds)
        data = states(self.cfg, {"alpha": 72, "beta": 72, "gamma": 72}, now)
        if raw != self.raw_contact:
            self.raw_contact = raw
            self.contact_changed = now.isoformat()
        if changed is not None:
            self.contact_changed = changed
        if not missing:
            reported = NOW - timedelta(days=30) if old_timestamp else now
            data[CONTACT] = {"state": raw, "attributes": attributes or {},
                "last_changed": self.contact_changed, "last_reported": reported.isoformat()}
        data[OUTDOOR] = {"state": "50", "last_reported": now.isoformat(),
            "attributes": {"unit_of_measurement": "°F"}}
        data.update(extra or {})
        return self.cadence.decide(data, now, self.settings, monotonic_now=seconds)

    def test_only_confirmed_open_is_serialized_with_no_measured_effect(self):
        first = self.tick(0)
        self.assertEqual(first.protection_context["confirmed_open_rooms"], ())
        self.assertEqual(self.tick(119).protection_context["confirmed_open_rooms"], ())
        confirmed = self.tick(120)
        self.assertEqual(confirmed.protection_context["confirmed_open_rooms"], ("alpha",))
        self.assertEqual(confirmed.protection_context["window_effects"], {})
        self.assertEqual(confirmed.room_targets["alpha"]["effective_comfort_priority"],
                         {"heat": 1, "cool": 1})
        payload = json.loads(json.dumps(confirmed.protection_context))
        self.assertEqual(payload["confirmed_open_rooms"], ["alpha"])
        self.assertNotIn("window_states", payload)
        self.assertNotIn(CONTACT, json.dumps(payload["confirmed_open_rooms"]))

    def test_confirmed_closure_removes_context_after_existing_debounce(self):
        self.tick(0)
        self.tick(120)
        closing = self.tick(121, "off")
        self.assertEqual(closing.protection_context["confirmed_open_rooms"], ("alpha",))
        self.assertEqual(closing.window_states["alpha"]["pending_state"], "closed")
        self.assertEqual(self.tick(240, "off").protection_context["confirmed_open_rooms"], ("alpha",))
        self.assertEqual(self.tick(241, "off").protection_context["confirmed_open_rooms"], ())

    def test_unknown_unavailable_restored_and_missing_remove_context_immediately(self):
        failures = (("unknown", False, {}), ("unavailable", False, {}),
                    ("on", False, {"restored": True}), ("on", True, {}))
        for raw, missing, attributes in failures:
            with self.subTest(raw=raw, missing=missing, attributes=attributes):
                self.prepare()
                self.tick(0)
                self.assertEqual(self.tick(120).protection_context["confirmed_open_rooms"], ("alpha",))
                failed = self.tick(121, raw, missing=missing, attributes=attributes)
                self.assertEqual(failed.protection_context["confirmed_open_rooms"], ())
                self.assertEqual(failed.window_states["alpha"]["state"], "unknown")

    def test_bounce_and_missed_close_reopen_cannot_inherit_confirmation(self):
        self.tick(0)
        self.tick(60, "off")
        self.tick(119)
        self.assertEqual(self.tick(238).protection_context["confirmed_open_rooms"], ())
        self.assertEqual(self.tick(239).protection_context["confirmed_open_rooms"], ("alpha",))
        reopened = self.tick(250, changed=(NOW + timedelta(seconds=245)).isoformat())
        self.assertEqual(reopened.protection_context["confirmed_open_rooms"], ())
        self.assertEqual(self.tick(369).protection_context["confirmed_open_rooms"], ())
        self.assertEqual(self.tick(370).protection_context["confirmed_open_rooms"], ("alpha",))

    def test_old_unchanged_report_on_change_is_still_confirmed(self):
        old_changed = (NOW - timedelta(days=30)).isoformat()
        self.tick(0, old_timestamp=True, changed=old_changed)
        self.assertEqual(self.tick(120, old_timestamp=True).protection_context["confirmed_open_rooms"], ("alpha",))
        self.assertEqual(self.tick(86400, old_timestamp=True).protection_context["confirmed_open_rooms"], ("alpha",))

    def test_context_contains_only_configured_room_ids_in_stable_order(self):
        self.cfg = replace(self.cfg, zones={**self.cfg.zones,
            "beta": replace(self.cfg.zones["beta"], windows=(SECOND,))})
        self.cadence = DecisionCadence(self.cfg, clock=lambda: self.clock)
        self.settings = Settings.read({}, self.cfg)
        extra = {SECOND: {"state": "on"}, "binary_sensor.unconfigured_opening": {"state": "on"}}
        self.tick(0, extra=extra)
        plan = self.tick(120, extra=extra)
        self.assertEqual(plan.protection_context["confirmed_open_rooms"], ("alpha", "beta"))
        self.assertLessEqual(len(plan.protection_context["confirmed_open_rooms"]), len(self.cfg.zones))

    def test_confirmation_and_unknown_invalidate_cadence_while_effects_are_empty(self):
        decide = Mock(wraps=self.cadence.policy.decide)
        self.cadence.policy.decide = decide
        self.tick(0)
        self.tick(100)
        self.assertEqual(decide.call_count, 2)
        self.tick(119)
        self.assertEqual(decide.call_count, 2)
        self.assertEqual(self.cadence.next_decision, 190)
        confirmed = self.tick(120)
        self.assertEqual(confirmed.protection_context["window_effects"], {})
        self.assertEqual(decide.call_count, 3)
        self.assertEqual(self.cadence.next_decision, 210)
        unknown = self.tick(121, "unavailable")
        self.assertEqual(unknown.protection_context["window_effects"], {})
        self.assertEqual(decide.call_count, 4)

    def test_confirmed_closure_invalidates_cadence_before_next_regular_decision(self):
        decide = Mock(wraps=self.cadence.policy.decide)
        self.cadence.policy.decide = decide
        self.tick(0)
        self.tick(100)
        self.tick(120)
        calls = decide.call_count
        self.tick(121, "off")
        self.assertEqual(decide.call_count, calls)
        self.tick(240, "off")
        calls = decide.call_count
        self.assertEqual(self.cadence.next_decision, 330)
        closed = self.tick(241, "off")
        self.assertEqual(closed.protection_context["confirmed_open_rooms"], ())
        self.assertEqual(decide.call_count, calls + 1)


if __name__ == "__main__":
    unittest.main()
