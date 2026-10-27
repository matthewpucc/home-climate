"""Run the existing synchronous engine outside HA's event loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from dataclasses import dataclass, replace
import logging
import threading
import time
from types import SimpleNamespace

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .const import SIGNAL_STATUS
from .controller.__main__ import run_loop
from .controller.ha import HAActivationDeferred, HAError
from .recovery import RecoveryManager

_LOGGER = logging.getLogger(__name__)

UNLOAD_ACK_TIMEOUT_SECONDS = 120
SHUTDOWN_RECHECK_SECONDS = 15
NATIVE_HEARTBEAT_DISPATCH_SECONDS = 5
HEARTBEAT_WAIT_REASON = "Waiting for Home Assistant to confirm the controller heartbeat; new activations are deferred"


@dataclass(frozen=True)
class HeartbeatRequest:
    token: int
    generation: int
    queued_at: float
    deadline: float


@dataclass(frozen=True)
class HeartbeatState:
    state: str = "idle"
    request: HeartbeatRequest | None = None
    blocked: bool = False
    reason: str | None = None


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


class NativeActivationDispatch:
    """Serialize worker abandonment with HA-loop dispatch for one positive call."""

    def __init__(self, deadline):
        self.deadline = deadline
        self._lock = threading.Lock()
        self._dispatched = False
        self._abandoned = False

    def abandon_if_queued(self):
        with self._lock:
            if self._dispatched:
                return False
            self._abandoned = True
            return True

    def begin(self):
        with self._lock:
            if self._abandoned or time.monotonic() >= self.deadline:
                self._abandoned = True
                raise HAActivationDeferred(
                    "Home Assistant has not dispatched this command; retrying after responsiveness returns")
            # Mark possible execution immediately before the actual service call.
            # From here a timeout remains an ambiguous actuator failure.
            self._dispatched = True


class NativeAPI:
    """Read configured cached states; marshal service calls onto HA's loop."""

    def __init__(self, hass, live, stop_event, config):
        self.hass = hass
        self.live = live
        self.stop_event = stop_event
        self.ownership_entity = config.entity("heater_owned")
        self.heartbeat_entity = config.entity("heartbeat", "input_datetime")
        self.status_entity = config.entity("status", "input_text")
        self.entity_ids = controller_entity_ids(config)
        self.controls = None
        self.power_guard = None
        self.heartbeat_notify = None
        self._heartbeat_lock = threading.Lock()
        self._heartbeat = HeartbeatState()
        self._heartbeat_token = 0

    def request(self, path, body=None):
        if body is not None and not self.live:
            raise HAError("Shadow mode forbids all service calls")
        if threading.get_ident() == self.hass.loop_thread_id:
            raise RuntimeError("NativeAPI synchronous methods must run in the worker")
        if path == "states" and body is None:
            return self._read_states()
        if self.controls is not None and body is not None:
            if path == "services/input_text/set_value" and body.get("entity_id") == self.status_entity:
                # Native sensors already receive the worker's report. This sink
                # has no operation and must not queue a synchronous round trip.
                return []
            if (path == "services/input_datetime/set_datetime"
                    and body.get("entity_id") == self.heartbeat_entity):
                if self.stop_event.is_set():
                    raise HAError("Controller is stopping; heartbeat ignored")
                self._queue_heartbeat()
                return []
        activation = self._is_activation(path, body)
        generation = None
        if self.controls is not None and activation:
            issue = self.heartbeat_issue
            if issue:
                raise HAActivationDeferred(issue)
            generation = self.controls._snapshot["generation"]
        timeout = 2 if self.stop_event.is_set() else 10
        deadline = time.monotonic() + timeout
        dispatch = NativeActivationDispatch(deadline) if self.controls is not None and activation else None
        future = asyncio.run_coroutine_threadsafe(
            self._async_request(path, body, deadline, generation=generation, dispatch=dispatch), self.hass.loop
        )
        try:
            return future.result(timeout=timeout + 1)
        except FutureTimeout:
            # Cancellation alone cannot prevent a queued coroutine from starting
            # first when HA's loop resumes. The shared ticket closes that race.
            deferred = dispatch is not None and dispatch.abandon_if_queued()
            future.cancel()
            if deferred:
                raise HAActivationDeferred(
                    "Home Assistant has not dispatched this command; retrying after responsiveness returns") from None
            raise HAError(f"Home Assistant native operation timed out: {path}") from None

    @staticmethod
    def _is_activation(path, body):
        if body is None:
            return False
        pieces = path.split("/")
        return (len(pieces) == 3 and pieces[0] == "services"
                and pieces[1] in ("climate", "fan")
                and not (pieces[2] == "turn_off"
                         or (pieces[2] == "set_hvac_mode" and body.get("hvac_mode") == "off")
                         or (pieces[2] == "set_percentage" and body.get("percentage") == 0)))

    def _expire_heartbeat_locked(self, now):
        state = self._heartbeat
        if state.request and now >= state.request.deadline:
            state = replace(state, state="overdue", blocked=True, reason=HEARTBEAT_WAIT_REASON)
            self._heartbeat = state
        return state

    @property
    def heartbeat_diagnostics(self):
        instant = time.monotonic()
        with self._heartbeat_lock:
            state = self._expire_heartbeat_locked(instant)
        controls = self.controls
        snapshot = controls._snapshot if controls else {}
        lease = snapshot.get("heartbeat_monotonic", 0)
        remaining = max(0.0, controls.config.heartbeat_timeout_seconds - (instant - lease)) if snapshot.get("owned") else None
        auxiliary_remaining = (max(0.0, controls.config.auxiliary_heartbeat_timeout_seconds - (instant - lease))
                               if snapshot.get("owned") and controls.config.heater_entity else None)
        return {
            "state": state.state, "blocked": state.blocked, "reason": state.reason,
            "pending_seconds": max(0.0, instant - state.request.queued_at) if state.request else None,
            "lease_remaining_seconds": remaining,
            "auxiliary_lease_remaining_seconds": auxiliary_remaining,
            "auxiliary_blocked": snapshot.get("auxiliary_heartbeat_blocked", False),
            "auxiliary_reason": snapshot.get("auxiliary_heartbeat_reason"),
        }

    @property
    def heartbeat_issue(self):
        state = self.heartbeat_diagnostics
        return state["reason"] if state["blocked"] else None

    def _queue_heartbeat(self):
        """Coalesce worker pulses without granting or extending a lease here."""
        controls = self.controls
        generation = controls._snapshot["generation"]
        instant = time.monotonic()
        with self._heartbeat_lock:
            state = self._expire_heartbeat_locked(instant)
            if state.request and state.request.generation == generation:
                return  # An overdue request keeps its original immutable deadline.
            self._heartbeat_token += 1
            request = HeartbeatRequest(
                self._heartbeat_token, generation, instant,
                instant + NATIVE_HEARTBEAT_DISPATCH_SECONDS)
            self._heartbeat = replace(state, state="pending", request=request)
        try:
            self.hass.loop.call_soon_threadsafe(self._async_confirm_heartbeat, request)
        except RuntimeError:
            with self._heartbeat_lock:
                if self._heartbeat.request is request:
                    self._heartbeat = HeartbeatState("overdue", blocked=True, reason=HEARTBEAT_WAIT_REASON)

    @callback
    def _async_confirm_heartbeat(self, request):
        """Only this HA-loop callback may acknowledge the queued worker pulse."""
        with self._heartbeat_lock:
            state = self._expire_heartbeat_locked(time.monotonic())
            if state.request is not request:
                return
        controls = self.controls
        error = None
        if time.monotonic() >= request.deadline:
            error = HEARTBEAT_WAIT_REASON
        elif self.stop_event.is_set() or controls is None:
            error = "stopped"
        else:
            try:
                controls.confirm_heartbeat(generation=request.generation)
            except HAActivationDeferred as exc:
                error = str(exc)
            except Exception:
                # Do not strand an unacknowledged request if a custom callback
                # or notification fails. Retry remains blocked and bounded by
                # the last HA-confirmed ownership lease.
                error = HEARTBEAT_WAIT_REASON
                _LOGGER.debug("Native heartbeat confirmation failed", exc_info=True)
        with self._heartbeat_lock:
            if self._heartbeat.request is not request:
                return
            if error is None:
                self._heartbeat = HeartbeatState("confirmed")
            elif self.stop_event.is_set() or not controls or not controls.enabled:
                self._heartbeat = HeartbeatState("stopped", blocked=True)
            else:
                self._heartbeat = HeartbeatState(
                    "overdue", blocked=True, reason=HEARTBEAT_WAIT_REASON)
        if self.heartbeat_notify is not None:
            try:
                self.heartbeat_notify()
            except Exception:
                _LOGGER.debug("Native heartbeat status notification failed", exc_info=True)

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
        if self.controls is not None:
            states = self.controls.overlay({state["entity_id"]: state for state in result})
            if self.power_guard is not None and self.power_guard.blocked:
                key = self.controls.config.entity("auxiliary_probe_safe", "binary_sensor")
                states[key] = {**states[key], "state": "off"}
            return [{"entity_id": entity, **state} for entity, state in states.items()]
        return result

    async def _async_request(self, path, body, deadline, *, generation=None, dispatch=None):
        # Cancelling the worker's future is not enough: a queued coroutine can
        # start before its cancellation callback when the loop resumes.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if dispatch is not None and dispatch.abandon_if_queued():
                raise HAActivationDeferred(
                    "Home Assistant has not dispatched this command; retrying after responsiveness returns")
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
        controls = self.controls
        if controls and controls.accepts(domain, service, body.get("entity_id")):
            async with asyncio.timeout(remaining):
                await controls.async_service(domain, service, body)
            return []
        activation = (domain in ("climate", "fan")
                      and not (service == "turn_off"
                               or (service == "set_hvac_mode" and body.get("hvac_mode") == "off")
                               or (service == "set_percentage" and body.get("percentage") == 0)))
        if controls and activation:
            issue = self.heartbeat_issue
            with self._heartbeat_lock:
                confirming = self._heartbeat.request is not None
            if issue or confirming:
                raise HAActivationDeferred(issue or HEARTBEAT_WAIT_REASON)
            controls.check_activation(body.get("entity_id"), body, generation=generation)
        if activation and self.power_guard is not None:
            # Check on HA's loop immediately before dispatch, not against an old
            # worker snapshot. Off requests always remain allowed.
            self.power_guard.check_activation(body.get("entity_id"), body)
        if dispatch is not None:
            dispatch.begin()
        if controls and activation:
            controls.inflight += 1
            if body.get("entity_id") == controls.config.heater_entity:
                controls.auxiliary_activation_started(body)
        auxiliary_off = bool(
            controls and body.get("entity_id") == controls.config.heater_entity
            and service == "set_hvac_mode" and body.get("hvac_mode") == "off")
        off_receipt = controls.auxiliary_off_started() if auxiliary_off else None
        try:
            async with asyncio.timeout(remaining):
                await self.hass.services.async_call(domain, service, body, blocking=True)
        except (HAError, TimeoutError):
            raise
        except Exception as exc:
            raise HAError(f"Home Assistant native operation failed: {path}: {exc}") from exc
        else:
            if auxiliary_off:
                controls.auxiliary_off_finished(off_receipt)
        finally:
            if controls and activation:
                controls.inflight -= 1
                if body.get("entity_id") == controls.config.heater_entity:
                    controls.auxiliary_activation_finished()
                controls._schedule_check()
        return []

    def states(self):
        return {state["entity_id"]: state for state in self.request("states")}

    def service(self, domain, service, **data):
        return self.request(f"services/{domain}/{service}", data)


