"""Continuity tickets never choose window comfort policy or control devices."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import unittest

from house_climate.config import Config, Probe, Profile, Settings
from house_climate.protection import ProtectionTemperatures
from house_climate.window_authorization import (
    WindowAuthorization, build_window_authorization, check_window_authorization,
)
from tests.test_rooms import NOW, configuration, states


CONTACT = "binary_sensor.authorization_opening"
OUTDOOR = "sensor.authorization_outdoor"


class WindowAuthorizationTests(unittest.TestCase):
    def setUp(self):
        cfg = Config.from_dict(configuration())
        self.cfg = replace(cfg, protection=ProtectionTemperatures(),
            zones={**cfg.zones, "alpha": replace(cfg.zones["alpha"], windows=(CONTACT,))},
            outdoor_probes=(Probe(OUTDOOR, "authorization_outdoor_source"),))
        self.data = states(self.cfg, {"alpha": 71.1, "beta": 75.6}, NOW)
        self.data[CONTACT] = {"state": "on", "last_changed": (NOW - timedelta(days=30)).isoformat(),
                              "last_reported": NOW.isoformat(), "attributes": {}}
        self.data[OUTDOOR] = {"state": "51", "last_reported": NOW.isoformat(),
                              "attributes": {"unit_of_measurement": "°F"}}
        self.settings = Settings.read({}, self.cfg)

    def build(self, *, expiry=110, tick=100, data=None, now=NOW):
        return build_window_authorization(self.cfg, "shared", "cool", data or self.data,
            now, self.settings, expires_at_monotonic=expiry, monotonic_now=tick)

    def check(self, ticket, *, data=None, now=NOW, tick=101, cfg=None, settings=None,
              entity=None, mode="cool"):
        return check_window_authorization(ticket, cfg or self.cfg,
            entity or self.cfg.heads["shared"], mode, self.data if data is None else data,
            now, settings or self.settings, monotonic_now=tick)

    def test_current_scoped_snapshot_passes_without_choosing_or_mutating_policy(self):
        before = deepcopy(self.data)
        ticket = self.build()
        result = self.check(ticket)
        self.assertTrue(result)
        self.assertTrue(result.valid)
        self.assertTrue(result.applicable)
        self.assertEqual(self.data, before)
        self.assertEqual(ticket.mode, "cool")
        self.assertFalse(hasattr(ticket, "minimum_temperature_f"))

    def test_closed_unknown_restored_or_missing_contact_invalidates_old_ticket(self):
        ticket = self.build()
        for contact in ({"state": "off"}, {"state": "unknown"}, {"state": "unavailable"},
                        {"state": "on", "attributes": {"restored": True}}, None):
            with self.subTest(contact=contact):
                current = deepcopy(self.data)
                if contact is None:
                    current.pop(CONTACT)
                else:
                    current[CONTACT] = {**current[CONTACT], **contact}
                self.assertFalse(self.check(ticket, data=current))

    def test_close_reopen_marker_invalidates_even_when_cached_value_ends_open(self):
        ticket = self.build()
        current = deepcopy(self.data)
        current[CONTACT]["last_changed"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertFalse(self.check(ticket, data=current))

    def test_assumed_contact_cannot_reuse_or_establish_trusted_open_feedback(self):
        ticket = self.build()
        current = deepcopy(self.data)
        current[CONTACT]["attributes"]["assumed_state"] = True
        self.assertFalse(self.check(ticket, data=current))
        with self.assertRaises(ValueError):
            self.build(data=current)
        # Trust metadata alone, without changing the contact's cached value,
        # must invalidate the old authorization.
        self.assertEqual(current[CONTACT]["state"], "on")

    def test_one_trusted_open_contact_remains_usable_alongside_an_unknown_one(self):
        other = "binary_sensor.authorization_second_opening"
        room = self.cfg.zones["alpha"]
        self.cfg = replace(self.cfg, zones={**self.cfg.zones,
            "alpha": replace(room, windows=(CONTACT, other))})
        self.data[other] = {"state": "unknown", "last_changed": NOW.isoformat()}
        ticket = self.build()
        self.assertTrue(self.check(ticket))
        current = deepcopy(self.data)
        current[other]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertTrue(self.check(ticket, data=current))
        current[CONTACT]["state"] = "unavailable"
        self.assertFalse(self.check(ticket, data=current))

    def test_old_report_on_change_contact_is_valid_and_report_noise_does_not_renew(self):
        ticket = self.build()
        current = deepcopy(self.data)
        current[CONTACT]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        current[CONTACT]["last_updated"] = (NOW + timedelta(seconds=1)).isoformat()
        current[CONTACT]["attributes"]["friendly_name"] = "A display-only name"
        for room in self.cfg.zones.values():
            for probe in room.probes:
                current[probe.entity_id]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        current[OUTDOOR]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertTrue(self.check(ticket, data=current, now=NOW + timedelta(seconds=1)))
        self.assertEqual(ticket.expires_at_monotonic, 110)
        result = self.check(ticket, data=current, now=NOW + timedelta(seconds=1), tick=110)
        self.assertFalse(result)
        self.assertIn("expired", result.reason)

    def test_relevant_temperature_change_and_invalid_readings_invalidate(self):
        ticket = self.build()
        probe = self.cfg.zones["alpha"].probes[0].entity_id
        for value in ("70.9", "65", "unknown", "unavailable", True, "nan"):
            with self.subTest(value=value):
                current = deepcopy(self.data)
                current[probe]["state"] = value
                self.assertFalse(self.check(ticket, data=current))
        for attribute in ("restored", "assumed_state"):
            current = deepcopy(self.data)
            current[probe]["attributes"][attribute] = True
            self.assertFalse(self.check(ticket, data=current))

    def test_unchanged_probe_expiry_invalidates_without_any_new_event(self):
        ticket = self.build(expiry=2000)
        result = self.check(ticket, now=NOW + timedelta(seconds=self.cfg.sensor_stale_seconds + .01),
                            tick=101)
        self.assertFalse(result)
        self.assertIn("no longer current", result.reason)

    def test_outdoor_value_or_validity_change_invalidates_but_forecast_noise_does_not(self):
        ticket = self.build(expiry=5000)
        current = deepcopy(self.data)
        current[OUTDOOR]["attributes"]["forecast"] = [{"temperature": -40}]
        self.assertTrue(self.check(ticket, data=current))
        current[OUTDOOR]["state"] = "31"
        self.assertFalse(self.check(ticket, data=current))
        current = deepcopy(self.data)
        for room in self.cfg.zones.values():
            for probe in room.probes:
                current[probe.entity_id]["last_reported"] = (NOW + timedelta(seconds=1801)).isoformat()
        self.assertFalse(self.check(ticket, data=current, now=NOW + timedelta(seconds=1801)))

    def test_unrelated_head_mode_probe_and_helper_changes_do_not_self_invalidate(self):
        ticket = self.build()
        current = deepcopy(self.data)
        current[self.cfg.heads["second"]]["state"] = "heat"
        for probe in self.cfg.zones["gamma"].probes:
            current[probe.entity_id]["state"] = "81"
        unrelated = replace(self.settings,
                            room_profiles={"gamma": Profile(target_f=69, half_band_f=1)})
        self.assertTrue(self.check(ticket, data=current, settings=unrelated))
        current[self.cfg.heads["shared"]]["state"] = "cool"
        self.assertTrue(self.check(ticket, data=current, settings=unrelated))

    def test_related_profile_or_source_configuration_change_requires_new_plan(self):
        ticket = self.build()
        changed = replace(self.settings,
                          room_profiles={"alpha": Profile(target_f=70, half_band_f=1)})
        self.assertFalse(self.check(ticket, settings=changed))
        room = self.cfg.zones["alpha"]
        changed_cfg = replace(self.cfg, zones={**self.cfg.zones,
            "alpha": replace(room, probes=(replace(room.probes[0], offset_f=.1), *room.probes[1:]))})
        self.assertFalse(self.check(ticket, cfg=changed_cfg))

    def test_ticket_scope_is_immutable_and_wrong_head_or_mode_is_rejected(self):
        ticket = self.build()
        with self.assertRaises(FrozenInstanceError):
            ticket.mode = "heat"
        self.assertFalse(self.check(ticket, entity=self.cfg.heads["second"]))
        self.assertFalse(self.check(ticket, mode="heat"))
        self.assertFalse(self.check(None))

    def test_invalid_expiry_or_clock_cannot_create_or_renew_authorization(self):
        for expiry in (100, 99, True, None, float("nan"), float("inf"), "110"):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.build(expiry=expiry)
        ticket = self.build()
        for tick in (99, float("nan"), True):
            self.assertFalse(self.check(ticket, tick=tick))

    def test_off_needs_no_ticket_or_live_evidence(self):
        result = self.check(None, data={}, mode="off", tick=float("nan"))
        self.assertTrue(result)
        self.assertFalse(result.applicable)

    def test_initial_redundant_missing_or_stale_probe_does_not_reject_room(self):
        entity = self.cfg.zones["alpha"].probes[1].entity_id
        for status in ("missing", "stale", "unavailable", "assumed"):
            with self.subTest(status=status):
                current = deepcopy(self.data)
                if status == "missing":
                    current.pop(entity)
                elif status == "stale":
                    current[entity]["last_reported"] = (NOW - timedelta(
                        seconds=self.cfg.sensor_stale_seconds + 1)).isoformat()
                elif status == "assumed":
                    current[entity]["attributes"]["assumed_state"] = True
                else:
                    current[entity]["state"] = status
                ticket = self.build(data=current)
                self.assertTrue(self.check(ticket, data=current))
                # Recovering an excluded source changes the authorized fusion
                # inputs even if it agrees with the surviving temperature.
                current[entity] = deepcopy(self.data[entity])
                self.assertFalse(self.check(ticket, data=current))

    def test_excluded_source_material_invalidity_changes_invalidate(self):
        entity = self.cfg.zones["alpha"].probes[1].entity_id
        current = deepcopy(self.data)
        current[entity]["state"] = "unknown"
        ticket = self.build(data=current)
        current[entity]["last_reported"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertTrue(self.check(ticket, data=current))
        current[entity]["state"] = "unavailable"
        self.assertFalse(self.check(ticket, data=current))

    def test_loss_of_all_room_sources_or_unknown_contact_rejects_authorization(self):
        ticket = self.build()
        current = deepcopy(self.data)
        for probe in self.cfg.zones["alpha"].probes:
            current[probe.entity_id]["state"] = "unknown"
        self.assertFalse(self.check(ticket, data=current))
        with self.assertRaises(ValueError):
            self.build(data=current)
        current = deepcopy(self.data)
        current[CONTACT]["state"] = "unknown"
        with self.assertRaises(ValueError):
            self.build(data=current)


if __name__ == "__main__":
    unittest.main()
