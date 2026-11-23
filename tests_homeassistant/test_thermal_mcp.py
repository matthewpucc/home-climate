"""Real HA tool registration and the private, bounded SQLite read surface."""
import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest

import probatio
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import llm

from custom_components.house_climate.config_flow import _values
from custom_components.house_climate.controller.protection import ProtectionTemperatures
from custom_components.house_climate.const import CONF_THERMAL_MCP, DOMAIN
from custom_components.house_climate.controller.thermal_history import ThermalHistory
from custom_components.house_climate import thermal_mcp


class ThermalHistoryMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.auth = SimpleNamespace(async_get_user=AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(is_active=user_id != "disabled",
                                                        is_admin=user_id != "member")))
        path = Path(self.directory.name) / "thermal.sqlite"
        self.entry = SimpleNamespace(entry_id="synthetic", data={}, options={CONF_THERMAL_MCP: True})
        self.runtime = SimpleNamespace(stop_event=threading.Event(), thermal_storage=SimpleNamespace(path=path))
        self.hass.data[DOMAIN] = self.runtime
        self.remove = thermal_mcp.register(self.hass, self.entry, self.runtime)
        self.now = time.time()
        self.args = {"start": self.now - 120, "end": self.now - 1}
        self.context = self.ctx("admin")
        store = ThermalHistory(path)
        store.open()
        try:
            for entry, room in (("synthetic", "living"), ("not_this_entry", "hidden")):
                store.append(entry, "session-private", self.now - 10,
                    {"rooms": {room: {"head": "synthetic_head"}}, "hidden_wiring": "private_value"},
                    {"event": "live", "mode": "heat", "warnings": ["ignored free text"],
                     "head_fan_trial": {"trial": {"trial_id": "private_trial"}},
                     "rooms": {room: {"temperature": 67, "source_evidence": [
                         {"freshness": "qualified", "source": "room-probe", "entity_id": "sensor.private_probe",
                          "timestamp_kind": "device_observation", "measurement_at": self.now - 11,
                          "temperature_f": 67}]}},
                     "room_targets": {room: {"heat_requested": True, "target_f": 69}}})
        finally:
            store.close()

    async def asyncTearDown(self):
        self.remove()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def ctx(self, user, platform="mcp_server"):
        return llm.LLMContext(platform=platform, context=Context(user_id=user),
                              language="en", assistant="conversation", device_id=None)

    async def instance(self, context=None):
        return await llm.async_get_api(self.hass, thermal_mcp.API_ID, context or self.context)

    async def call(self, instance=None, args=None):
        instance = instance or await self.instance()
        # The optional Conversation component is not needed to exercise this
        # registry and tool; APIInstance's wrapper only adds tracing here.
        return await instance.tools[0].async_call(self.hass,
            llm.ToolInput(tool_name="GetThermalHistory", tool_args=self.args if args is None else args),
            instance.llm_context)

    def test_entry_default_off_and_saved_off_overrides_setup_opt_in(self):
        protection = ProtectionTemperatures()
        self.assertNotIn(CONF_THERMAL_MCP, _values({"live": False}, protection))
        enabled = _values({"live": False, CONF_THERMAL_MCP: True}, protection)
        self.assertIs(enabled[CONF_THERMAL_MCP], True)
        disabled = _values({"live": False, CONF_THERMAL_MCP: False}, protection,
                           thermal_mcp=True, keep_thermal_mcp=True)
        self.assertIs({**enabled, **disabled}[CONF_THERMAL_MCP], False)
        unrelated = _values({"live": False}, protection, thermal_mcp=False, keep_thermal_mcp=True)
        self.assertIs({**enabled, **unrelated}[CONF_THERMAL_MCP], False)

    async def test_registered_tool_is_read_only_projected_scoped_and_does_not_write(self):
        api = await self.instance()
        self.assertEqual([tool.name for tool in api.tools], ["GetThermalHistory"])
        fields = probatio.to_openapi(api.tools[0].parameters)
        self.assertEqual(set(fields["properties"]), {"start", "end", "limit"})
        self.assertEqual(set(fields["required"]), {"start", "end"})
        self.assertEqual(api.tools[0].annotations,
            llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False))
        path = self.runtime.thermal_storage.path
        before = path.read_bytes()
        result = await self.call(api)
        self.assertFalse(result.error)
        self.assertEqual(result.data["decisions"][0]["rooms"]["living"]["target_f"], 69)
        self.assertEqual(result.data["observations"][0]["timestamp_kind"], "device_observation")
        self.assertEqual(result.data["episodes"][0]["demand"], "heat")
        self.assertIs(result.data["availability"]["coverage_complete"], False)
        text = json.dumps(result.data)
        for private in ("hidden", "session-private", "private_trial", "private_probe",
                        "private_value", "ignored free text", "not_this_entry"):
            self.assertNotIn(private, text)
        self.assertEqual(path.read_bytes(), before)

    async def test_non_admin_anonymous_and_other_platform_never_offer_a_tool(self):
        for user in (None, "member", "disabled"):
            with self.subTest(user=user):
                try:
                    self.assertEqual((await self.instance(self.ctx(user))).tools, [])
                except Unauthorized:
                    pass  # Newer HA may reject before constructing the API instance.
        self.assertEqual((await self.instance(self.ctx("admin", "conversation"))).tools, [])

    async def test_cached_tool_rechecks_disable_and_runtime_identity(self):
        api = await self.instance()
        self.entry.options[CONF_THERMAL_MCP] = False
        with self.assertRaises(HomeAssistantError):
            await self.call(api)
        self.entry.options[CONF_THERMAL_MCP] = True
        self.runtime.stop_event.set()
        with self.assertRaises(HomeAssistantError):
            await self.call(api)
        self.runtime.stop_event.clear()
        self.hass.data[DOMAIN] = object()
        with self.assertRaises(HomeAssistantError):
            await self.call(api)

    async def test_registration_callback_unregisters_and_reload_replaces_without_old_tool(self):
        old = await self.instance()
        self.remove()
        with self.assertRaises(HomeAssistantError):
            await self.instance()
        replacement = SimpleNamespace(stop_event=threading.Event(), thermal_storage=self.runtime.thermal_storage)
        self.hass.data[DOMAIN] = replacement
        self.remove = thermal_mcp.register(self.hass, self.entry, replacement)
        self.assertFalse((await self.call()).error)
        with self.assertRaises(HomeAssistantError):
            await self.call(old)

    async def test_validation_and_missing_database(self):
        for changes in ({"start": True}, {"start": float("nan")}, {"start": float("inf")},
                        {"start": "0"}, {"start": -1}, {"end": self.now + 3600},
                        {"start": self.now - 10 * 86400}, {"limit": True}, {"limit": 0},
                        {"limit": 51}, {"limit": 1.5}, {"sql": "anything"}, {"room": "living"}):
            with self.subTest(changes=changes):
                result = await self.call(args={**self.args, **changes})
                self.assertTrue(result.error)
        missing = Path(self.directory.name) / "does-not-exist.sqlite"
        self.runtime.thermal_storage.path = missing
        result = await self.call()
        self.assertTrue(result.error)
        self.assertNotIn(str(missing), str(result.data))
        self.assertFalse(missing.exists())

    async def test_output_budget_and_empty_range_are_explicit(self):
        with patch.object(thermal_mcp, "MAX_BYTES", 610):
            result = await self.call()
            self.assertLessEqual(thermal_mcp._size(result.data), 610)
            self.assertTrue(any(result.data[key + "_truncated"] for key in thermal_mcp._TABLES))
        empty = await self.call(args={"start": self.now - 60, "end": self.now - 50})
        self.assertFalse(empty.error)
        self.assertEqual(empty.data["observations"], [])
        self.assertIs(empty.data["availability"]["raw_available"], False)

    async def test_permission_revoked_during_read_does_not_return_in_flight_history(self):
        entered, release = threading.Event(), threading.Event()
        original = thermal_mcp._read_for_tool

        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(*args)

        with patch.object(thermal_mcp, "_read_for_tool", delayed):
            job = asyncio.create_task(self.call())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.entry.options[CONF_THERMAL_MCP] = False
            finally:
                release.set()
            with self.assertRaises(HomeAssistantError):
                await job

    async def test_cancel_does_not_start_another_read_until_executor_has_finished(self):
        entered, release = threading.Event(), threading.Event()
        original = thermal_mcp._read_for_tool

        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(*args)

        with patch.object(thermal_mcp, "_read_for_tool", delayed):
            job = asyncio.create_task(self.call())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                self.assertTrue((await self.call()).error)
            finally:
                release.set()
                pending = self.hass.data.get(thermal_mcp._QUERY_JOB)
                if pending is not None:
                    await pending
        self.assertFalse((await self.call()).error)
