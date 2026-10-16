"""Plans are readable as normal HA entity states, without inspecting attributes."""
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.house_climate.controller.config import Config, Settings
from custom_components.house_climate.controller.policy import Policy
from custom_components.house_climate.runtime import ControllerRuntime
from custom_components.house_climate.sensor import (
    AuxiliaryPlan, ControllerStatus, CurrentIssues, FanPlan, LastFault, Overview,
    RoomPlan, RoomTemperature, async_setup_entry,
)
from tests.test_rooms import NOW, configuration, states
from tests_homeassistant import test_native as fixtures


class PlanEntityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until

    def entry(self, config=None):
        runtime = ControllerRuntime(self.hass, config or Config.from_dict(configuration()), False)
        self.runtimes.append(runtime)
        return SimpleNamespace(entry_id="plans", runtime_data=runtime, disabled_by=None,
                               pref_disable_new_entities=False)

    def decide(self, entry, temperatures=None, now=NOW, data=None):
        cfg = entry.runtime_data.config
        decision = Policy(cfg).decide(
            data if data is not None else states(cfg, temperatures, now),
            now, Settings.read({}, cfg),
        )
        entry.runtime_data._async_report("shadow", {"phase": "new", **decision.as_dict()})
        return decision

    def rendered(self, entity):
        entity.hass = self.hass
        # Exercise SensorEntity's value/unit handling and HA's string-state limit.
        result = entity._async_calculate_state()
        State("sensor.test_plan", result.state, result.attributes)
        return result.state

    async def test_setup_exposes_enabled_normal_entities_with_friendly_fan_names(self):
        entry = self.entry()
        self.hass.states.async_set("fan.test_airflow", "off", {"friendly_name": "Desk fan"})
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        by_name = {entity.name: entity for entity in added}
        for name in ("Plan summary", "Current issues", "North alcove plan", "Quiet space plan",
                     "Desk fan planned speed"):
            entity = by_name[name]
            self.assertIsNone(entity.entity_category)
            self.assertTrue(entity.entity_registry_enabled_default)
            self.assertTrue(entity.entity_registry_visible_default)
            self.assertEqual(entity.device_info["identifiers"], {("house_climate", "plans")})
        self.assertFalse(any(isinstance(entity, AuxiliaryPlan) for entity in added))
        self.assertEqual(by_name["North alcove temperature"].unique_id, "plans_room_alpha")
        self.assertEqual(self.rendered(by_name["Plan summary"]), "Waiting for Home Assistant")

    async def test_shared_head_plans_show_actual_selected_target_and_room_band(self):
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 68})
        self.assertEqual(self.rendered(Overview(entry)),
                         "Preview only · Would cool North alcove, South nook")
        self.assertEqual(self.rendered(RoomPlan(entry, "alpha")),
                         "Would cool shared head to 72 °F · comfort 71.5 °F–72.5 °F")
        # The second area shares the head's target, not an invented independent target.
        self.assertEqual(self.rendered(RoomPlan(entry, "beta")),
                         "Would cool shared head to 72 °F · limits only 66 °F–75 °F")
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")
        self.assertEqual(ControllerStatus(entry).native_value, "shadow")
        self.assertEqual(self.calls, [])

    async def test_metric_plan_and_temperature_agree(self):
        self.hass.config.units = METRIC_SYSTEM
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 68})
        self.assertEqual(self.rendered(RoomPlan(entry, "alpha")),
                         "Would cool shared head to 22.2 °C · comfort 21.9 °C–22.5 °C")
        self.assertAlmostEqual(float(self.rendered(RoomTemperature(entry, "alpha"))), 22.8, places=1)

    async def test_schedules_use_decision_timezone_and_clear_daytime_fan_plan(self):
        cfg = replace(Config.from_dict(configuration()), timezone="America/Los_Angeles")
        entry = self.entry(cfg)
        fan = cfg.fan_controls["fan.test_rest"]
        # UTC 23:00 is 16:00 locally: base limits and fan off.
        self.decide(entry, {"gamma": 68})
        self.assertIn("limits only 66 °F–75 °F", self.rendered(RoomPlan(entry, "gamma")))
        self.assertEqual(self.rendered(FanPlan(entry, fan, "Rest fan")), "0")
        # UTC 06:00 next day is 23:00 locally: the sleep profile applies.
        self.decide(entry, {"gamma": 68}, now=NOW + timedelta(hours=7))
        self.assertIn("comfort 67 °F–69 °F", self.rendered(RoomPlan(entry, "gamma")))
        self.assertEqual(self.rendered(FanPlan(entry, fan, "Rest fan")), "60")
        self.decide(entry, {"gamma": 68}, now=NOW + timedelta(hours=13))
        self.assertIn("limits only", self.rendered(RoomPlan(entry, "gamma")))
        self.assertEqual(self.rendered(FanPlan(entry, fan, "Rest fan")), "0")

    async def test_missing_probe_fault_and_stop_never_display_previous_plan_as_current(self):
        entry = self.entry()
        data = states(entry.runtime_data.config, {"alpha": 78, "gamma": 68})
        data.pop("sensor.test_c")
        data.pop("sensor.test_d")
        self.decide(entry, data=data)
        self.assertIn("No usable temperature", self.rendered(RoomPlan(entry, "beta")))
        self.assertIn("keep shared head off", self.rendered(RoomPlan(entry, "alpha")))
        self.assertIn("warning(s)", self.rendered(Overview(entry)))
        self.assertIn("South nook", self.rendered(CurrentIssues(entry)))
        fan = next(iter(entry.runtime_data.config.fan_controls.values()))
        sensors = [RoomPlan(entry, "alpha"), FanPlan(entry, fan, "Desk fan")]
        with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
            entry.runtime_data._async_report("fault", {"error": "Test native timeout"})
        for sensor in sensors:
            self.assertFalse(sensor.available)
            self.assertEqual(self.rendered(sensor), "unavailable")
        self.assertEqual(self.rendered(Overview(entry)), "Controller fault: Test native timeout")
        self.decide(entry, {"alpha": 72, "beta": 71, "gamma": 68})
        self.assertEqual(self.rendered(CurrentIssues(entry)), "None")
        self.assertEqual(self.rendered(LastFault(entry)), "Test native timeout")
        entry.runtime_data._async_report("stopped", {})
        self.assertEqual(self.rendered(Overview(entry)), "Controller stopped")
        self.assertFalse(sensors[0].available)

    async def test_auxiliary_heat_fan_and_off_are_plans_not_equipment_feedback(self):
        raw = configuration()
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha")
        raw["rooms"]["alpha"]["profile"] = {"target_f": 68, "half_band_f": .5}
        entry = self.entry(Config.from_dict(raw))
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        sensor = next(entity for entity in added if isinstance(entity, AuxiliaryPlan))
        for temperature, expected in (
            (67, "Would heat using room probes"),
            (69, "Would circulate air without heat"),
            (68, "Would stay off"),
        ):
            self.decide(entry, {"alpha": temperature, "gamma": 68})
            self.assertEqual(self.rendered(sensor), expected)
        self.decide(entry, {"alpha": 67, "gamma": 68})
        entry.runtime_data.status.update(event="live", phase="settling")
        self.assertEqual(self.rendered(sensor), "Plan: heat using room probes")
        self.assertIn("Waiting for all heads off / settling", self.rendered(Overview(entry)))
        self.assertEqual(self.calls, [])

    async def test_reload_preserves_ids_and_removes_only_obsolete_owned_entities(self):
        entry = self.entry()
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_get_entry.return_value = entry
        registry = er.async_get(self.hass)
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        old = {}
        for entity in added:
            old[entity.unique_id] = registry.async_get_or_create(
                "sensor", "house_climate", entity.unique_id, config_entry=entry,
                original_name=entity.name,
            ).entity_id
        unrelated = registry.async_get_or_create(
            "sensor", "another_integration", "plans_room_removed", config_entry=entry
        )
        raw = configuration()
        raw["rooms"]["alpha"]["name"] = "Renamed alcove"
        raw["rooms"]["alpha"]["fans"] = []
        del raw["rooms"]["beta"]
        entry.runtime_data.config = Config.from_dict(raw)
        refreshed = []
        await async_setup_entry(self.hass, entry, refreshed.extend)
        self.assertEqual(registry.async_get_entity_id("sensor", "house_climate", "plans_room_alpha"),
                         old["plans_room_alpha"])
        self.assertEqual(registry.async_get_entity_id("sensor", "house_climate", "plans_plan_room_alpha"),
                         old["plans_plan_room_alpha"])
        for key in ("plans_room_beta", "plans_plan_room_beta", "plans_plan_fan_fan.test_airflow"):
            self.assertIsNone(registry.async_get(old[key]))
        self.assertIsNotNone(registry.async_get(unrelated.entity_id))
        self.assertIn("Renamed alcove plan", [entity.name for entity in refreshed])

    async def test_long_names_and_errors_fit_ha_state_limit(self):
        raw = configuration()
        raw["rooms"]["alpha"]["name"] = "Long room name " * 40
        entry = self.entry(Config.from_dict(raw))
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 68})
        self.assertEqual(len(self.rendered(Overview(entry))), 255)
        with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
            entry.runtime_data._async_report("fault", {"error": "Timed out " * 80})
        for sensor in (Overview(entry), CurrentIssues(entry), LastFault(entry)):
            self.assertEqual(len(self.rendered(sensor)), 255)

    async def test_native_shadow_worker_feeds_visible_plans_without_device_commands(self):
        entry = self.entry(self.cfg)
        entry.runtime_data.async_start()
        await self.until(lambda: entry.runtime_data.status["event"] == "shadow")
        self.assertTrue(self.rendered(Overview(entry)).startswith("Preview only"))
        self.assertIn("comfort", self.rendered(RoomPlan(entry, "guest")))
        for fan in self.cfg.fan_controls.values():
            self.assertTrue(FanPlan(entry, fan, "Test fan").available)
        self.assertEqual(self.calls, [])
        self.assertTrue(await entry.runtime_data.async_stop())
        self.assertEqual(self.rendered(Overview(entry)), "Controller stopped")

    async def test_unfinished_fan_shutdown_is_visible_without_blocking_plan(self):
        entry = self.entry()
        self.decide(entry, {"alpha": 73, "beta": 71, "gamma": 68})
        entry.runtime_data.status.update(event="live", phase="running",
                                         pending_fans=["fan.test_previous_mapping"])
        issues = CurrentIssues(entry)
        self.assertIn("fan.test_previous_mapping", self.rendered(issues))
        self.assertIn("retrying independently", self.rendered(issues))
        self.assertIn("1 warning(s)", self.rendered(Overview(entry)))
        self.assertTrue(RoomPlan(entry, "alpha").available)
        self.assertEqual(issues.extra_state_attributes["warnings"], [
            "Circulation fan fan.test_previous_mapping: waiting for off; retrying independently"])
        entry.runtime_data.status["pending_fans"] = []
        self.assertEqual(self.rendered(issues), "None")

    async def test_recovering_climate_plans_show_off_request_without_hiding_healthy_rooms(self):
        raw = configuration()
        raw.update(heater_entity="climate.test_auxiliary", auxiliary_room="alpha")
        raw["rooms"]["alpha"]["profile"] = {"target_f": 68, "half_band_f": .5}
        entry = self.entry(Config.from_dict(raw))
        self.decide(entry, {"alpha": 67, "gamma": 76})
        head = entry.runtime_data.config.heads[entry.runtime_data.config.zones["alpha"].head]
        entry.runtime_data.status.update(event="live", phase="running", device_issues={
            head: "Waiting for device to reconnect",
            "climate.test_auxiliary": "Waiting for heater off acknowledgement",
        })
        self.assertIn("wait for shared head; off requested", self.rendered(RoomPlan(entry, "alpha")))
        self.assertIn("cool head", self.rendered(RoomPlan(entry, "gamma")))
        self.assertEqual(self.rendered(AuxiliaryPlan(entry)), "Waiting for heater · off requested")
        entry.runtime_data.status["device_issues"] = {}
        self.assertNotIn("wait for", self.rendered(RoomPlan(entry, "alpha")))
        self.assertEqual(self.rendered(AuxiliaryPlan(entry)), "Plan: heat using room probes")
