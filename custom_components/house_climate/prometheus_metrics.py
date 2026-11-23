"""Read-only, route-scoped exposition of cached Home Climate evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import re

from aiohttp import web

from homeassistant.core import callback
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN
from .mode_diagnostics import FIELDS, MODE, _valid

URL = "/api/house_climate/metrics"
CONFIG_FILE = "home_climate_metrics.json"
TOKEN_HEADER = "X-Home-Climate-Metrics-Token"
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_VIEW_KEY = "house_climate_metrics_view"
_ACTIVE_KEY = "house_climate_metrics_binding"
_HEX = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z0-9_]{1,48}")
BLOCKER = {"missing": 1, "house_limit": 2, "comfort_limit": 3, "comfort_rearm": 4}
_LOGGER = logging.getLogger(__name__)


def load_digest(path):
    """Read only the optional, small config file; never log its contents."""
    try:
        with open(path, "rb") as config:
            raw = config.read(257)
        if len(raw) > 256:
            raise ValueError
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {"token_sha256"}
                or not isinstance(data["token_sha256"], str) or not _HEX.fullmatch(data["token_sha256"])):
            raise ValueError
        return bytes.fromhex(data["token_sha256"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _LOGGER.warning("Home Climate metrics disabled: invalid metrics credential configuration")
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _age(stamp, now):
    try:
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is not None:
            age = now - when.timestamp()
            if age >= 0:
                return age
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None


def _labels(values, cap):
    return sorted(name for name in values if isinstance(name, str) and _LABEL.fullmatch(name))[:cap]


def render(runtime, now):
    """Use existing runtime snapshots, only fixed-cardinality numeric families."""
    families = {}

    def gauge(name, description, value, **labels):
        if not _number(value):
            return
        rows = families.setdefault("house_climate_" + name, (description, []))[1]
        suffix = "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}" if labels else ""
        rows.append(f"house_climate_{name}{suffix} {value}")

    status = runtime.status
    age = _age(status.get("timestamp"), now)
    observed = status.get("event") in {"live", "shadow"}
    fresh = bool(observed and age is not None and age <= max(30, 3 * runtime.config.poll_seconds))
    gauge("worker_snapshot_fresh", "Fresh live or preview worker snapshot.", int(fresh))
    if observed and age is not None:
        gauge("worker_snapshot_age_seconds", "Age of the last worker plan snapshot.", age)
        interval, remaining = status.get("decision_interval_seconds"), status.get("decision_remaining_seconds")
        if _number(interval) and _number(remaining) and 0 <= remaining <= interval:
            gauge("decision_age_seconds", "Estimated age of full comfort decision including worker snapshot age.",
                  age + interval - remaining)
    if fresh:
        gauge("worker_live", "Whether the latest fresh worker snapshot is live rather than Preview.",
              int(status["event"] == "live"))
        selected = status.get("mode")
        if selected in ("off", "heat", "cool"):
            gauge("selected_mode_code", "Latest planned shared direction, not equipment acknowledgement.",
                  MODE[selected])
        rooms = status.get("room_targets", {})
        for room in _labels(runtime.config.zones, 32):
            demand = rooms.get(room, {})
            for mode in ("heat", "cool"):
                value = demand.get(f"{mode}_requested")
                if type(value) is bool:
                    gauge("room_demand", "Latest planned room demand, not equipment operation.",
                          int(value), room=room, mode=mode)

    diagnostics = getattr(runtime.controls, "mode_diagnostics", None)
    for head in _labels(runtime.config.heads, 16):
        if fresh:
            for mode in ("heat", "cool"):
                constraint = status.get("head_constraints", {}).get(head, {}).get(mode, {})
                blocker = BLOCKER.get(constraint.get("kind"))
                if blocker is None:
                    if mode in status.get("unavailable_mode_requests", {}).get(head, {}):
                        blocker = 5
                    elif status.get("mode") == mode and head in status.get("inhibited_heads", {}):
                        blocker = 6
                    else:
                        blocker = 0
                gauge("head_blocker_code", "Structured blocker in the fresh plan; zero is none recorded.",
                      blocker, head=head, mode=mode)
        if diagnostics is None:
            continue
        row = diagnostics.values(runtime.config.heads[head])
        for field in FIELDS:
            value = row.get(field)
            if _valid(field, value):
                name = field.removesuffix("_at") + "_timestamp_seconds" if field.endswith("_at") else field
                gauge(f"head_{name}", "Latest Home Climate mode journal " + field.replace("_", " ") + ".",
                      value, head=head)
        for phase in ("command", "handoff"):
            dispatch, finished = row.get(phase + "_dispatched_at"), row.get(phase + "_finished_at")
            if (_valid("dispatched_at", dispatch) and _valid("finished_at", finished)
                    and dispatch <= finished):
                gauge(f"head_{phase}_handler_duration_seconds", "Duration from HA dispatch to handler finish.",
                      finished - dispatch, head=head)
    lines = []
    for name, (description, rows) in families.items():
        lines += [f"# HELP {name} {description}", f"# TYPE {name} gauge", *rows]
    return "\n".join(lines) + "\n"


class HomeClimateMetricsView(HomeAssistantView):
    url = URL
    name = "api:house_climate:metrics"
    # Only this route uses an independent, route-scoped credential. The HA
    # bearer middleware, IP bans, trusted-proxy checks and other views remain
    # unchanged; a Home Assistant bearer token never authenticates this view.
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        headers = {"Cache-Control": "no-store"}
        binding = self.hass.data.get(_ACTIVE_KEY)
        if (binding is None or self.hass.data.get(DOMAIN) is not binding[0]
                or binding[0].stop_event.is_set()):
            return web.Response(status=404, headers=headers)
        if not request.secure:
            return web.Response(status=403, headers=headers)
        if request.query_string:
            return web.Response(status=400, headers=headers)
        presented = request.headers.getall(TOKEN_HEADER, [])
        if (len(presented) != 1 or not _HEX.fullmatch(presented[0])
                or not hmac.compare_digest(hashlib.sha256(presented[0].encode("ascii")).digest(), binding[1])):
            headers["WWW-Authenticate"] = 'HomeClimateMetrics realm="Home Climate metrics"'
            return web.Response(status=401, headers=headers)
        headers["Content-Type"] = _CONTENT_TYPE
        body = render(binding[0], datetime.now(timezone.utc).timestamp())
        return web.Response(text=body, headers=headers)


@callback
def bind(hass, runtime, digest):
    """Mount once; a retired config entry can never expose another runtime."""
    if _VIEW_KEY not in hass.data:
        try:
            hass.http.register_view(HomeClimateMetricsView(hass))
        except (RuntimeError, ValueError):
            # Optional telemetry cannot interrupt a controller setup or leak
            # configuration details into logs.
            _LOGGER.warning("Home Climate metrics disabled: could not register scrape route")
            return lambda: None
        hass.data[_VIEW_KEY] = True
    binding = (runtime, digest)
    hass.data[_ACTIVE_KEY] = binding

    @callback
    def unbind():
        if hass.data.get(_ACTIVE_KEY) is binding:
            hass.data.pop(_ACTIVE_KEY)

    return unbind

async def async_attach(hass, entry, runtime):
    """Bind if HTTP exists; otherwise retry after HA's cold-start setup barrier."""

    def server_if_active():
        if (not hass.is_stopping and hass.data.get(DOMAIN) is runtime
                and not runtime.stop_event.is_set()):
            return getattr(hass, "http", None)
        return None

    async def attach(_hass):
        server = server_if_active()
        if server is None:
            return
        digest = await hass.async_add_executor_job(load_digest, hass.config.path(CONFIG_FILE))
        if digest is not None and server_if_active() is server:
            entry.async_on_unload(bind(hass, runtime, digest))

    if getattr(hass, "http", None) is not None:
        await attach(hass)
    else:
        entry.async_on_unload(async_at_started(hass, attach))
