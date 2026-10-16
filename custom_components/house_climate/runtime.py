"""Run the existing synchronous engine outside HA's event loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
import logging
import threading
import time
from types import SimpleNamespace

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_STATUS
from .controller.__main__ import run_loop
from .controller.ha import HAError

_LOGGER = logging.getLogger(__name__)


def controller_entity_ids(config):
    """The complete input set, including legacy helpers and safety interlocks."""
    entities = {
        *config.climate_entities, *config.fans.values(),
        *(probe.entity_id for room in config.zones.values() for probe in room.probes),
        config.entity("enabled"), config.entity("heater_owned"),
        config.entity("heartbeat", "input_datetime"),
        config.entity("status", "input_text"),
        config.entity("auxiliary_watchdog", "automation"),
        config.entity("auxiliary_probe_safe", "binary_sensor"),
    }
    if config.legacy:
        from .controller.legacy import NUMBERS, TIMES, TOGGLES, helper
        for domain, names in (("input_number", NUMBERS), ("input_datetime", TIMES),
                              ("input_boolean", TOGGLES)):
            entities.update(helper(name, domain) for name in names)
    return frozenset(entities)


class NativeAPI:
    """Read configured cached states; marshal service calls onto HA's loop."""

    def __init__(self, hass, live, stop_event, config):
        self.hass = hass
        self.live = live
        self.stop_event = stop_event
        self.ownership_entity = config.entity("heater_owned")
        self.entity_ids = controller_entity_ids(config)

    def request(self, path, body=None):
        if body is not None and not self.live:
            raise HAError("Shadow mode forbids all service calls")
        if threading.get_ident() == self.hass.loop_thread_id:
            raise RuntimeError("NativeAPI synchronous methods must run in the worker")
        if path == "states" and body is None:
            return self._read_states()
        timeout = 2 if self.stop_event.is_set() else 10
        deadline = time.monotonic() + timeout
        future = asyncio.run_coroutine_threadsafe(
            self._async_request(path, body, deadline), self.hass.loop
        )
        try:
            return future.result(timeout=timeout + 1)
        except FutureTimeout:
            future.cancel()
            raise HAError(f"Home Assistant native operation timed out: {path}") from None

    def _read_states(self):
        # StateMachine.get is HA's synchronous, direct cached-state accessor.
        # Do not call async_all from this worker or queue a whole-house copy on
        # HA's loop. Keep original report times so an unresponsive HA cannot
        # make old probe readings appear fresh.
        result = []
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is not None:
                result.append({
                    "entity_id": state.entity_id, "state": state.state,
                    "attributes": dict(state.attributes),
                    "last_changed": state.last_changed.isoformat(),
                    "last_updated": state.last_updated.isoformat(),
                    "last_reported": state.last_reported.isoformat(),
                })
        return result

    async def _async_request(self, path, body, deadline):
        # Cancelling the worker's future is not enough: a queued coroutine can
        # start before its cancellation callback when the loop resumes.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HAError(f"Home Assistant native operation expired before dispatch: {path}")

        if path == "config" and body is None:
            return {"time_zone": self.hass.config.time_zone,
                    "unit_system": {"temperature": self.hass.config.units.temperature_unit}}
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
        async with asyncio.timeout(remaining):
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
        self.api = NativeAPI(hass, live, self.stop_event, config)
        self.thread = None
        self.finished = None
        self.status = {"event": "waiting_for_home_assistant"}
        self.fault_count = 0
        self.last_fault = {}

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
        if event in ("fault", "worker_failed"):
            self.fault_count += 1
            self.last_fault = {
                "last_error": data.get("error"),
                "last_fault_at": datetime.now(timezone.utc).isoformat(),
                "fault_count": self.fault_count,
            }
        self.status = {"event": event, **data, **self.last_fault}
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
