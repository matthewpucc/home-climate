"""Exercise the bridge and worker against real Home Assistant state/services."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant, CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.house_climate import async_setup_entry, async_unload_entry
from custom_components.house_climate.config_flow import HouseClimateConfigFlow
from custom_components.house_climate.configuration import load_configuration
from custom_components.house_climate.runtime import ControllerRuntime, NativeAPI
from custom_components.house_climate.controller.ha import HAError
from custom_components.house_climate.controller.config import Config, Settings
from custom_components.house_climate.controller.__main__ import preflight
from custom_components.house_climate.controller.executor import Executor
from custom_components.house_climate.controller.ha_package import watchdog_signature
from custom_components.house_climate.controller.policy import Policy
from custom_components.house_climate.sensor import ControllerStatus
from tests.test_controller import config, fixture, add_helpers


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        await self.hass.config.async_set_time_zone("America/Los_Angeles")
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.cfg = replace(config(), poll_seconds=.03, off_settle_seconds=.04,
                           command_timeout_seconds=1,
                           equipment_timing_confirmed=True, heater_setpoint_confirmed=True)
        self.calls = []
        self.runtimes = []
        self.stops = []
        for domain, services in {
            "climate": ("set_hvac_mode", "set_temperature"),
            "fan": ("turn_off", "set_percentage"),
            "input_boolean": ("turn_on", "turn_off"),
            "input_datetime": ("set_datetime",),
            "input_text": ("set_value",),
        }.items():
            for service in services:
                self.hass.services.async_register(domain, service, self.handle)
        states = fixture(self.cfg, datetime.now(timezone.utc), guest=76, living=67)
        add_helpers(states)
        states["input_boolean.hp_enabled"] = {"state": "on"}
        states["input_boolean.hp_heater_owned"]["state"] = "off"
        for entity, state in states.items():
            self.hass.states.async_set(entity, state["state"], state.get("attributes", {}))
        source = Path("tests/fixtures/legacy.toml").read_text().replace(
            "equipment_timing_confirmed = false", "equipment_timing_confirmed = true"
        ).replace("heater_setpoint_confirmed = false", "heater_setpoint_confirmed = true")
        Path(self.directory.name, "house_climate.toml").write_text(source)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.async_stop()
        for remove in self.stops:
            remove()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def handle(self, call):
        self.calls.append((call.domain, call.service, dict(call.data)))
        entity = call.data["entity_id"]
        old = self.hass.states.get(entity)
        attrs = dict(old.attributes) if old else {}
        state = old.state if old else "off"
        if call.service == "set_temperature":
            state = call.data["hvac_mode"]
            attrs["temperature"] = call.data["temperature"]
        elif call.service == "set_hvac_mode":
            state = call.data["hvac_mode"]
        elif call.service in ("turn_on", "turn_off"):
            state = "on" if call.service == "turn_on" else "off"
        elif call.service == "set_percentage":
            state, attrs["percentage"] = "on", call.data["percentage"]
        elif call.service == "set_datetime":
            attrs["timestamp"] = call.data["timestamp"]
            state = datetime.fromtimestamp(call.data["timestamp"], timezone.utc).isoformat()
        elif call.service == "set_value":
            state = call.data["value"]
        self.hass.states.async_set(entity, state, attrs)

    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.005)

    def runner(self, live):
        runtime = ControllerRuntime(self.hass, self.cfg, live)
        self.runtimes.append(runtime)
        return runtime

    async def test_shadow_worker_reads_real_ha_without_writes_or_signal_handlers(self):
        runtime = self.runner(False)
        with patch("signal.signal", side_effect=AssertionError("Must not replace HA signal handlers")):
            runtime.async_start()
            await self.until(lambda: runtime.status["event"] == "shadow")
            self.assertEqual(runtime.status["mode"], "cool")
            self.assertEqual(self.calls, [])
            self.assertTrue(await runtime.async_stop())
        self.assertFalse(runtime.thread.is_alive())

    async def test_live_worker_keeps_off_barrier_and_stops_devices_on_unload(self):
        runtime = self.runner(True)
        runtime.async_start()
        await self.until(lambda: self.hass.states.get(self.cfg.heater_entity).state == "heat")
        await self.until(lambda: self.hass.states.get(self.cfg.heads["guest"]).state == "cool")
        ownership = next(i for i, call in enumerate(self.calls) if call[1] == "turn_on")
        activation = next(i for i, call in enumerate(self.calls) if call[1] == "set_temperature")
        self.assertLess(ownership, activation)
        off_before = {c[2]["entity_id"] for c in self.calls[:activation]
                      if c[1] == "set_hvac_mode" and c[2]["hvac_mode"] == "off"}
        self.assertTrue(set(self.cfg.heads.values()) <= off_before)
        before_stop = len(self.calls)
        self.assertTrue(await runtime.async_stop())
        after_stop = self.calls[before_stop:]
        self.assertFalse(any(c[1] in ("set_temperature", "set_percentage", "turn_on") for c in after_stop))
        for entity in [self.cfg.heater_entity, *self.cfg.heads.values(), *self.cfg.fans.values()]:
            self.assertEqual(self.hass.states.get(entity).state, "off")
        self.assertEqual(self.hass.states.get("input_boolean.hp_heater_owned").state, "off")

    async def test_bridge_refuses_shadow_writes_and_activation_after_stop(self):
        stop = threading.Event()
        api = NativeAPI(self.hass, False, stop, self.cfg)
        with self.assertRaises(HAError):
            await asyncio.to_thread(api.service, "climate", "set_hvac_mode",
                                    entity_id=self.cfg.heater_entity, hvac_mode="heat")
        api.live = True
        stop.set()
        with self.assertRaises(HAError):
            await asyncio.to_thread(api.service, "climate", "set_hvac_mode",
                                    entity_id=self.cfg.heater_entity, hvac_mode="heat")
        self.assertEqual(self.calls, [])
        await asyncio.to_thread(api.service, "climate", "set_hvac_mode",
                                entity_id=self.cfg.heater_entity, hvac_mode="off")
        self.assertEqual(len(self.calls), 1)

    async def test_bridge_does_not_block_event_loop_or_lose_probe_report_times(self):
        api = NativeAPI(self.hass, False, threading.Event(), self.cfg)
        with self.assertRaises(RuntimeError):
            api.states()
        states = await asyncio.to_thread(api.states)
        probe = self.cfg.zones["living"].probes[0].entity_id
        self.assertIsInstance(states[probe]["last_reported"], str)
        self.assertIsNotNone(datetime.fromisoformat(states[probe]["last_reported"]).tzinfo)

    async def test_state_reads_finish_without_the_ha_loop_processing_callbacks(self):
        api = NativeAPI(self.hass, False, threading.Event(), self.cfg)
        self.hass.states.async_set("sensor.test_unrelated", "123")
        done, observed, errors = threading.Event(), {}, []

        def read():
            try:
                observed.update(api.states())
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        # Deliberately do not yield the HA loop. Cached reads must still finish.
        completed = done.wait(.5)
        await asyncio.to_thread(worker.join, 2)
        self.assertTrue(completed, "State reads waited for HA's event loop")
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("sensor.test_unrelated", observed)
        self.assertTrue(set(self.cfg.climate_entities) <= observed.keys())
        Settings.read(observed, self.cfg, strict=True)
        preflight(self.cfg, observed, Executor(self.cfg, api, "°F"))
        self.assertEqual(self.calls, [])

    async def test_selective_reads_include_custom_prefix_interlocks(self):
        cfg = replace(Config.from_dict(self.cfg.to_dict()), helper_prefix="test_controller")
        helpers = {
            cfg.entity("enabled"): ("on", {}),
            cfg.entity("heater_owned"): ("off", {}),
            cfg.entity("heartbeat", "input_datetime"): ("2026-10-16 05:00:00", {}),
            cfg.entity("status", "input_text"): ("", {}),
            cfg.entity("auxiliary_watchdog", "automation"): ("on", {}),
            cfg.entity("auxiliary_probe_safe", "binary_sensor"):
                ("on", {"configuration_signature": watchdog_signature(cfg)}),
        }
        for entity, (state, attrs) in helpers.items():
            self.hass.states.async_set(entity, state, attrs)
        api = NativeAPI(self.hass, False, threading.Event(), cfg)
        observed = await asyncio.to_thread(api.states)
        self.assertTrue(helpers.keys() <= observed.keys())
        self.assertTrue(set(cfg.fans.values()) <= observed.keys())
        self.assertTrue({p.entity_id for room in cfg.zones.values() for p in room.probes}
                        <= observed.keys())
        self.assertNotIn("input_number.hp_office_target", observed)
        self.assertNotIn("input_boolean.hp_enabled", observed)
        preflight(cfg, observed, Executor(cfg, api, "°F"))

    async def test_cached_reads_preserve_staleness_and_observe_removed_probes(self):
        api = NativeAPI(self.hass, False, threading.Event(), self.cfg)
        first = await asyncio.to_thread(api.states)
        second = await asyncio.to_thread(api.states)
        probe, other = self.cfg.zones["living"].probes[:2]
        self.assertEqual(first[probe.entity_id]["last_reported"],
                         second[probe.entity_id]["last_reported"])
        later = datetime.now(timezone.utc) + timedelta(seconds=self.cfg.sensor_stale_seconds + 1)
        plan = Policy(self.cfg).decide(second, later, Settings.read(second, self.cfg))
        self.assertEqual(plan.rooms, {})
        self.assertEqual(plan.head_targets_f, {})
        self.assertFalse(plan.heater_on)
        self.assertTrue(all(speed == 0 for speed in plan.fans.values()))
        self.hass.states.async_set(probe.entity_id, "unavailable")
        self.hass.states.async_remove(other.entity_id)
        observed = await asyncio.to_thread(api.states)
        self.assertEqual(observed[probe.entity_id]["state"], "unavailable")
        self.assertNotIn(other.entity_id, observed)

    async def test_expired_queued_heat_command_is_never_dispatched(self):
        api = NativeAPI(self.hass, True, threading.Event(), self.cfg)
        # Simulate eleven seconds passing between queuing and dispatch.
        clock = SimpleNamespace(monotonic=Mock(side_effect=[100, 111]))
        with patch("custom_components.house_climate.runtime.time", clock):
            with self.assertRaisesRegex(HAError, "expired before dispatch: services/climate/set_temperature"):
                await asyncio.to_thread(api.service, "climate", "set_temperature",
                                        entity_id=self.cfg.heater_entity, hvac_mode="heat", temperature=86)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get(self.cfg.heater_entity).state, "off")

    async def test_fault_details_remain_visible_after_recovery(self):
        runtime = self.runner(False)
        sensor = ControllerStatus(SimpleNamespace(entry_id="test", runtime_data=runtime))
        with self.assertLogs("custom_components.house_climate.runtime", level="ERROR"):
            runtime._async_report("fault", {"error": "Test native timeout"})
        failed_at = sensor.extra_state_attributes["last_fault_at"]
        runtime._async_report("shadow", {"mode": "cool"})
        self.assertEqual(sensor.native_value, "shadow")
        self.assertNotIn("error", sensor.extra_state_attributes)
        self.assertEqual(sensor.extra_state_attributes["last_error"], "Test native timeout")
        self.assertEqual(sensor.extra_state_attributes["last_fault_at"], failed_at)
        self.assertEqual(sensor.extra_state_attributes["fault_count"], 1)

    async def test_setup_waits_for_ha_start_and_unload_before_start_is_safe(self):
        from types import SimpleNamespace
        entry = SimpleNamespace(entry_id="native_test", data={"config_file": "house_climate.toml", "live": False},
                                options={}, async_on_unload=self.stops.append,
                                add_update_listener=Mock(return_value=lambda: None))
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_setup_entry(self.hass, entry)
        self.runtimes.append(entry.runtime_data)
        self.assertIsNone(entry.runtime_data.thread)
        await self.hass.services.async_call("house_climate", "generate_package", {}, blocking=True)
        self.assertTrue(Path(self.directory.name, "house_climate_generated/house_climate.yaml").exists())
        self.assertTrue(await async_unload_entry(self.hass, entry))
        self.assertNotIn("house_climate", self.hass.data)
        entry.runtime_data.async_start()
        self.assertIsNone(entry.runtime_data.thread)

    async def test_setup_rejects_an_existing_live_controller_lease(self):
        from types import SimpleNamespace
        entry = SimpleNamespace(data={"config_file": "house_climate.toml", "live": True}, options={})
        self.hass.states.async_set("input_boolean.hp_heater_owned", "on")
        self.hass.states.async_set("input_datetime.hp_heartbeat", "now",
                                  {"timestamp": datetime.now(timezone.utc).timestamp()})
        with self.assertRaises(ConfigEntryNotReady):
            await async_setup_entry(self.hass, entry)
        self.assertNotIn("house_climate", self.hass.data)

    async def test_config_flow_import_and_single_instance(self):
        flow = HouseClimateConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        flow._async_current_entries = Mock(return_value=[])
        menu = await flow.async_step_user()
        self.assertEqual(menu["type"], "menu")
        self.assertFalse(flow.live)
        result = await flow.async_step_import_file({"config_file": "house_climate.toml"})
        self.assertEqual(result["type"], "menu")
        self.assertEqual(len(flow.draft["rooms"]), 5)
        result = await flow.async_step_finish({})
        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["data"]["live"])
        self.assertEqual(result["data"]["configuration"]["schema_version"], 2)
        bad = await flow.async_step_import_file({"config_file": "../escape.toml"})
        self.assertEqual(bad["errors"], {"base": "invalid_config"})
        flow._async_current_entries.return_value = [object()]
        self.assertEqual((await flow.async_step_user())["type"], "abort")

    async def test_config_loader_preserves_live_commissioning_gate(self):
        path = Path(self.directory.name, "unconfirmed.toml")
        path.write_text(Path("tests/fixtures/legacy.toml").read_text())
        await asyncio.to_thread(load_configuration, self.directory.name, "unconfirmed.toml", False)
        with self.assertRaises(ValueError):
            await asyncio.to_thread(load_configuration, self.directory.name, "unconfirmed.toml", True)
        with self.assertRaises(ValueError):
            await asyncio.to_thread(load_configuration, self.directory.name, "unconfirmed.toml", "false")


if __name__ == "__main__":
    unittest.main()
