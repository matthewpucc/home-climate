"""Run the existing synchronous engine outside HA's event loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import logging
import threading
from types import SimpleNamespace

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_STATUS
from .controller.__main__ import run_loop
from .controller.ha import HAError

_LOGGER = logging.getLogger(__name__)


class NativeAPI:
    """Marshal state reads and service calls onto HA's loop from one worker."""

    def __init__(self, hass, live, stop_event, ownership_entity="input_boolean.hp_heater_owned"):
        self.hass = hass
        self.live = live
        self.stop_event = stop_event
        self.ownership_entity = ownership_entity

    def request(self, path, body=None):
        if body is not None and not self.live:
            raise HAError("Shadow mode forbids all service calls")
        if threading.get_ident() == self.hass.loop_thread_id:
            raise RuntimeError("NativeAPI synchronous methods must run in the worker")
        timeout = 2 if self.stop_event.is_set() else 10
        future = asyncio.run_coroutine_threadsafe(
            self._async_request(path, body, timeout), self.hass.loop
        )
        try:
            return future.result(timeout=timeout + 1)
        except FutureTimeout:
            future.cancel()
            raise HAError("Home Assistant native operation timed out") from None

    async def _async_request(self, path, body, timeout):
        if path == "config" and body is None:
            return {"time_zone": self.hass.config.time_zone,
                    "unit_system": {"temperature": self.hass.config.units.temperature_unit}}
        if path == "states" and body is None:
            return [
                {"entity_id": state.entity_id, "state": state.state,
                 "attributes": dict(state.attributes),
                 "last_changed": state.last_changed.isoformat(),
                 "last_updated": state.last_updated.isoformat(),
                 "last_reported": state.last_reported.isoformat()}
                for state in self.hass.states.async_all()
            ]
        pieces = path.split("/")
        if len(pieces) != 3 or pieces[0] != "services" or body is None:
            raise HAError("Unsupported native HA operation")
        if not self.live:
            raise HAError("Shadow mode forbids all service calls")
        _, domain, service = pieces
        if self.stop_event.is_set():
            safe_stop = (
                (domain == "climate" and service == "set_hvac_mode" and body.get("hvac_mode") == "off")
                or (domain == "fan" and service == "turn_off")
                or (domain == "input_boolean" and service == "turn_off"
                    and body.get("entity_id") == self.ownership_entity)
            )
            if not safe_stop:
                raise HAError("Controller is stopping; only shutdown commands are allowed")
        async with asyncio.timeout(timeout):
            await self.hass.services.async_call(domain, service, body, blocking=True)
        return []

    def states(self):
        return {state["entity_id"]: state for state in self.request("states")}

    def service(self, domain, service, **data):
        return self.request(f"services/{domain}/{service}", data)


class ControllerRuntime:
    def __init__(self, hass, config, live):
        self.hass = hass
        self.config = config
        self.live = live
        self.stop_event = threading.Event()
        self.api = NativeAPI(hass, live, self.stop_event, config.entity("heater_owned"))
        self.thread = None
        self.finished = None
        self.status = {"event": "waiting_for_home_assistant"}

    @callback
    def async_start(self, _hass=None):
        if self.thread is not None or self.stop_event.is_set():
            return
        self.finished = self.hass.loop.create_future()
        self.thread = threading.Thread(
            target=self._run, name="house-climate-controller", daemon=True
        )
        self.thread.start()

    def _report(self, event, **data):
        self.hass.loop.call_soon_threadsafe(self._async_report, event, data)

    @callback
    def _async_report(self, event, data):
        self.status = {"event": event, **data}
        if event in ("fault", "worker_failed"):
            _LOGGER.error("Controller %s: %s", event, data.get("error"))
        else:
            _LOGGER.debug("Controller %s: %s", event, data)
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    def _run(self):
        error = None
        try:
            args = SimpleNamespace(live=self.live, once=False, snapshot=None, at=None)
            run_loop(args, self.config, ha_client=self.api,
                     stop_event=self.stop_event, event_callback=self._report)
        except Exception as exc:
            error = str(exc)
            _LOGGER.exception("Controller worker stopped unexpectedly")
        finally:
            self.hass.loop.call_soon_threadsafe(self._async_finished, error)

    @callback
    def _async_finished(self, error):
        self._async_report("worker_failed" if error else "stopped",
                           {"error": error} if error else {})
        if not self.finished.done():
            self.finished.set_result(None)

    async def async_stop(self, _event=None):
        self.stop_event.set()
        if self.finished is None:
            self._async_report("stopped", {})
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self.finished), timeout=45)
        except TimeoutError:
            # Keep the runtime registered; a reload must never start a second worker.
            self._async_report("worker_failed", {"error": "Controller shutdown timed out"})
            return False
        await self.hass.async_add_executor_job(self.thread.join)
        return True
