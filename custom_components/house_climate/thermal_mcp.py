"""Opt-in, administrator-only MCP reads of the current entry's thermal history."""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time

import probatio as schema

from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import llm

from .const import CONF_THERMAL_MCP, DOMAIN
from .controller.thermal_history import read_history

API_ID = "house_climate_thermal_history"
MAX_SECONDS = 7 * 86400
MAX_ROWS = 50
MAX_BYTES = 65536
_QUERY_JOB = "house_climate_thermal_mcp_query"
_TABLES = ("decisions", "observations", "summaries", "episodes")
_TIMESTAMP_KINDS = ("device_observation", "bluetooth_packet_receipt", "ha_report")
_END_REASONS = ("restart", "regime_changed", "gap", "clock_changed", "demand_changed", "context_changed")


def _fields(row, keys):
    return {key: row[key] for key in keys if key in row}


def _payload(row):
    raw = row.get("payload")
    item = json.loads(raw) if isinstance(raw, str) else {}
    return item if isinstance(item, dict) else {}


def _row(table, row):
    # Explicit projection: never export entry/session IDs, regime metadata,
    # arbitrary warnings, fan-trial metadata or episode payload/context.
    if table == "decisions":
        payload = _payload(row)
        result = _fields(row, ("at", "regime"))
        if payload.get("mode") in ("heat", "cool", "off"):
            result["mode"] = payload["mode"]
        rooms = payload.get("rooms", {})
        targets = payload.get("room_targets", {})
        if isinstance(rooms, dict) and isinstance(targets, dict):
            result["rooms"] = {
                room: {
                    **_fields(reading, ("temperature", "low", "high", "confirmed_low", "confirmed_high")),
                    **_fields(targets.get(room) or {}, ("low_f", "high_f", "target_f", "heat_requested", "cool_requested")),
                } for room, reading in rooms.items() if isinstance(reading, dict)
            }
        return result
    if table == "observations":
        result = _fields(row, ("room", "regime", "source", "at", "received", "value"))
        stamp = _payload(row).get("timestamp_kind")
        if stamp in _TIMESTAMP_KINDS:
            result["timestamp_kind"] = stamp
        return result
    if table == "summaries":
        return _fields(row, ("room", "regime", "source", "width", "bucket", "n", "total",
                             "squares", "minimum", "maximum", "first", "last", "longest_gap"))
    result = _fields(row, ("room", "regime", "start", "last", "end", "decisions", "minimum", "maximum"))
    if row.get("demand") in ("heat", "cool", "idle", "unknown"):
        result["demand"] = row["demand"]
    if row.get("end_reason") in _END_REASONS:
        result["end_reason"] = row["end_reason"]
    return result


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode())


def _read_for_tool(path, entry_id, start, end, limit):
    data = read_history(path, start, end, limit, entry_id=entry_id, include_regimes=False)
    result = _fields(data, ("schema", "start", "end", "semantics", "limitations", "availability"))
    result["availability"]["coverage_complete"] = False
    for table in _TABLES:
        result[table] = []
        result[table + "_truncated"] = data[table + "_truncated"]
    used = _size(result) + 2  # Two availability booleans may need an extra byte.
    for table in _TABLES:
        for item in data[table]:
            row = _row(table, item)
            size = _size(row) + 1  # Account conservatively for the separating comma.
            if used + size > MAX_BYTES:
                result[table + "_truncated"] = True
                break
            result[table].append(row)
            used += size
    result["availability"]["raw_available"] = bool(result["observations"])
    result["availability"]["summaries_available"] = bool(result["summaries"])
    return result


