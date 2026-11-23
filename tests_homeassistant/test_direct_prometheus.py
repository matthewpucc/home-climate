"""Exercise HA's actual view wrapper and Prometheus's real sample parser."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView, KEY_AUTHENTICATED, KEY_HASS
from prometheus_client.parser import text_string_to_metric_families

from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate import prometheus_metrics as metrics


class OrdinaryHAView(HomeAssistantView):
    url = "/ordinary-ha-view"
    name = "test:ordinary_view"

    async def get(self, _request):
        return web.Response(text="ordinary")


class DirectPrometheusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.now = datetime.now(timezone.utc)
        self.token = "5" * 64  # Synthetic test fixture, never a deployment credential.
        self.digest = hashlib.sha256(self.token.encode()).digest()
        self.runtime = self.new_runtime()

        @web.middleware
        async def transport(request, handler):
            # Simulate the HTTPS scheme *after* trusted transport middleware;
            # the view must never honor client-supplied forwarding headers.
            if request.headers.get("X-For-Test") == "secure":
                request = request.clone(scheme="https")
            request[KEY_AUTHENTICATED] = request.headers.get("Authorization") == "Bearer ha-fixture"
            return await handler(request)

        self.app = web.Application(middlewares=[transport])
        self.app[KEY_HASS] = self.hass
        registrar = Mock(side_effect=lambda view: view.register(self.hass, self.app, self.app.router))
        self.hass.http = SimpleNamespace(register_view=registrar)
        OrdinaryHAView().register(self.hass, self.app, self.app.router)
        self.hass.data[DOMAIN] = self.runtime
        self.unbind = metrics.bind(self.hass, self.runtime, self.digest)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.unbind()
        await self.client.close()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def new_runtime(self):
        return SimpleNamespace(
            config=SimpleNamespace(heads={"office": "climate.private_head"},
                                   zones={"office": {}}, poll_seconds=15),
            controls=SimpleNamespace(mode_diagnostics=SimpleNamespace(values=lambda entity: {
                "command_mode_code": 2, "command_path_code": 1, "command_outcome_code": 2,
                "command_dispatched_at": 21, "command_finished_at": 22.5,
                "feedback_mode_code": 19, "feedback_attribution_code": True,
                "feedback_at": float("nan"), "handoff_mode_code": 4,
                "unknown_private_field": 123} if entity == "climate.private_head" else {})),
            status={"event": "live", "timestamp": self.now.isoformat(), "mode": "heat",
                    "decision_interval_seconds": 90, "decision_remaining_seconds": 80,
                    "room_targets": {"office": {"heat_requested": True, "cool_requested": False}},
                    "head_constraints": {"office": {"cool": {"kind": "house_limit", "reason": "secret text"}}},
                    "warnings": ["private warning"]},
            stop_event=Event(),
        )

    def headers(self, token=None):
        return {"X-For-Test": "secure", metrics.TOKEN_HEADER: self.token if token is None else token}

    async def test_auth_is_route_scoped_header_only_and_tls_only(self):
        cases = [({"X-For-Test": "secure"}, 401), (self.headers("a" * 64), 401),
                 ({metrics.TOKEN_HEADER: self.token, "X-Forwarded-Proto": "https"}, 403),
                 ({"X-For-Test": "secure", "Authorization": "Bearer ha-fixture"}, 401),
                 ([("X-For-Test", "secure"), (metrics.TOKEN_HEADER, self.token),
                   (metrics.TOKEN_HEADER, self.token)], 401)]
        for headers, expected in cases:
            with self.subTest(expected=expected, headers=headers):
                response = await self.client.get(metrics.URL, headers=headers)
                self.assertEqual(response.status, expected)
                self.assertNotIn(self.token, await response.text())
        response = await self.client.get(metrics.URL + "?ignored=x", headers=self.headers())
        self.assertEqual(response.status, 400)
        response = await self.client.post(metrics.URL, headers=self.headers())
        self.assertEqual(response.status, 405)
        response = await self.client.get(OrdinaryHAView.url, headers=self.headers())
        self.assertEqual(response.status, 401)
        response = await self.client.get(OrdinaryHAView.url, headers={"Authorization": "Bearer ha-fixture"})
        self.assertEqual(response.status, 200)

    async def test_auth_rejection_logs_are_private_bounded_and_not_returned(self):
        cases = [
            ({"X-For-Test": "secure"}, "missing header"),
            (self.headers("8" * 64), "digest mismatch"),
            (self.headers("PRIVATE log-escape attempt"), "invalid format"),
            ([("X-For-Test", "secure"), (metrics.TOKEN_HEADER, self.token),
              (metrics.TOKEN_HEADER, "PRIVATE duplicate")], "duplicate header"),
        ]
        with self.assertLogs(metrics.__name__, "WARNING") as captured:
            # The missing-header probe arrives first; it must not suppress
            # the later actionable mismatch or any of the other categories.
            for _ in range(6):
                for headers, _ in cases:
                    response = await self.client.get(metrics.URL, headers=headers)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(response.headers["WWW-Authenticate"],
                                     'HomeClimateMetrics realm="Home Climate metrics"')
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(await response.read(), b"")
            response = await self.client.get(metrics.URL, headers=self.headers("c" * 64))
            self.assertEqual(response.status, 401)  # New input, same category.
            self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 200)
        self.assertEqual([record.getMessage() for record in captured.records],
                         [f"Home Climate metrics authentication rejected: {reason}" for _, reason in cases])

    async def test_auth_rejection_log_scope_resets_for_replacement_not_for_success(self):
        no_header = {"X-For-Test": "secure"}
        with self.assertLogs(metrics.__name__, "WARNING") as captured:
            self.assertEqual((await self.client.get(metrics.URL, headers=no_header)).status, 401)
            self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 200)
            self.assertEqual((await self.client.get(metrics.URL, headers=no_header)).status, 401)

            old_unbind = self.unbind
            other = self.new_runtime()
            self.hass.data[DOMAIN] = other
            self.unbind = metrics.bind(self.hass, other, self.digest)
            old_unbind()
            self.assertEqual((await self.client.get(metrics.URL, headers=no_header)).status, 401)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["Home Climate metrics authentication rejected: missing header"] * 2)

    async def test_reload_rotation_unload_and_stale_runtime(self):
        other = self.new_runtime()
        rotated = "a" * 64
        self.runtime.stop_event.set()
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 404)
        self.hass.data[DOMAIN] = other
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 404)
        old_unbind = self.unbind
        self.unbind = metrics.bind(self.hass, other, hashlib.sha256(rotated.encode()).digest())
        old_unbind()  # Unloading old runtime cannot revoke the new binding.
        self.hass.http.register_view.assert_called_once()
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 401)
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers(rotated))).status, 200)
        self.unbind()
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers(rotated))).status, 404)

    async def test_format_observed_vs_planned_and_no_scrape_writes(self):
        with (patch.object(type(self.hass.services), "async_call", side_effect=AssertionError("equipment call")),
              patch("sqlite3.connect", side_effect=AssertionError("database access")),
              patch("builtins.open", side_effect=AssertionError("file access"))):
            response = await self.client.get(metrics.URL, headers=self.headers())
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
            body = await response.text()
        self.assertTrue(body.endswith("\n"))
        parsed = {sample.name: [] for family in text_string_to_metric_families(body) for sample in family.samples}
        for family in text_string_to_metric_families(body):
            for sample in family.samples:
                parsed[sample.name].append(sample)
        self.assertEqual(parsed["house_climate_selected_mode_code"][0].value, 2)
        demands = {sample.labels["mode"]: sample.value for sample in parsed["house_climate_room_demand"]}
        self.assertEqual(demands, {"heat": 1, "cool": 0})
        self.assertEqual(parsed["house_climate_head_command_handler_duration_seconds"][0].value, 1.5)
        self.assertIn('house_climate_head_blocker_code{head="office",mode="cool"} 2', body)
        self.assertNotIn("feedback_", body)
        for private in ("private_head", "private warning", "secret text", self.token):
            self.assertNotIn(private, body)

    async def test_max_cardinality_and_unknown_states(self):
        self.runtime.config.heads = {f"head{n}": f"climate.unit{n}" for n in range(100)}
        self.runtime.config.zones = {f"room{n}": {} for n in range(80)}
        self.runtime.config.zones['bad"\nlabel'] = {}
        self.runtime.status["room_targets"] = {
            key: {"heat_requested": False, "cool_requested": True} for key in self.runtime.config.zones}
        body = metrics.render(self.runtime, self.now.timestamp() + 1)
        count = Counter(sample.name for family in text_string_to_metric_families(body) for sample in family.samples)
        self.assertEqual(count["house_climate_head_blocker_code"], 32)
        self.assertEqual(count["house_climate_room_demand"], 64)
        self.assertNotIn("bad", body)
        stale = metrics.render(self.runtime, self.now.timestamp() + 600)
        self.assertIn("house_climate_worker_snapshot_fresh 0", stale)
        self.assertNotIn("house_climate_room_demand", stale)
        self.assertNotIn("house_climate_selected_mode_code", stale)
        self.assertIn("house_climate_decision_age_seconds", stale)

    async def test_optional_config_requires_exact_digest_and_does_not_log_contents(self):
        path = Path(self.directory.name, "test.json")
        self.assertIsNone(metrics.load_digest(path))
        for invalid in ("malformed private text", json.dumps({"token_sha256": 0}),
                        json.dumps({"token_sha256": self.digest.hex(), "extra": self.token}),
                        " " * 257):
            path.write_text(invalid)
            with self.assertLogs(metrics.__name__, "WARNING") as log:
                self.assertIsNone(metrics.load_digest(path))
            self.assertNotIn("private text", " ".join(log.output))
            self.assertNotIn(self.token, " ".join(log.output))
        path.write_text(json.dumps({"token_sha256": self.digest.hex()}))
        self.assertEqual(metrics.load_digest(path), self.digest)

    async def test_cold_start_without_http_never_reads_config(self):
        self.hass.http = None
        entry = SimpleNamespace(async_on_unload=Mock())
        with patch.object(metrics, "load_digest", side_effect=AssertionError("unexpected config read")):
            await metrics.async_attach(self.hass, entry, self.runtime)
            self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await self.hass.async_block_till_done()
        entry.async_on_unload.assert_called_once()

    async def test_cold_start_registers_only_after_http_is_ready(self):
        # Stop the test's pre-mounted fixture and exercise real startup events.
        server = self.hass.http
        self.unbind()
        self.hass.data.pop(metrics._VIEW_KEY)
        server.register_view = Mock()
        self.hass.http = None
        entry = SimpleNamespace(async_on_unload=Mock())
        Path(self.directory.name, metrics.CONFIG_FILE).write_text(
            json.dumps({"token_sha256": self.digest.hex()}))
        await metrics.async_attach(self.hass, entry, self.runtime)
        server.register_view.assert_not_called()
        self.assertNotIn(metrics._ACTIVE_KEY, self.hass.data)
        self.hass.http = server
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()
        server.register_view.assert_called_once()
        self.assertIs(self.hass.data[metrics._ACTIVE_KEY].runtime, self.runtime)
        self.unbind = entry.async_on_unload.call_args.args[0]

    async def test_unloading_before_http_is_ready_cancels_binding(self):
        server = self.hass.http
        self.hass.http = None
        entry = SimpleNamespace(async_on_unload=Mock())
        with patch.object(metrics, "load_digest", side_effect=AssertionError("unexpected config read")):
            await metrics.async_attach(self.hass, entry, self.runtime)
            entry.async_on_unload.call_args.args[0]()
            self.hass.http = server
            self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await self.hass.async_block_till_done()

    async def test_runtime_replacement_during_credential_read_cannot_bind_stale(self):
        entry = SimpleNamespace(async_on_unload=Mock())

        async def read_then_replace(*_args):
            self.hass.data[DOMAIN] = self.new_runtime()
            return self.digest

        with patch.object(self.hass, "async_add_executor_job", side_effect=read_then_replace):
            await metrics.async_attach(self.hass, entry, self.runtime)
        entry.async_on_unload.assert_not_called()
        self.assertEqual((await self.client.get(metrics.URL, headers=self.headers())).status, 404)