class ControllerRuntime:
    def __init__(self, hass, config, live, *, house_policy=False, power_bindings=None):
        self.hass = hass
        self.config = config
        self.live = live
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.controls = None
        self.comfort = None
        self.house_policy = house_policy
        self.power_bindings = power_bindings
        self.power_guard = None
        self.api = NativeAPI(hass, live, self.stop_event, config)
        self.api.heartbeat_notify = self._async_heartbeat_changed
        self.thread = None
        self.finished = None
        self.status = {"event": "waiting_for_home_assistant"}
        self.fault_count = 0
        self.last_fault = {}
        self._controls_ready = None
        self._remove_climate_listener = None
        self._logged_fault = None
        self._logged_stop = None
        self._logged_stop_failures = None
        self._logged_unload_blockers = None
        self._logged_heartbeat_issue = None
        self._stop_lock = asyncio.Lock()
        self._shutdown_progress = asyncio.Event()
        self._options_reload_task = None
        self._configuration_reload_pending = False
        self._configuration_reload_reason = None
        self.recovery = RecoveryManager(hass, self)

    async def async_setup_controls(self, entry_id):
        from .comfort import RoomComfort
        self.comfort = RoomComfort(self.hass, self.config, entry_id,
                                   self._async_comfort_changed, self.wake_event)
        try:
            await self.comfort.async_load()
            if not self.config.legacy:
                from .controls import NativeControls
                self.controls = NativeControls(self.hass, self.config, self.live, entry_id,
                                               self._async_controls_changed)
                self.api.controls = self.controls
                await self.controls.async_load()
            if self.house_policy:
                from .power_guard import HeaterPowerGuard
                self.power_guard = HeaterPowerGuard(
                    self.hass, bindings=self.power_bindings, live=self.live, entry_id=entry_id,
                    notify=self._async_power_guard_changed)
                self.api.power_guard = self.power_guard
                if self.controls is not None:
                    self.controls.power_guard = self.power_guard
                await self.power_guard.async_load()
        except BaseException:
            if self.power_guard is not None:
                await self.power_guard.async_close()
            await self.comfort.async_close()
            raise

    def _update_power_guard_status(self):
        if self.power_guard is not None:
            self.status["power_guard"] = self.power_guard.diagnostics

    @callback
    def _async_power_guard_changed(self):
        self._update_power_guard_status()
        self.wake_event.set()
        if self.controls is not None:
            self.controls._schedule_check()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    def _update_heartbeat_status(self):
        diagnostics = self.api.heartbeat_diagnostics
        self.status["heartbeat"] = diagnostics
        issue = diagnostics["reason"] if diagnostics["blocked"] else None
        if issue and issue != self._logged_heartbeat_issue:
            _LOGGER.warning("Controller heartbeat confirmation delayed: %s", issue)
            self._logged_heartbeat_issue = issue
        elif diagnostics["state"] == "confirmed" and self._logged_heartbeat_issue:
            _LOGGER.info("Controller heartbeat confirmation recovered")
            self._logged_heartbeat_issue = None
            self.wake_event.set()
        elif diagnostics["state"] == "stopped":
            self._logged_heartbeat_issue = None

    @callback
    def _async_heartbeat_changed(self):
        self._update_heartbeat_status()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_comfort_changed(self):
        self.wake_event.set()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_controls_changed(self):
        if self.controls is None:
            return
        self._shutdown_progress.set()
        previous = self.status.get("control_enabled")
        if self.status.get("event") == "live" and not self.controls.enabled:
            self.status = {"event": "stopping", **self.last_fault}
        self.status.update(control_enabled=self.controls.enabled,
                           control_block_reason=self.controls.hint,
                           pending_fans=list(self.controls.pending_fans))
        ready = self.controls._snapshot.get("enabled", False)
        if previous != self.controls.enabled or ready != self._controls_ready:
            self.wake_event.set()
        self._controls_ready = ready
        self._update_heartbeat_status()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_recovery_changed(self):
        self.status["integration_recovery"] = self.recovery.diagnostics
        self.wake_event.set()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_climate_changed(self, event):
        if self.stop_event.is_set():
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        unavailable = (None, "unknown", "unavailable")
        old_state, new_state = old.state if old else None, new.state if new else None
        old_modes = frozenset(old.attributes.get("hvac_modes") or ()) if old else frozenset()
        new_modes = frozenset(new.attributes.get("hvac_modes") or ()) if new else frozenset()
        availability_changed = (old_state in unavailable) != (new_state in unavailable)
        confirming_off = (
            new_state == "off"
            and new.attributes.get("hvac_action") not in ("heating", "cooling")
            and (event.data["entity_id"] in self.status.get("waiting_devices", ())
                 or self.status.get("phase") == "stopping")
        )
        # Wake for recovery evidence, without turning ordinary temperature and
        # setpoint updates into extra control cycles.
        if availability_changed or old_modes != new_modes or confirming_off:
            self.wake_event.set()

    @callback
    def async_start(self, _hass=None):
        if self.thread is not None or self.stop_event.is_set():
            return
        if self.controls:
            self.controls.async_start()
        if self.power_guard is not None:
            self.power_guard.async_start()
        self._remove_climate_listener = async_track_state_change_event(
            self.hass, self.config.climate_entities, self._async_climate_changed
        )
        self.finished = self.hass.loop.create_future()
        self.thread = threading.Thread(
            target=self._run, name="home-climate-controller", daemon=True
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
        if self.controls and event == "live" and not self.controls.enabled:
            event = "stopping" if self.controls.owned else "shadow"
        self.status = {"event": event, **data, **self.last_fault}
        self._update_power_guard_status()
        if self.controls:
            self.status.update(control_enabled=self.controls.enabled,
                               control_block_reason=self.controls.hint,
                               pending_fans=list(self.controls.pending_fans))
        self._update_reload_status()
        self._update_heartbeat_status()
        self.recovery.observe(self.status)
        self.status["integration_recovery"] = self.recovery.diagnostics
        if event in ("fault", "worker_failed"):
            incident = (event, data.get("error"))
            if incident != self._logged_fault:
                # An active-loop fault is immediately followed by a reasoned
                # all-device stop request. Log that complete context once.
                log = _LOGGER.debug if data.get("shutdown_requested") else _LOGGER.error
                log("Controller %s: %s", event, data.get("error"))
                self._logged_fault = incident
        elif event == "stopping" and data.get("stop_scope") == "all_configured_devices":
            entities = data.get("stop_entities") or []
            incident = (data.get("stop_cause"), data.get("reason"), tuple(entities))
            if incident != self._logged_stop:
                log = _LOGGER.error if data.get("forced_stop") else _LOGGER.info
                log("Controller-wide Off requested: %s; cause=%s; all %d configured devices: %s",
                    data.get("reason"), data.get("stop_cause"), len(entities), ", ".join(entities))
                self._logged_stop = incident
        elif event == "stopped" and data.get("failures"):
            incident = (data.get("reason"), tuple(data["failures"]))
            if incident != self._logged_stop_failures:
                _LOGGER.warning(
                    "Controller shutdown incomplete: %s; cause: %s",
                    ", ".join(data["failures"]), data.get("reason", "Controller shutdown"))
                self._logged_stop_failures = incident
        else:
            _LOGGER.debug("Controller %s: %s", event, data)
        if event in ("live", "shadow"):
            self._logged_fault = None
        if event == "live":
            self._logged_stop = self._logged_stop_failures = None
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    def _run(self):
        error = None
        try:
            args = SimpleNamespace(live=self.live, once=False, snapshot=None, at=None)
            run_loop(args, self.config, ha_client=self.api,
                     stop_event=self.stop_event, wake_event=self.wake_event, event_callback=self._report,
                     settings_provider=self.comfort.settings if self.comfort else None)
        except Exception as exc:
            error = str(exc)
            _LOGGER.exception("Controller worker stopped unexpectedly")
        finally:
            self.hass.loop.call_soon_threadsafe(self._async_finished, error)

    @callback
    def _async_finished(self, error):
        if self.controls and error:
            self.controls.pause(f"Control paused: controller worker stopped: {error}")
        self._async_report("worker_failed" if error else "stopped",
                           {"error": error} if error else {"reason": "Controller worker exited"})
        if not self.finished.done():
            self.finished.set_result(None)

    def _warn_unload_deferred(self, *, worker_pending=False, recovery_pending=False):
        # Diagnostic collection/delivery cannot change cleanup or its outcome.
        try:
            controls = self.controls
            owned = tuple(controls.owned) if controls else ()
            waiting = []
            for entity in owned:
                if not entity.startswith("climate."):
                    continue
                state = self.hass.states.get(entity)
                if (state is None or state.state != "off"
                        or state.attributes.get("hvac_action") in ("heating", "cooling")):
                    waiting.append(entity)
            inflight = controls.inflight if controls else 0
            problem = controls.problem if controls else None
            incident = (tuple(waiting), owned, inflight, worker_pending, recovery_pending, problem)
            if incident == self._logged_unload_blockers:
                return
            self._logged_unload_blockers = incident
            _LOGGER.warning(
                "Controller unload deferred; waiting for Off acknowledgement: %s; "
                "in-flight activations=%d; owning integration reload pending=%s; "
                "worker still stopping=%s; retained ownership: %s; shutdown issue: %s",
                ", ".join(waiting) or "none", inflight, recovery_pending, worker_pending,
                ", ".join(owned) or "none", problem or "none",
            )
        except Exception:
            pass

    def _update_reload_status(self):
        self.status["configuration_reload_pending"] = self._configuration_reload_pending
        self.status["configuration_reload_reason"] = self._configuration_reload_reason

    @callback
    def async_schedule_options_reload(self, entry_id):
        """Drain the old controller before asking HA to enter its unload state.

        HA treats a False unload result as nonrecoverable. A disconnected head
        must keep a configuration save pending, rather than strand the entry in
        FAILED_UNLOAD after the watchdog eventually receives its Off report.
        """
        if self._options_reload_task and not self._options_reload_task.done():
            return
        self._configuration_reload_pending = True
        self._configuration_reload_reason = "Configuration saved; waiting for devices to stop before reload"
        self._update_reload_status()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)
        self._options_reload_task = self.hass.async_create_background_task(
            self._async_reload_when_stopped(entry_id), "Home Climate saved configuration reload",
            eager_start=False,
        )

    async def _async_wait_shutdown_progress(self, timeout=None):
        if timeout is None:
            timeout = SHUTDOWN_RECHECK_SECONDS
        try:
            await asyncio.wait_for(self._shutdown_progress.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def _async_reload_when_stopped(self, entry_id):
        try:
            while not self.hass.is_stopping:
                if await self.async_stop():
                    if self.hass.is_stopping:
                        return
                    self._configuration_reload_reason = "Applying saved configuration"
                    self._update_reload_status()
                    async_dispatcher_send(self.hass, SIGNAL_STATUS)
                    if not await self.hass.config_entries.async_reload(entry_id):
                        _LOGGER.error("Saved configuration could not reload after controller shutdown")
                    return
                self._shutdown_progress.clear()
                await self._async_wait_shutdown_progress()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Saved configuration reload stopped unexpectedly")
            self._configuration_reload_reason = "Saved configuration reload failed; check the Home Assistant log"
        finally:
            self._configuration_reload_pending = False
            self._options_reload_task = None
            self._update_reload_status()
            async_dispatcher_send(self.hass, SIGNAL_STATUS)

    async def _async_close_controls(self, wait_for_ack):
        if self.controls is None:
            return True
        closed = await self.controls.async_close()
        if closed or not wait_for_ack:
            return closed
        # Only wait for acknowledgement here. Cleanup services retain their own
        # bounds and are never cancelled by an acknowledgement-wait timeout.
        deadline = self.hass.loop.time() + UNLOAD_ACK_TIMEOUT_SECONDS
        while not closed:
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                return False
            self._shutdown_progress.clear()
            await self._async_wait_shutdown_progress(min(SHUTDOWN_RECHECK_SECONDS, remaining))
            closed = await self.controls.async_close()
        return True

    async def async_stop(self, _event=None, *, wait_for_ack=False):
        # An external unload supersedes a pending options reload. The managed
        # reload itself re-enters this method from HA and must not cancel itself.
        task = self._options_reload_task
        if wait_for_ack and task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._stop_lock:
            return await self._async_stop(wait_for_ack)

    async def _async_stop(self, wait_for_ack):
        self.stop_event.set()
        self.wake_event.set()
        if self.comfort:
            await self.comfort.async_close()
        recovery_closed = await self.recovery.async_close()
        if self.controls:
            await self.controls.async_set_enabled(False)
        if self.finished is None:
            self._async_report("stopped", {})
        else:
            try:
                await asyncio.wait_for(asyncio.shield(self.finished), timeout=45)
            except TimeoutError:
                # Keep the runtime registered; a reload must never start a second worker.
                self._warn_unload_deferred(worker_pending=True, recovery_pending=not recovery_closed)
                self._async_report("worker_failed", {"error": "Controller shutdown timed out"})
                return False
            await self.hass.async_add_executor_job(self.thread.join)
        controls_closed = await self._async_close_controls(wait_for_ack)
        closed = controls_closed and recovery_closed
        if closed:
            if self.power_guard is not None:
                guard_closed = await self.power_guard.async_close()
                if guard_closed is False:
                    # A delayed outlet-On is an activation, even if an older Off
                    # is cached. Keep this runtime until compensation is proven.
                    closed = False
                    _LOGGER.warning("Controller unload deferred; waiting for heater outlet restoration to be safely stopped")
                    self._update_power_guard_status()
            if closed:
                self._logged_unload_blockers = None
        else:
            self._warn_unload_deferred(recovery_pending=not recovery_closed)
        if closed and self._remove_climate_listener:
            self._remove_climate_listener()
            self._remove_climate_listener = None
        if not recovery_closed:
            self._async_report("worker_failed", {
                "error": "Owning heat-pump integration reload is still stopping; unload deferred"
            })
        return closed