class ThermalHistoryTool(llm.Tool):
    name = "GetThermalHistory"
    title = "Get Home Climate thermal history"
    description = (
        "Read stored whole-home Home Climate temperatures, source reports, and planned demand. "
        "Read-only; no equipment actions. Supply a UTC Unix timestamp interval of at most seven days."
    )
    integration = DOMAIN
    annotations = llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)
    parameters = schema.Schema({
        schema.Required("start", description="Inclusive UTC Unix seconds"): schema.Any(int, float),
        schema.Required("end", description="Exclusive UTC Unix seconds"): schema.Any(int, float),
        schema.Optional("limit", default=20, description="Rows per history category (1–50)"):
            schema.All(int, schema.Range(min=1, max=MAX_ROWS)),
    })

    def __init__(self, api):
        self.api = api

    async def async_call(self, hass, tool_input, llm_context):
        await self.api.async_check_access(llm_context)
        try:
            args = self.parameters(tool_input.tool_args)
            start, end, limit = args["start"], args["end"], args["limit"]
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end) or type(limit) is not int
                    or not 0 <= start < end <= time.time() or end - start > MAX_SECONDS):
                raise ValueError
        except (schema.Invalid, KeyError, TypeError, ValueError, OverflowError):
            return llm.ToolResult(data={"message": "Use finite past UTC Unix start/end seconds, a span at most seven days, and an integer limit of 1–50."}, error=True)
        if _QUERY_JOB in hass.data:
            return llm.ToolResult(data={"message": "A Home Climate thermal history query is already running."}, error=True)
        history = self.api.runtime.thermal_storage
        job = hass.async_add_executor_job(_read_for_tool, history.path, self.api.entry.entry_id, start, end, limit)
        hass.data[_QUERY_JOB] = job

        def finished(done):
            if hass.data.get(_QUERY_JOB) is done:
                hass.data.pop(_QUERY_JOB, None)
            if not done.cancelled():
                done.exception()

        job.add_done_callback(finished)
        try:
            result = await asyncio.shield(job)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return llm.ToolResult(data={"message": "Thermal history is unavailable or the query exceeded its read budget."}, error=True)
        await self.api.async_check_access(llm_context)
        return llm.ToolResult(data=result)


class ThermalHistoryAPI(llm.API):
    def __init__(self, hass, entry, runtime):
        # HA 2026.11 adds discovery-level permissions; the explicit context
        # gate also applies on the supported 2026.10 baseline.
        options = {"requires_admin": True} if hasattr(llm.API, "requires_admin") else {}
        super().__init__(hass=hass, id=API_ID, name="Home Climate thermal history", **options)
        self.entry, self.runtime = entry, runtime

    def _available(self):
        return (self.entry.options.get(CONF_THERMAL_MCP, self.entry.data.get(CONF_THERMAL_MCP, False)) is True
                and self.hass.data.get(DOMAIN) is self.runtime and not self.runtime.stop_event.is_set()
                and self.runtime.thermal_storage is not None)

    async def async_check_access(self, context):
        user_id = context.context.user_id if context.context else None
        if context.platform != "mcp_server" or not user_id:
            raise Unauthorized(context=context.context)
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_active or not user.is_admin:
            raise Unauthorized(context=context.context)
        if not self._available():
            raise HomeAssistantError("Home Climate thermal history MCP is unavailable")

    async def async_get_api_instance(self, llm_context):
        # HA can discover this opt-in API across integrations; it provides tools
        # only to the MCP transport, never an assistant or voice pipeline.
        if llm_context.platform != "mcp_server":
            return llm.APIInstance(self, "", llm_context, [])
        try:
            await self.async_check_access(llm_context)
        except (Unauthorized, HomeAssistantError):
            # Especially on 2026.10, a non-admin querying a merged MCP URL
            # must not lose other tool sets because ours is unauthorized.
            return llm.APIInstance(self, "", llm_context, [])
        return llm.APIInstance(self, "Read-only stored Home Climate evidence; not physical HVAC runtime.",
                               llm_context, [ThermalHistoryTool(self)])


def register(hass, entry, runtime):
    return llm.async_register_api(hass, ThermalHistoryAPI(hass, entry, runtime))
