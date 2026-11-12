"""Run the existing synchronous engine outside HA's event loop."""
from __future__ import annotations


import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR
import logging
import math
import threading
import time
from types import SimpleNamespace

from homeassistant.core import callback
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.helpers.temperature import display_temp
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .handoff import notification_disposition
from .const import SIGNAL_STATUS
from .controller.__main__ import run_loop
from .controller.ha import HAActivationDeferred, HAError, HAStopping
from .controller.config import Settings
from .controller.auxiliary_target import AuxiliaryTarget, trusted_auxiliary_state
from .controller.window_authorization import WindowAuthorization, check_window_authorization
from .recovery import RecoveryManager
from .repair_gate import RepairGate

_LOGGER = logging.getLogger(__name__)

UNLOAD_ACK_TIMEOUT_SECONDS = 120
WORKER_STOP_TIMEOUT_SECONDS = 45
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


@dataclass
class ServiceOperation:
    entity: str | None
    activation: bool
    started: bool = False
    controls: object | None = None
    observation: dict | None = None


def controller_entity_ids(config):
    """The complete input set, including legacy helpers and safety interlocks."""
    entities = {
        *config.climate_entities, *config.fans.values(),
        *(entity for room in config.zones.values() for probe in room.probes
          for entity in (probe.entity_id, getattr(probe, "observation_time_entity", None),
                         getattr(probe, "require_active_entity", None)) if entity),
        *(entity for room in config.zones.values() for entity in room.windows),
        *(probe.entity_id for probe in config.outdoor_probes),
        *(probe.observation_time_entity for probe in config.outdoor_probes if probe.observation_time_entity),
        config.entity("enabled"), config.entity("heater_owned"),
        config.entity("heartbeat", "input_datetime"),
        config.entity("auxiliary_authorization", "input_datetime"),
        config.entity("auxiliary_manual"),
        config.entity("auxiliary_stop_limit_f", "input_number"),
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

    def __init__(self, deadline, window_authorization=None, auxiliary_target=None, airflow_authorization=None):
        self.deadline = deadline
        self.window_authorization = window_authorization
        self.auxiliary_target = auxiliary_target
        self.airflow_authorization = airflow_authorization
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
        self.config = config
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
        self.repair_gate = RepairGate([*config.heads.values(), *config.fans.values()])
        self._airflow_authorizations = {}
        self._fixed_airflow_bindings = {}
        self._airflow_calibration_error = None
        self._service_jobs = {}
        self._uncertain_service_entities = set()
        self.service_notify = None
        self.settings_provider = None
        self.bluetooth_freshness = None
        # Volatile controller metadata survives a worker/executor replacement
        # on this runtime, without becoming a persisted emergency-guard latch.
        self.auxiliary_startup_failure = None
        self._auxiliary_target_snapshot = None

    def request(self, path, body=None, *, window_authorization=None,
                activation_deadline=None, auxiliary_target=None, airflow_authorization=None):
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
                    raise HAStopping("Controller is stopping; heartbeat ignored")
                self._queue_heartbeat()
                return []
        activation = self._is_activation(path, body)
        actuator = bool(body is not None and path.startswith(("services/climate/", "services/fan/")))
        repair_epoch = (self.repair_gate.capture(body.get("entity_id"), activation=activation)
                        if actuator else None)
        generation = self.controls.command_generation(body.get("entity_id")) if self.controls is not None and actuator else None
        if self.controls is not None and activation:
            issue = self.heartbeat_issue
            if issue:
                raise HAActivationDeferred(issue)
            generation = self.controls.command_generation(body.get("entity_id"))
        timeout = 2 if self.stop_event.is_set() else 10
        deadline = time.monotonic() + timeout
        bounded_activation = activation and activation_deadline is not None
        if bounded_activation:
            deadline = min(deadline, self._activation_deadline(activation_deadline))
            if deadline <= time.monotonic():
                raise HAActivationDeferred("Activation deadline expired before dispatch")
        dispatch = (NativeActivationDispatch(deadline, window_authorization, auxiliary_target, airflow_authorization)
                    if activation and (self.controls is not None or window_authorization is not None
                                       or bounded_activation or auxiliary_target is not None
                                       or airflow_authorization is not None) else None)
        future = asyncio.run_coroutine_threadsafe(
            self._async_request(path, body, deadline, generation=generation, dispatch=dispatch,
                                repair_epoch=repair_epoch), self.hass.loop
        )
        try:
            wait = max(0, deadline - time.monotonic()) if bounded_activation else timeout + 1
            return future.result(timeout=wait)
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
        auxiliary_lease = snapshot.get("auxiliary_authorized_at", 0)
        remaining = max(0.0, controls.config.heartbeat_timeout_seconds - (instant - lease)) if snapshot.get("owned") else None
        auxiliary_remaining = (max(0.0, controls.config.auxiliary_heartbeat_timeout_seconds - (instant - auxiliary_lease))
                               if snapshot.get("owned") and controls.config.heater_entity else None)
        return {
            "state": state.state, "blocked": state.blocked, "reason": state.reason,
            "pending_seconds": max(0.0, instant - state.request.queued_at) if state.request else None,
            "lease_remaining_seconds": remaining,
            "auxiliary_lease_remaining_seconds": auxiliary_remaining,
            "auxiliary_blocked": snapshot.get("auxiliary_heartbeat_blocked", False),
            "auxiliary_authorized_at": snapshot.get("auxiliary_authorized_wall", 0),
            "independent_auxiliary_authorization": True,
            "controller_timeout_seconds": self.config.heartbeat_timeout_seconds,
            "auxiliary_timeout_seconds": self.config.auxiliary_heartbeat_timeout_seconds,
            "auxiliary_authorization_state": ("manual" if snapshot.get("auxiliary_manual") else
                "valid" if auxiliary_remaining and not snapshot.get("auxiliary_heartbeat_blocked") else "expired"),
            "auxiliary_reason": snapshot.get("auxiliary_heartbeat_reason"),
        }

    @property
    def heartbeat_issue(self):
        state = self.heartbeat_diagnostics
        return state["reason"] if state["blocked"] else None

    def authorize_auxiliary(self, plan, states):
        """Issue a separate authorization only for a currently eligible plan."""
        heater = self.config.heater_entity
        if not heater or not (plan.heater_on or plan.auxiliary_fan_only):
            return False
        if states.get(heater, {}).get("attributes", {}).get("home_climate_manual"):
            return False
        mode = "heat" if plan.heater_on else "fan_only"
        try:
            self.request("services/input_datetime/set_datetime", {
                "entity_id": self.config.entity("auxiliary_authorization", "input_datetime"),
                "timestamp": datetime.now(timezone.utc).timestamp(),
                **({"hvac_mode": mode, "stop_limit_f": getattr(plan, "auxiliary_policy", {}).get("stop_target_f"),
                    "context": getattr(plan, "auxiliary_policy", {}).get("context"),
                    "stop_revision": states.get(heater, {}).get("attributes", {}).get("home_climate_auxiliary_stop_revision", 0)}
                   if self.controls else {})})
        except (HAError, RuntimeError, ValueError) as exc:
            plan.warnings.append(f"Auxiliary authorization withheld: {exc}")
            return False
        return True

    def release_control(self, *, reason, cause, forced=False):
        """The native owner performs its durable terminal handoff on HA's loop."""
        if self.controls is None:
            return False
        self.request("services/input_boolean/turn_off", {
            "entity_id": self.ownership_entity, "reason": reason, "cause": cause,
            "forced": forced})
        return True

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

    def _read_states_without_controls(self):
        # StateMachine.get is HA's synchronous, direct cached-state accessor.
        # Do not call async_all from this worker or queue a whole-house copy on
        # HA's loop. Keep original report times so an unresponsive HA cannot
        # make old probe readings appear fresh.
        result = []
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is not None:
                airflow_identity = ()
                if any(f.entity_id == entity_id and f.airflow for f in self.config.fan_controls.values()):
                    from .airflow_adapter import identity
                    airflow_identity = identity(self.hass, entity_id)
                result.append({
                    "entity_id": state.entity_id, "state": state.state,
                    "attributes": dict(state.attributes),
                    "last_changed": state.last_changed.isoformat(),
                    "last_updated": state.last_updated.isoformat(),
                    "last_reported": state.last_reported.isoformat(),
                    **({"bluetooth_temperature_receipt": receipt}
                       if self.bluetooth_freshness and (receipt := self.bluetooth_freshness.evidence(entity_id, state)) else {}),
                    **({"integration_repair": repair} if (repair := self.repair_gate.overlay(entity_id)) else {}),
                    **({"airflow_identity": airflow_identity} if airflow_identity else {}),
                })
        return result

    def _read_states(self):
        result = self._read_states_without_controls()
        if self.controls is not None:
            states = self.controls.overlay({state["entity_id"]: state for state in result})
            heater = self.config.heater_entity
            if self.power_guard is not None and heater in states:
                states[heater] = {**states[heater], "attributes": {
                    **states[heater].get("attributes", {}),
                    "home_climate_auxiliary_blocked": self.power_guard.blocked,
                    "home_climate_auxiliary_fan_blocked": getattr(self.power_guard, "fan_blocked", self.power_guard.blocked),
                    "home_climate_auxiliary_reason": getattr(self.power_guard, "diagnostics", {}).get("reason")}}
            if self.power_guard is not None and self.power_guard.blocked:
                key = self.controls.config.entity("auxiliary_probe_safe", "binary_sensor")
                states[key] = {**states[key], "state": "off"}
            return [{"entity_id": entity, **state} for entity, state in states.items()]
        return result

    async def _async_request(self, path, body, deadline, *, generation=None, dispatch=None,
                             repair_epoch=None):
        # Cancelling the worker's future is not enough: a queued coroutine can
        # start before its cancellation callback when the loop resumes.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if dispatch is not None and dispatch.abandon_if_queued():
                raise HAActivationDeferred(
                    "Home Assistant has not dispatched this command; retrying after responsiveness returns")
            raise HAError(f"Home Assistant native operation expired before dispatch: {path}")

        if path.startswith("auxiliary_target/") and body is None:
            return self._auxiliary_target(float(path.split("/", 1)[1]))
        if path == "config" and body is None:
            return {"time_zone": self.hass.config.time_zone,
                    "unit_system": {"temperature": self.hass.config.units.temperature_unit}}
        pieces = path.split("/")
        if len(pieces) != 3 or pieces[0] != "services" or body is None:
            raise HAError("Unsupported native HA operation")
        if not self.live:
            raise HAError("Shadow mode forbids all service calls")
        _, domain, service = pieces
        if domain == "hass_dyson":
            raise HAError("Device-targeted airflow must use the typed bound-purifier adapter")
        if service == "set_airflow_profile" and (domain != "fan" or dispatch is None
                                                 or dispatch.airflow_authorization is None):
            raise HAError("Airflow profile requires a typed current authorization")
        if self.stop_event.is_set():
            safe_stop = (
                (domain == "climate" and service == "set_hvac_mode" and body.get("hvac_mode") == "off")
                or (domain == "fan" and service == "turn_off")
                or (domain == "input_boolean" and service == "turn_off"
                    and body.get("entity_id") == self.ownership_entity)
            )
            if not safe_stop:
                raise HAStopping("Controller is stopping; only shutdown commands are allowed")
        controls = self.controls
        if controls and controls.accepts(domain, service, body.get("entity_id")):
            async with asyncio.timeout(remaining):
                await controls.async_service(domain, service, body)
            return []
        activation = (domain in ("climate", "fan")
                      and not (service == "turn_off"
                               or (service == "set_hvac_mode" and body.get("hvac_mode") == "off")
                               or (service == "set_percentage" and body.get("percentage") == 0)))
        if domain in ("climate", "fan"):
            self.repair_gate.check(body.get("entity_id"), repair_epoch, activation=activation)
        # The actual service has a separate lifetime from this caller. A worker
        # timeout/cancellation must not erase a dispatched device operation.
        operation = ServiceOperation(body.get("entity_id"), activation, controls=controls)
        if (controls and body.get("entity_id") == controls.config.heater_entity
                and domain == "climate" and service == "set_hvac_mode" and body.get("hvac_mode") == "off"):
            operation.observation = controls.observations.record(
                controls, "off_requested", source="worker", request_generation=generation)
        task = self.hass.async_create_background_task(
            self._async_service_job(path, domain, service, body, deadline, activation,
                                    generation, dispatch, repair_epoch, operation),
            "Home Climate device service", eager_start=False)
        self._service_jobs[task] = operation
        if controls is not None:
            # Registered queued Off jobs also outlive their callers. They must
            # drain before an old ownership lease can be released or reclaimed.
            controls.service_inflight += 1
        task.add_done_callback(self._service_job_done)
        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
            if not done:
                raise TimeoutError()
            task.result()
        except (HAError, TimeoutError):
            raise
        except Exception as exc:
            raise HAError(f"Home Assistant native operation failed: {path}: {exc}") from exc
        return []

    def _service_job_done(self, task):
        operation = self._service_jobs.pop(task, None)
        if operation is not None and operation.observation is not None:
            if task.cancelled():
                operation.controls.observations.record(
                    operation.controls, "off_cancelled", command=operation.observation)
            elif not operation.started:
                operation.controls.observations.record(
                    operation.controls, "off_not_dispatched", command=operation.observation)
        if operation is not None and operation.controls is not None:
            operation.controls.service_inflight -= 1
            operation.controls._schedule_check()
        if task.cancelled() and operation and operation.started and operation.activation:
            self._uncertain_service_entities.add(operation.entity)
            if self.controls is not None:
                self.controls.activation_job_uncertain(operation.entity)
        if not task.cancelled():
            task.exception()  # Retrieve errors even if the original caller timed out.
        if self.service_notify is not None:
            self.service_notify()

    def service_jobs(self, entities):
        entities = set(entities)
        native = tuple(task for task, operation in self._service_jobs.items()
                       if operation.entity in entities and not task.done())
        watchdog = self.controls.stop_jobs(entities) if self.controls is not None else ()
        return (*native, *watchdog)

    def uncertain_service_jobs(self, entities):
        uncertain = self._uncertain_service_entities.intersection(entities)
        if self.controls is not None:
            uncertain |= self.controls._uncertain_device_jobs.intersection(entities)
        return sorted(uncertain)

    async def _async_service_job(self, path, domain, service, body, deadline, activation,
                                  generation, dispatch, repair_epoch, operation):
        if activation and self.stop_event.is_set():
            raise HAStopping("Controller is stopping; activation was not dispatched")
        if time.monotonic() >= deadline:
            if dispatch is not None and dispatch.abandon_if_queued():
                raise HAActivationDeferred("Head service expired before actual dispatch")
            raise HAError(f"Home Assistant native operation expired before dispatch: {path}")
        controls = self.controls
        if domain == "fan":
            from .configuration import purifier_identity_issue
            if issue := purifier_identity_issue(self.hass, self.config, body.get("entity_id"),
                                                activation=activation, controls=controls):
                raise HAError(issue)
        if domain in ("climate", "fan"):
            self.repair_gate.check(body.get("entity_id"), repair_epoch, activation=activation)
            if (controls is not None and generation is not None
                    and generation != controls.command_generation(body.get("entity_id"))):
                raise HAActivationDeferred("Device service belongs to an earlier controller lease (ownership changed)")
            if controls is not None and not activation:
                controls.check_stop(body.get("entity_id"))
        if activation:
            if body.get("entity_id") in self.config.heads.values():
                state = self.hass.states.get(body["entity_id"])
                mode = body.get("hvac_mode") or (state.state if state is not None else None)
                if (state is None or state.state in ("unknown", "unavailable")
                        or not {"off", mode} <= set(state.attributes.get("hvac_modes") or ())):
                    raise HAActivationDeferred("Head mode unavailable in fresh metadata at dispatch")
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
        if activation and dispatch is not None and dispatch.window_authorization is not None:
            states = {state["entity_id"]: state for state in self._read_states()}
            config = replace(self.config, timezone=self.hass.config.time_zone)
            now = datetime.now(timezone.utc)
            settings = (self.settings_provider(states, config, now)
                        if self.settings_provider else Settings.read(states, config))
            checked = check_window_authorization(dispatch.window_authorization, config,
                body.get("entity_id"), body.get("hvac_mode"), states, now, settings,
                monotonic_now=time.monotonic(), climate_unit=self.hass.config.units.temperature_unit)
            if not checked:
                raise HAActivationDeferred(checked.reason)
        if activation and dispatch is not None and dispatch.auxiliary_target is not None:
            selected = dispatch.auxiliary_target
            try:
                current = self._auxiliary_target(selected.ceiling_f)
            except HAError as exc:
                raise HAActivationDeferred(str(exc)) from exc
            if body.get("entity_id") != self.config.heater_entity or current != selected:
                raise HAActivationDeferred("Auxiliary native target capabilities changed before dispatch")
            if service == "set_temperature" and body.get("temperature") != current.command:
                raise HAActivationDeferred("Auxiliary command differs from its supported target")
            if service == "set_hvac_mode" and body.get("hvac_mode") == "heat":
                state = self.hass.states.get(self.config.heater_entity)
                if state is None or not current.matches({"attributes": state.attributes}):
                    raise HAActivationDeferred("Auxiliary target changed before explicit Heat dispatch")
        if activation and self.stop_event.is_set():
            raise HAStopping("Controller is stopping; activation was not dispatched")
        actual_domain, actual_service, actual_body = domain, service, body
        if dispatch is not None and dispatch.airflow_authorization is not None:
            from .airflow_adapter import validate_dispatch
            actual_domain, actual_service, actual_body = validate_dispatch(
                self, dispatch.airflow_authorization, service, body)
        if dispatch is not None:
            dispatch.begin()
        if controls and activation:
            controls.inflight += 1
            controls.head_activation_started(body.get("entity_id"))
            if body.get("entity_id") == controls.config.heater_entity:
                controls.auxiliary_activation_started(body)
        auxiliary_off = bool(
            controls and body.get("entity_id") == controls.config.heater_entity
            and service == "set_hvac_mode" and body.get("hvac_mode") == "off")
        off_receipt = controls.auxiliary_off_started() if auxiliary_off else None
        head_off_receipt = (controls.head_off_started(body.get("entity_id"))
                            if controls and domain == "climate" and service == "set_hvac_mode"
                            and body.get("hvac_mode") == "off" else None)
        try:
            operation.started = True
            if (auxiliary_off and self.power_guard is not None
                    and getattr(self.power_guard, "emergency_fallback_enabled", False) is True):
                operation.observation = controls.observations.record(
                    controls, "off_delegated", command=operation.observation)
                if not await self.power_guard.async_request_heater_off():
                    raise HAError("Heater shutdown remains unconfirmed")
                controls.observations.record(controls, "off_delegate_completed", command=operation.observation)
            else:
                if auxiliary_off:
                    operation.observation = controls.observations.record(
                        controls, "off_dispatched", command=operation.observation)
                await self.hass.services.async_call(actual_domain, actual_service, actual_body, blocking=True)
                if auxiliary_off:
                    controls.observations.record(controls, "off_completed",
                                                 command=operation.observation, receipt=off_receipt)
        except (HAError, TimeoutError):
            if auxiliary_off:
                controls.observations.record(controls, "off_failed", command=operation.observation)
            raise
        except Exception as exc:
            if auxiliary_off:
                controls.observations.record(controls, "off_failed", command=operation.observation)
            raise HAError(f"Home Assistant native operation failed: {path}: {exc}") from exc
        else:
            if activation and body.get("entity_id") == self.config.heater_entity:
                self.capture_auxiliary_target()
            if auxiliary_off:
                controls.auxiliary_off_finished(off_receipt)
            if controls and head_off_receipt is not None:
                controls.head_off_finished(body.get("entity_id"), head_off_receipt)
        finally:
            if controls and activation:
                controls.inflight -= 1
                controls.head_activation_finished(body.get("entity_id"))
                if body.get("entity_id") == controls.config.heater_entity:
                    controls.auxiliary_activation_finished()
                controls._schedule_check()
        return []

    def states(self):
        return {state["entity_id"]: state for state in self.request("states")}

    def service(self, domain, service, *, auxiliary_target=None, **data):
        return self.request(f"services/{domain}/{service}", data, auxiliary_target=auxiliary_target)

    def reset_airflow_authorizations(self):
        self._airflow_authorizations = {}

    def airflow_authorization(self, entity, plan, states, now):
        from .airflow_adapter import authorize
        return authorize(self, entity, plan, states, now)

    def airflow_service(self, ticket, service, **data):
        from .airflow_adapter import AirflowAuthorization
        if not isinstance(ticket, AirflowAuthorization):
            raise HAActivationDeferred("Airflow requires typed authorization")
        return self.request(f"services/fan/{service}", {"entity_id": ticket.entity, **data},
                            activation_deadline=ticket.deadline, airflow_authorization=ticket)

    def airflow_jobs_clear(self, entity):
        return not self.service_jobs((entity,)) and not self.uncertain_service_jobs((entity,))

    def auxiliary_target(self, ceiling_f):
        """Resolve native capabilities on HA's loop; never infer units from state."""
        return self.request(f"auxiliary_target/{float(ceiling_f)}")

    def cached_auxiliary_target(self, observed):
        """Nonblocking evidence for a stop; never postpone Off for metadata I/O."""
        snapshot = self._auxiliary_target_snapshot
        if snapshot is None or snapshot[0] != observed.get("last_reported"):
            return None
        return snapshot[1]

    @callback
    def capture_auxiliary_target(self):
        try:
            self._auxiliary_target(self.config.heater_high_setpoint_f)
        except HAError:
            pass  # The invalidated cache cannot acknowledge a completed start.

    @callback
    def _auxiliary_target(self, ceiling_f):
        try:
            component = self.hass.data.get(DATA_COMPONENT)
            entity = component.get_entity(self.config.heater_entity) if component else None
            if entity is None:
                raise ValueError("native climate entity unavailable")
            state = self.hass.states.get(self.config.heater_entity)
            if (not entity.available or state is None or not trusted_auxiliary_state(
                    {"state": state.state, "attributes": state.attributes})):
                raise ValueError("native climate feedback unavailable or untrusted")
            unit = entity.temperature_unit
            display_unit = self.hass.config.units.temperature_unit
            if unit not in ("°C", "°F") or display_unit not in ("°C", "°F"):
                raise ValueError("unsupported temperature unit")
            low, high, step, precision = (entity.min_temp, entity.max_temp,
                                         entity.target_temperature_step, entity.precision)
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in (low, high, step, precision, ceiling_f)):
                raise ValueError("missing or non-finite native range, step or precision")
            if low >= high or step <= 0 or precision <= 0:
                raise ValueError("invalid native range, step or precision")
            # Decimal conversion avoids flooring one extra rung at exact C/F
            # boundaries. The advertised minimum anchors the native step grid.
            cap = Decimal(str(ceiling_f))
            if unit == "°C":
                cap = (cap - 32) * 5 / 9
            lower, upper, increment = map(lambda v: Decimal(str(v)), (low, high, step))
            if not lower <= cap <= upper:
                raise ValueError("configured heater ceiling outside native range")
            native = float(lower + ((cap - lower) / increment).to_integral_value(rounding=ROUND_FLOOR) * increment)
            command = TemperatureConverter.convert(native, unit, display_unit)
            reported = display_temp(self.hass, native, unit, precision)
            raw = entity.target_temperature
            observed = (float(raw) if not isinstance(raw, bool) and isinstance(raw, (int, float))
                        and math.isfinite(raw) else None)
            selected = AuxiliaryTarget(command, reported, native, ceiling_f,
                (id(entity), unit, low, high, step, precision, display_unit), observed)
            self._auxiliary_target_snapshot = (state.last_reported.isoformat(), selected)
            return selected
        except Exception as exc:
            self._auxiliary_target_snapshot = None
            # Third-party entity properties can fail independently of the rest
            # of the house; retain an auxiliary-only inhibition and Off path.
            raise HAError(f"Auxiliary native target capabilities unavailable: {exc}") from exc

    @staticmethod
    def _activation_deadline(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HAActivationDeferred("Activation deadline must be a finite monotonic time")
        try:
            deadline = float(value)
        except (ValueError, OverflowError):
            raise HAActivationDeferred("Activation deadline must be a finite monotonic time") from None
        if not math.isfinite(deadline):
            raise HAActivationDeferred("Activation deadline must be a finite monotonic time")
        return deadline

    def service_before(self, domain, service, *, deadline, auxiliary_target=None, **data):
        """Bound positive dispatch without leaking controller metadata to HA."""
        path = f"services/{domain}/{service}"
        if self._is_activation(path, data):
            deadline = self._activation_deadline(deadline)
        return self.request(path, data,
                            activation_deadline=deadline, auxiliary_target=auxiliary_target)

    def service_guarded(self, domain, service, *, ticket, **data):
        """Carry a prior plan's proof; never mint permission from current inputs."""
        if (self._is_activation(f"services/{domain}/{service}", data)
                and not isinstance(ticket, WindowAuthorization)):
            raise HAActivationDeferred("Window authorization is missing")
        return self.request(f"services/{domain}/{service}", data, window_authorization=ticket)


class ControllerRuntime:
    def __init__(self, hass, config, live, *, house_policy=False, power_bindings=None,
                 notification_settings=None):
        self.hass = hass
        self.config = config
        self.live = live
        self._window_entities = frozenset(entity for room in config.zones.values() for entity in room.windows)
        self._outdoor_entities = frozenset(
            entity for probe in config.outdoor_probes
            for entity in (probe.entity_id, probe.observation_time_entity) if entity)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.controls = None
        self.comfort = None
        self.purifier_speeds = None
        self.house_policy = house_policy
        self.power_bindings = power_bindings
        self.power_guard = None
        self.notifications = None
        self.notification_settings = notification_settings
        self.seasonal_state = None
        self.thermal_storage = None
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
        self.api.service_notify = self._async_service_jobs_changed
        self._options_reload_task = None
        self._unload_task = None
        self._unload_deadline = None
        self._unload_phase = None
        self._unload_timed_out = False
        self._configuration_reload_pending = False
        self._configuration_reload_reason = None
        self.recovery = RecoveryManager(hass, self)

    @callback
    def _async_service_jobs_changed(self):
        self._shutdown_progress.set()

    async def async_setup_controls(self, entry_id):
        from .airflow_calibration import async_load_calibrations
        await async_load_calibrations(self.api, entry_id)
        from .comfort import RoomComfort
        self.comfort = RoomComfort(self.hass, self.config, entry_id,
                                   self._async_comfort_changed, self.wake_event)
        try:
            from .seasonal_state import SeasonalState
            self.seasonal_state = SeasonalState(self.hass, entry_id)
            await self.seasonal_state.async_load()
            await self.comfort.async_load()
            from .purifier import RoomPurifierSpeeds
            self.purifier_speeds = RoomPurifierSpeeds(
                self.hass, self.config, entry_id, self._async_comfort_changed, self.wake_event)
            await self.purifier_speeds.async_load()
            from .bluetooth_freshness import BluetoothFreshness
            self.api.bluetooth_freshness = BluetoothFreshness(self.hass, self.config)
            self.api.bluetooth_freshness.async_start()
            if hasattr(self.comfort, "set_seasonal_profile"):
                self.comfort.set_seasonal_profile(self.seasonal_state.profile)
            if self.notification_settings is not None:
                from .notifications import AlertManager
                self.notifications = AlertManager(self.hass, entry_id, self.notification_settings,
                                                  self._notification_changed)
            self.api.settings_provider = self._settings
            await self.recovery.capabilities.async_load(entry_id)
            if not self.config.legacy:
                from .controls import NativeControls
                self.controls = NativeControls(self.hass, self.config, self.live, entry_id,
                                               self._async_controls_changed)
                self.controls.fan_controls_provider = self.purifier_speeds.fan_controls
                self.api.controls = self.controls
                self.controls.pending_device_jobs = lambda entities: (
                    self.api.service_jobs(entities) or self.api.uncertain_service_jobs(entities))
                self.controls.repair_gate = self.api.repair_gate
                if self.house_policy or getattr(self.config, "auxiliary_dynamic_stop", False):
                    self.controls.auxiliary_limit_provider = self._auxiliary_room_limit
                    self.controls.auxiliary_context_provider = self._auxiliary_room_context
                await self.controls.async_load()
            if self.house_policy:
                from .power_guard import HeaterPowerGuard
                self.power_guard = HeaterPowerGuard(
                    self.hass, bindings=self.power_bindings, live=self.live, entry_id=entry_id,
                    notify=self._async_power_guard_changed)
                self.api.power_guard = self.power_guard
                self.power_guard.pending_external_jobs = lambda: (
                    self.api.service_jobs({self.config.heater_entity})
                    or self.api.uncertain_service_jobs({self.config.heater_entity})
                    or (self.controls and self.controls.stop_jobs({self.config.heater_entity})))
                from .device_feedback import async_read_matter_switch
                self.power_guard.async_read_recovery_feedback = lambda entity: async_read_matter_switch(self.hass, entity)
                self.power_guard.commissioning_controller_off = self._commissioning_controller_off
                self.power_guard.recovery_controller_off = self._recovery_controller_off
                self.power_guard.manual_override = lambda: bool(self.controls and self.controls.auxiliary_manual)
                self.power_guard.alerts = self.notifications
                if self.controls is not None:
                    self.controls.power_guard = self.power_guard
                await self.power_guard.async_load()
        except BaseException:
            if self.power_guard is not None:
                await self.power_guard.async_close()
            await self.comfort.async_close()
            if self.purifier_speeds:
                await self.purifier_speeds.async_close()
            if self.api.bluetooth_freshness:
                self.api.bluetooth_freshness.async_close()
            raise

        # Optional evidence persistence starts only after control setup succeeds.
        # Its failures never prevent the controller from running.
        try:
            from .thermal_storage import ThermalStorage
            self.thermal_storage = ThermalStorage(self.hass, self.config, entry_id)
            self.thermal_storage.start()
        except Exception:
            self.thermal_storage = None
            _LOGGER.warning("Thermal history could not start; control is unaffected", exc_info=True)

    def _settings(self, states, config, now, strict=False):
        settings = self.comfort.settings(states, config, now, strict=strict)
        return self.purifier_speeds.settings(settings) if self.purifier_speeds else settings

    @callback
    def _notification_changed(self):
        if self.notifications is not None:
            self.status["notifications"] = self.notifications.diagnostics
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    def _auxiliary_room_limit(self):
        """Re-evaluate the active room bound without trusting an old worker plan."""
        instant = datetime.now(timezone.utc)
        states = {state["entity_id"]: state for state in self.api._read_states_without_controls()}
        settings = self.comfort.settings(states, self.config, instant)
        room_id = self.config.auxiliary_room
        profile = settings.profile(self.config.zones[room_id], instant, self.hass.config.time_zone, room_id)
        target = settings.value(profile.target_f) + profile.target_offset_f
        high = settings.value(profile.high_f) if profile.high_f is not None else target + settings.value(profile.half_band_f)
        protection = self.config.protection
        return min(high, protection.emergency_cooling_at_f - 1 if protection is not None else self.config.high_f - 1)

    def _auxiliary_room_context(self):
        """Compare the current profile/peak at dispatch without inventing trend evidence."""
        from .controller.auxiliary_dispatch import AuxiliaryDispatch
        from .controller.protection_context import limits
        instant = datetime.now(timezone.utc)
        states = {state["entity_id"]: state for state in self.api._read_states_without_controls()}
        settings = self.comfort.settings(states, self.config, instant)
        room_id = self.config.auxiliary_room
        profile = settings.profile(self.config.zones[room_id], instant, self.config.timezone, room_id)
        target = settings.value(profile.target_f) + profile.target_offset_f
        half = settings.value(profile.half_band_f)
        low, high = limits(self.config, self.status.get("protection_context"))
        return [max(low, settings.value(profile.low_f) if profile.low_f is not None else target - half),
                min(high, settings.value(profile.high_f) if profile.high_f is not None else target + half),
                target, not profile.comfort_enabled, AuxiliaryDispatch(self.config).peak(instant)]

    def _commissioning_controller_off(self):
        """A maintenance cut cannot overlap comfort ownership or device work."""
        return (self.live and self.controls is not None and self.controls.live
                and self._all_controllers_stopped())

    def _all_controllers_stopped(self):
        """Verify stopped ownership/jobs without granting command permission."""
        controls = self.controls
        if (self.stop_event.is_set() or self._configuration_reload_pending
                or controls is None or not controls.active
                or controls.enabled or controls.owned or controls.pending_fans
                or controls.recovering or controls._closing or controls._claiming
                or controls.inflight or controls.service_inflight or controls.auxiliary_inflight):
            return False
        devices = {*self.config.climate_entities, *self.config.fans.values()}
        return not (self.api.service_jobs(devices) or self.api.uncertain_service_jobs(devices))

    def _recovery_controller_off(self):
        """Only auxiliary work must be stopped for its explicit recovery."""
        controls = self.controls
        if (self.stop_event.is_set() or self._configuration_reload_pending
                or controls is None or not controls.active or controls._closing or controls._claiming
                or controls.auxiliary_inflight or self.power_guard is None or not self.power_guard.blocked):
            return False
        devices = {self.config.heater_entity}
        return not (self.api.service_jobs(devices) or self.api.uncertain_service_jobs(devices)
                    or controls.stop_jobs(devices))

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
                           enabled_intent=self.controls.enabled_intent,
                           takeover_pending=self.controls.takeover_pending,
                           pending_fans=list(self.controls.pending_fans),
                           auxiliary_manual=self.controls.auxiliary_manual,
                           auxiliary_manual_entity=self.controls.auxiliary_manual_entity,
                           handoff=self.controls.handoff,
                           handoff_notification_disposition=
                               notification_disposition(self.controls))
        ready = self.controls._snapshot.get("enabled", False)
        if previous != self.controls.enabled or ready != self._controls_ready:
            self.wake_event.set()
        self._controls_ready = ready
        self._update_reload_status()
        self._update_heartbeat_status()
        if self.notifications is not None:
            self.notifications.observe_status(self.status)
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_recovery_changed(self):
        self.status["integration_recovery"] = self.recovery.diagnostics
        self.wake_event.set()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    @callback
    def _async_climate_changed(self, event):
        if event.data["entity_id"] == self.config.heater_entity:
            self.api.capture_auxiliary_target()
        if self.stop_event.is_set():
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if event.data["entity_id"] in self._window_entities:
            if ((old.state if old else None) != (new.state if new else None)
                    or (old.attributes.get("restored") is True if old else False)
                    != (new.attributes.get("restored") is True if new else False)):
                self.wake_event.set()
            return
        if event.data["entity_id"] in self._outdoor_entities:
            def evidence(state):
                return ((state.state, *(state.attributes.get(key) for key in
                        ("temperature", "temperature_unit", "unit_of_measurement", "restored",
                         *(probe.attribute for probe in self.config.outdoor_probes if probe.attribute))))
                        if state is not None else None)
            if evidence(old) != evidence(new):
                self.wake_event.set()
            return
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
        if self.thread is not None or self.stop_event.is_set() or self.hass.is_stopping:
            return
        if self.controls:
            self.controls.async_start()
        if self.power_guard is not None:
            self.power_guard.async_start()
        self._remove_climate_listener = async_track_state_change_event(
            self.hass, [*self.config.climate_entities, *self._window_entities, *self._outdoor_entities], self._async_climate_changed
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
        if self.thermal_storage is not None:
            self.thermal_storage.observe(event, data)
        if self.seasonal_state is not None:
            seasonal = data.get("seasonal")
            self.seasonal_state.observe(seasonal)
            if isinstance(seasonal, dict) and hasattr(self.comfort, "set_seasonal_profile"):
                self.comfort.set_seasonal_profile(seasonal.get("profile"))
            self.status["seasonal_persistence_error"] = self.seasonal_state.error
        self._update_power_guard_status()
        if self.controls:
            self.status.update(control_enabled=self.controls.enabled,
                               control_block_reason=self.controls.hint,
                               enabled_intent=self.controls.enabled_intent,
                               takeover_pending=self.controls.takeover_pending,
                               pending_fans=list(self.controls.pending_fans),
                               auxiliary_manual=self.controls.auxiliary_manual,
                               auxiliary_manual_entity=self.controls.auxiliary_manual_entity,
                               handoff=self.controls.handoff,
                           handoff_notification_disposition=
                               notification_disposition(self.controls))
        self._update_reload_status()
        self._update_heartbeat_status()
        self.recovery.observe(self.status)
        self.status["integration_recovery"] = self.recovery.diagnostics
        if self.notifications is not None:
            self.notifications.observe_status(self.status)
            self.status["notifications"] = self.notifications.diagnostics
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
                     settings_provider=self._settings if self.comfort else None)
        except Exception as exc:
            error = str(exc)
            _LOGGER.exception("Controller worker stopped unexpectedly")
        finally:
            self.hass.loop.call_soon_threadsafe(self._async_finished, error)

    @callback
    def _async_finished(self, error):
        if error is None and not self.stop_event.is_set():
            error = "Controller worker exited without a shutdown request"
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
        if self._unload_task is not None:
            self.status["unload"] = {
                "phase": self._unload_phase, "timed_out": self._unload_timed_out,
                "remaining_seconds": max(0, self._unload_deadline - self.hass.loop.time()),
                "sampled_at": datetime.now(timezone.utc).isoformat(),
            }

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
        deadline = (self._unload_deadline if self._unload_deadline is not None
                    else self.hass.loop.time() + UNLOAD_ACK_TIMEOUT_SECONDS)
        while not closed:
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                return False
            self._shutdown_progress.clear()
            await self._async_wait_shutdown_progress(min(SHUTDOWN_RECHECK_SECONDS, remaining))
            closed = await self.controls.async_close()
        return True

    def _unload_budget_seconds(self):
        """Allow bounded stop and fallback-feedback windows before failure."""
        if self.config.fallback is None:
            return UNLOAD_ACK_TIMEOUT_SECONDS
        devices = len(set((*self.config.climate_entities, *self.config.fans.values())))
        return max(UNLOAD_ACK_TIMEOUT_SECONDS,
                   WORKER_STOP_TIMEOUT_SECONDS + self.config.command_timeout_seconds
                   + self.config.head_activation_timeout_seconds + 5 * devices + 20)

    def _set_unload_phase(self, phase):
        if self._unload_task is None:
            return
        self._unload_phase = phase
        self._update_reload_status()
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    async def async_wait_for_unload(self, operation):
        """Retain one whole unload through caller cancellation and deadline expiry.

        ``operation`` receives the original caller so a saved-options reload
        never cancels its own parent when the retained task invokes async_stop.
        The deadline bounds the caller, not actual storage/device operations.
        A timeout retains this runtime and all unresolved cleanup authority.
        """
        task = self._unload_task
        if task is not None and task.done():
            try:
                completed = not task.cancelled() and task.result() is True
            except Exception:
                completed = False
            if completed:
                return True
            # A later explicit unload can recheck evidence after a terminal
            # failed attempt. Never replace an operation which is still running.
            task = None
        if task is None:
            caller = asyncio.current_task()
            self._unload_deadline = self.hass.loop.time() + self._unload_budget_seconds()
            self._unload_timed_out = False
            self.stop_event.set()
            self.wake_event.set()

            async def unload():
                try:
                    result = await operation(caller)
                    self._set_unload_phase("complete" if result else "blocked")
                    return result
                except Exception:
                    self._set_unload_phase("failed")
                    raise

            self._unload_task = self.hass.async_create_background_task(
                unload(), "Home Climate retained unload", eager_start=False)
            # A deadline may release the HA caller before a failed operation
            # returns. Retrieve its exception without cancelling the operation.
            self._unload_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None)
            self._set_unload_phase("starting")
        task = self._unload_task
        while not task.done():
            remaining = self._unload_deadline - self.hass.loop.time()
            if remaining <= 0:
                self._unload_timed_out = True
                self._set_unload_phase(self._unload_phase)
                _LOGGER.error("Controller unload deadline expired during %s; actual cleanup is retained",
                              self._unload_phase)
                return False
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # Propagating cancellation here strands HA in its nonrecoverable
                # UNLOAD_IN_PROGRESS state. Finish the same bounded operation;
                # never cancel a physical command merely to cancel its waiter.
                _LOGGER.warning("Controller unload caller was cancelled; completing retained cleanup")
        if task.cancelled():
            # HA shutdown can cancel background tasks independently of the
            # caller. No completed cleanup can be claimed in that case.
            self._set_unload_phase("cancelled")
            return False
        return task.result()

    async def async_stop(self, _event=None, *, wait_for_ack=False, _unload_caller=None):
        # An external unload supersedes a pending options reload. The managed
        # reload itself re-enters this method from HA and must not cancel itself.
        task = self._options_reload_task
        if (wait_for_ack and task and task is not asyncio.current_task()
                and task is not _unload_caller and not task.done()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._stop_lock:
            return await self._async_stop(wait_for_ack)

    async def _async_stop(self, wait_for_ack):
        self.stop_event.set()
        self.wake_event.set()
        self._set_unload_phase("comfort_storage")
        if self.comfort:
            await self.comfort.async_close()
            if self.purifier_speeds:
                await self.purifier_speeds.async_close()
            if self.api.bluetooth_freshness:
                self.api.bluetooth_freshness.async_close()
        self._set_unload_phase("integration_recovery")
        recovery_closed = await self.recovery.async_close()
        self._set_unload_phase("ownership_storage")
        if self.controls:
            await self.controls.async_set_enabled(False, preserve_intent=True)
        self._set_unload_phase("worker_stop")
        if self.finished is None:
            self._async_report("stopped", {})
        else:
            try:
                await asyncio.wait_for(asyncio.shield(self.finished), timeout=WORKER_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                # Keep the runtime registered; a reload must never start a second worker.
                self._warn_unload_deferred(worker_pending=True, recovery_pending=not recovery_closed)
                self._async_report("worker_failed", {"error": "Controller shutdown timed out"})
                return False
            await self.hass.async_add_executor_job(self.thread.join)
        self._set_unload_phase("device_handoff")
        controls_closed = await self._async_close_controls(wait_for_ack)
        devices = (*self.config.climate_entities, *self.config.fans.values())
        jobs_closed = not self.api.service_jobs(devices) and not self.api.uncertain_service_jobs(devices)
        closed = controls_closed and recovery_closed and jobs_closed
        if closed:
            if self.power_guard is not None:
                self._set_unload_phase("heater_guard")
                guard_closed = await self.power_guard.async_close()
                if guard_closed is False:
                    # A delayed outlet-On is an activation, even if an older Off
                    # is cached. Keep this runtime until compensation is proven.
                    closed = False
                    _LOGGER.warning("Controller unload deferred; waiting for heater outlet restoration to be safely stopped")
                    self._update_power_guard_status()
            if closed:
                self._logged_unload_blockers = None
                if self.seasonal_state is not None:
                    self._set_unload_phase("seasonal_storage")
                    await self.seasonal_state.async_close()
                if self.notifications is not None:
                    await self.notifications.async_close()
                if self.thermal_storage is not None:
                    await self.thermal_storage.async_close()
        else:
            self._warn_unload_deferred(recovery_pending=not recovery_closed)
            if not jobs_closed:
                _LOGGER.warning(
                    "Controller unload deferred; device service jobs are still running or uncertain; "
                    "the runtime retains ownership until completion is established")
        if closed and self._remove_climate_listener:
            self._remove_climate_listener()
            self._remove_climate_listener = None
        if not recovery_closed:
            self._async_report("worker_failed", {
                "error": "Owning heat-pump integration reload is still stopping; unload deferred"
            })
        return closed
