"""Fixed, heater-only load shedding for the living-room battery-backed circuit.

Emporia reports real power, not RMS branch current.  The 1350 W input budget is
deliberately below 12 A at nominal US voltage; it is a conservative automation
budget, not a claim to measure branch amps or replace circuit protection.  The
Elite's input overlaps the Emporia reading: they are compared, never added.

The native thermostat is stopped first, followed by an optional dedicated plug.
An explicitly enabled shared-AC fallback is a final emergency action with manual
recovery; it never automatically restores AC, DC, charging, or network power.
This module imports HA lazily so its
measurement policy and asynchronous failure paths can also be tested in isolation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller.bindings import PowerBindings

_LOGGER = logging.getLogger(__name__)

INPUT_BUDGET_W = 1350.0
INPUT_CURRENT_CEILING_A = 12.0
OUTPUT_SPIKE_W = 2000.0  # Operational shedding threshold, not an inverter rating.
BATTERY_RESERVE_PERCENT = 40.0
TELEMETRY_MAX_AGE_SECONDS = 180
BATTERY_MAX_AGE_SECONDS = 900
OFF_ACK_SECONDS = 5.0
RESTORE_ACK_SECONDS = 10.0
SERVICE_TIMEOUT_SECONDS = 5.0
CHECK_SECONDS = 5
COOLDOWN_SECONDS = 300.0
SAFE_LOAD_SECONDS = 60.0
MAX_RESTORE_ATTEMPTS = 3


def _state_parts(state):
    if state is None:
        return None, {}, None
    if isinstance(state, dict):
        return state.get("state"), state.get("attributes", {}), (
            state.get("last_reported") or state.get("last_updated"))
    return state.state, state.attributes, (
        getattr(state, "last_reported", None) or getattr(state, "last_updated", None))


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.timestamp()
    raise ValueError("missing reporting timestamp")


def _measurement(states, entity, units, now, *, maximum_age=TELEMETRY_MAX_AGE_SECONDS):
    value, attrs, reported = _state_parts(states.get(entity))
    if isinstance(value, bool) or value in (None, "unknown", "unavailable"):
        raise ValueError(f"{entity}: unavailable")
    unit = attrs.get("unit_of_measurement")
    if unit not in units:
        raise ValueError(f"{entity}: unexpected unit {unit!r}")
    try:
        age = now - _timestamp(reported)
        number = float(value) * units[unit]
        if not math.isfinite(number) or number < 0:
            raise ValueError("invalid number")
        if not -5 <= age <= maximum_age:
            raise ValueError("reading not current")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{entity}: {exc}") from None
    return number


@dataclass(frozen=True)
class PowerReading:
    circuit_w: float | None = None
    input_w: float | None = None
    output_w: float | None = None
    battery_percent: float | None = None
    heater_a: float | None = None
    voltage_v: float | None = None
    input_budget_w: float = INPUT_BUDGET_W
    issue: str | None = None
    telemetry_missing: bool = False

    @property
    def safe_to_rearm(self):
        return (not self.issue and self.circuit_w < self.input_budget_w * .85
                and self.input_w < self.input_budget_w * .85
                and self.output_w < OUTPUT_SPIKE_W * .8
                and (self.heater_a is None or self.heater_a < 1)
                and self.battery_percent > BATTERY_RESERVE_PERCENT)


def evaluate_power(states, bindings: PowerBindings, now=None):
    """Read independent limits; no inferred circuit-current value is presented."""
    now = time.time() if now is None else now
    values = {}
    specs = (
        ("circuit_w", bindings.circuit_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS),
        ("input_w", bindings.input_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS),
        ("output_w", bindings.output_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS),
        ("battery_percent", bindings.battery, {"%": 1}, BATTERY_MAX_AGE_SECONDS),
        ("heater_a", bindings.heater_current, {"A": 1, "mA": .001}, TELEMETRY_MAX_AGE_SECONDS),
    )
    missing = []
    for key, entity, units, age in specs:
        if key == "heater_a" and entity is None:
            # An explicitly absent meter is unknown, never a fresh zero.
            continue
        try:
            values[key] = _measurement(states, entity, units, now, maximum_age=age)
        except ValueError as exc:
            missing.append(str(exc))
    try:
        # The heater plug is on the inverter output and cannot measure wall
        # brownouts. The circuit phase is unknown: use the lower wall phase.
        values["voltage_v"] = min(_measurement(states, entity, {"V": 1}, now)
                                  for entity in (bindings.wall_phase_a_voltage, bindings.wall_phase_b_voltage))
    except ValueError as exc:
        missing.append(str(exc))
    # Never increase the nominal budget when line voltage is high.  Lower it for
    # a brownout; reserve additional headroom for unknown aggregate power factor.
    voltage = values.get("voltage_v")
    voltage_valid = voltage is not None and 90 <= voltage <= 140
    budget = min(INPUT_BUDGET_W, INPUT_CURRENT_CEILING_A * min(voltage, 120) * .95) if voltage_valid else INPUT_BUDGET_W
    issue = None
    # The heater plug is downstream of the inverter. Its current is useful for
    # telemetry validation and proving low load before rearming, but must not
    # impose the wall-input 12 A ceiling on normal battery-supplied output.
    if max(values.get("circuit_w", -1), values.get("input_w", -1)) >= budget:
        issue = f"Circuit or Elite input reached the {budget:g} W heater-shedding budget"
    elif values.get("output_w", -1) >= OUTPUT_SPIKE_W:
        issue = f"Elite output spike reached {OUTPUT_SPIKE_W:g} W"
    elif 0 <= values.get("battery_percent", -1) <= BATTERY_RESERVE_PERCENT:
        issue = "Elite battery is at or below the 40% network reserve"
    # Missing unrelated telemetry must not hide a conclusive input/output trip.
    if issue:
        return PowerReading(**values, input_budget_w=budget, issue=issue)
    if missing:
        return PowerReading(**values, input_budget_w=budget,
                            issue="Power telemetry unavailable: " + "; ".join(missing), telemetry_missing=True)
    if not voltage_valid or values["battery_percent"] > 100:
        return PowerReading(**values, issue="Power telemetry outside plausible bounds", telemetry_missing=True)
    return PowerReading(**values, input_budget_w=budget, issue=issue)


class HeaterPowerGuard:
    """HA-loop guard independent of comfort ownership and the worker heartbeat."""

    def __init__(self, hass, bindings: PowerBindings, *, live, entry_id, notify, clock=time.monotonic, wall_clock=time.time):
        if not is_dataclass(bindings):
            raise TypeError("A validated PowerBindings dataclass is required")
        for role, domain in (("heater", "climate"), ("heater_switch", "switch"),
                             *((role, "sensor") for role in ("battery", "circuit_power", "input_power",
                                "output_power", "heater_current", "wall_phase_a_voltage", "wall_phase_b_voltage"))):
            entity = getattr(bindings, role, None)
            if entity is None and role in ("heater_switch", "heater_current"):
                continue
            if not isinstance(entity, str) or not entity.startswith(domain + "."):
                raise ValueError(f"Invalid {role} power binding")
        self.bindings = bindings
        self.entity_ids = bindings.entity_ids
        self.hass, self.live, self.entry_id, self.notify = hass, live, entry_id, notify
        self._clock, self._wall_clock = clock, wall_clock
        self._reading = PowerReading(issue="Waiting for power telemetry", telemetry_missing=True)
        self._blocked = True
        self._latched = False
        self._cut_owned = False
        self._tripped_at = None
        self._trip_wall = None
        self._safe_since = None
        self._off_confirmed = False
        self._off_request_wall = None
        self._manual_off_wall = None
        self._restore_attempts = 0
        self._restoring = False
        self._closed = False
        self._closing = False
        self._active = False
        self._reason = self._reading.issue
        self._unsubscribers = []
        self._check_task = None
        self._action_task = None
        self._save_task = None
        self._save_dirty = False
        self._recheck = False
        self._service_tasks = set()
        self._positive_tasks = set()
        self._invalid_positive = set()
        self._compensating_task = None
        self._force_cut_again = False
        self._compensation_pending = False
        self._cut_request_wall = None
        self._cut_changed = None
        self._persistence_error = None
        self._store = None
        self._store_lock = asyncio.Lock()
        self._restore_intent = False
        self._manual_power_seen = False
        self._cutting = False
        self._save_retry_at = 0.0
        self._last_diagnostics = None
        self._emergency_ac_latched = False
        self._emergency_ac_status = "idle"
        self._emergency_ac_reason = None
        self._emergency_ac_request_wall = None
        self._emergency_ac_attempts = 0
        self._emergency_service_tasks = set()
        self._manual_recovery_required = False
        self._recovery_task = None
        self._manual_recovery_wall = None
        self._identity_mismatch = False
        self._saved_binding_identity = self._binding_identity()
        self._shutdown_confirmation = "unconfirmed"
        self._shutdown_contradicted = False
        self._shutdown_stage = "idle"
        self._aggregate_evidence = None
        self._aggregate_outcome = None
        policy = getattr(self.bindings, "aggregate_stop_confirmation", None)
        self._aggregate_baseline = (policy.create_baseline()
            if policy is not None and policy.relative_drop_enabled else None)
        self._notice_signature = None
        self._notice_active = False
        self._group_notice_queued = False
        self._notice_error = None
        self.pending_external_jobs = lambda: ()

    def _state(self, entity):
        return self.hass.states.get(entity) if entity is not None else None

    def _binding_identity(self):
        return {key: getattr(self.bindings, key, None) for key in (
            "heater", "heater_switch", "heater_current", "emergency_ac_switch")}

    @property
    def _emergency_enabled(self):
        return (getattr(self.bindings, "emergency_ac_fallback_enabled", False)
                and bool(getattr(self.bindings, "emergency_ac_switch", None)))

    @property
    def emergency_fallback_enabled(self):
        return self._emergency_enabled

    @property
    def blocked(self):
        return (self._blocked or self._closing or self._closed or bool(self._persistence_error)
                or self._manual_recovery_required or self._emergency_ac_latched
                or (self.bindings.heater_switch is not None and not self._switch_observed("on"))
                or (self.bindings.heater_switch is None and not self._emergency_enabled)
                or (self._emergency_enabled and not self._ac_observed("on")))

    @property
    def switch_cut_owned(self):
        return self._cut_owned

    @property
    def power_cut_confirmed(self):
        """Actual dedicated-plug feedback, not completion of a service call."""
        return (not self._identity_mismatch
                and ((self._switch_cut_observed()
                      and self._heater_current_stopped(after=self._cut_request_wall) is not False)
                     or self._emergency_cut_observed())
                and not self._positive_tasks and not self._emergency_service_tasks
                and not self._compensation_pending and not self._restoring)

    @property
    def shutdown_confirmed(self):
        """Keep known electrical contradictions from becoming cached Off proof."""
        if self.power_cut_confirmed:
            return True
        if self._shutdown_contradicted:
            return False
        if self._off_boundary() is None:
            return self._heater_off()
        return self._off_confirmed and self._heater_off(fresh=True)

    def _off_boundary(self):
        """A verified manual recovery is evidence, not a fabricated command."""
        return max((stamp for stamp in (self._off_request_wall, self._manual_off_wall)
                    if stamp is not None), default=None)

    def _switch_cut_observed(self):
        # Physical isolation and restoration ownership are independent. A user's
        # manual Off is valid isolation proof but never a right to turn it On.
        return self._switch_observed("off", after=self._cut_request_wall)

    @property
    def diagnostics(self):
        r = self._reading
        return {"blocked": self.blocked, "reason": self._reason,
                "latched": self._latched, "switch_cut_owned": self._cut_owned,
                "power_cut_confirmed": self.power_cut_confirmed,
                "restoring": self._restoring, "restore_attempts": self._restore_attempts,
                "power_restore_inflight": bool(self._positive_tasks),
                "power_restore_cleanup_pending": self._compensation_pending,
                "heater_cutoff_configured": self.bindings.heater_switch is not None,
                "heater_current_configured": self.bindings.heater_current is not None,
                "electrical_stop_confirmed": self._heater_current_stopped() is True,
                "shutdown_stage": self._shutdown_stage,
                "shutdown_confirmation": self._shutdown_confirmation,
                "aggregate_stop": self._aggregate_outcome.as_dict() if self._aggregate_outcome else None,
                "emergency_ac_enabled": self._emergency_enabled,
                "emergency_ac_latched": self._emergency_ac_latched,
                "emergency_ac_status": self._emergency_ac_status,
                "emergency_ac_attempts": self._emergency_ac_attempts,
                "emergency_ac_inflight": bool(self._emergency_service_tasks),
                "emergency_notification_error": self._notice_error,
                "manual_recovery_required": self._manual_recovery_required,
                "binding_identity_mismatch": self._identity_mismatch,
                "persistence_error": self._persistence_error,
                "circuit_power_w": r.circuit_w, "elite_input_w": r.input_w,
                "elite_output_w": r.output_w, "battery_percent": r.battery_percent,
                "heater_current_a": r.heater_a, "input_budget_w": r.input_budget_w,
                "branch_current_measured": False, "live": self.live}

    def _publish(self):
        self._publish_emergency_notice()
        current = self.diagnostics
        stable = {key: current[key] for key in (
            "blocked", "reason", "latched", "switch_cut_owned", "power_cut_confirmed",
            "restoring", "restore_attempts", "power_restore_inflight",
            "power_restore_cleanup_pending", "persistence_error", "live",
            "shutdown_stage", "shutdown_confirmation", "aggregate_stop",
            "emergency_ac_enabled", "emergency_ac_latched", "emergency_ac_status",
            "emergency_ac_attempts", "emergency_ac_inflight", "manual_recovery_required",
            "binding_identity_mismatch", "emergency_notification_error")}
        if stable != self._last_diagnostics:
            self._last_diagnostics = stable
            self.notify()

    def _publish_emergency_notice(self):
        """Update one local notice; queue the chosen group once per latch."""
        if not self._emergency_ac_latched and not self._notice_active:
            return
        try:
            from homeassistant.components import persistent_notification
            notification_id = f"home_climate_heater_emergency_{self.entry_id}"
            if not self._emergency_ac_latched:
                persistent_notification.async_dismiss(self.hass, notification_id)
                self._notice_signature = None
                self._notice_active = self._group_notice_queued = False
                return
            reason = str(self._emergency_ac_reason or self._reason or "Heater shutdown is unconfirmed")[:2048]
            signature = (self._emergency_ac_status, reason)
            if signature == self._notice_signature:
                return
            status = {"requested": "AC output Off requested; awaiting confirmation",
                      "confirmed_off": "AC output Off confirmed by fresh feedback"}.get(
                          self._emergency_ac_status, "AC output Off requested; feedback is unknown or unconfirmed")
            message = (f"{reason}\n\n{status}. Manual recovery is required; Home Climate will not "
                       "turn shared AC back on. This output also supplies other loads, which may "
                       "include network equipment. Lost communication is not proof of a successful cut. "
                       "Check the equipment locally, restore power manually only after resolving the cause, "
                       "then use Acknowledge heater power recovery.")
            persistent_notification.async_create(
                self.hass, message, title="Home Climate heater emergency cutoff",
                notification_id=notification_id)
            self._notice_signature = signature
            self._notice_active = True
            service = getattr(self.bindings, "emergency_notify_service", None)
            if service and not self._group_notice_queued:
                self._group_notice_queued = True
                self.hass.async_create_task(self._send_emergency_notice(service, message))
        except Exception:
            error = "Could not update the local heater emergency notification"
            if self._notice_error != error:
                _LOGGER.exception(error)
            self._notice_error = error

    async def _send_emergency_notice(self, service, message):
        """Notification delivery is bounded and never blocks a power command."""
        domain, action = service.split(".", 1)
        if not self.hass.services.has_service(domain, action):
            self._notice_error = f"Configured emergency notification action is unavailable: {service}"
            _LOGGER.warning(self._notice_error)
            self._publish()
            return
        job = self.hass.async_create_task(self.hass.services.async_call(
            domain, action, {"message": message, "title": "Home Climate heater emergency cutoff"},
            blocking=True))
        job.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        done, _ = await asyncio.wait((job,), timeout=SERVICE_TIMEOUT_SECONDS)
        try:
            if job not in done:
                job.cancel()
                raise TimeoutError("Notification delivery timed out; delivery remains unknown")
            job.result()
        except (Exception, asyncio.CancelledError) as exc:
            self._notice_error = f"Emergency notification delivery failed or is unknown: {exc}"
            _LOGGER.warning(self._notice_error)
        else:
            self._notice_error = None
        self._publish()

    def _states(self):
        return {entity: self.hass.states.get(entity) for entity in self.entity_ids}

    async def async_load(self):
        from homeassistant.helpers.storage import Store
        self._store = Store(self.hass, 1, f"house_climate.{self.entry_id}.power_guard")
        data = await self._store.async_load() or {}
        if not isinstance(data, dict):
            data = {"latched": True}
        self._latched = data.get("latched") is True
        self._cut_owned = data.get("switch_cut_owned") is True
        self._emergency_ac_latched = data.get("emergency_ac_latched") is True
        self._emergency_ac_reason = data.get("emergency_ac_reason")
        self._emergency_ac_status = "unconfirmed" if self._emergency_ac_latched else "idle"
        try:
            requested = data.get("emergency_ac_request_wall")
            self._emergency_ac_request_wall = float(requested) if requested is not None else None
            if self._emergency_ac_request_wall is not None and not math.isfinite(self._emergency_ac_request_wall):
                raise ValueError("invalid AC request timestamp")
            self._emergency_ac_attempts = max(0, int(data.get("emergency_ac_attempts", 0)))
        except (TypeError, ValueError, OverflowError):
            self._emergency_ac_latched = True
            self._emergency_ac_request_wall = None
            self._emergency_ac_attempts = 1
        prior_identity = data.get("binding_identity")
        has_claim = self._cut_owned or self._latched or self._emergency_ac_latched
        if (has_claim and prior_identity != self._binding_identity()):
            # Ownership belongs to actual wiring, not just the config-entry ID.
            # Legacy claims have no identity and cannot authorize a new plug On.
            self._identity_mismatch = True
            self._manual_recovery_required = True
            self._saved_binding_identity = prior_identity
            self._cut_owned = False
        self._manual_recovery_required |= data.get("manual_recovery_required") is True
        self._manual_recovery_required |= self._emergency_ac_latched
        if self._manual_recovery_required:
            self._manual_recovery_wall = self._wall_clock()
        try:
            self._restore_attempts = min(MAX_RESTORE_ATTEMPTS, max(0, int(data.get("restore_attempts", 0))))
        except (TypeError, ValueError, OverflowError):
            self._latched = True
            self._restore_attempts = MAX_RESTORE_ATTEMPTS
            _LOGGER.error("Invalid saved heater restoration attempts; keeping automatic power restoration inhibited")
        # HA recreates State.last_changed at Core startup. It cannot establish
        # physical switch ownership across processes. The durable restore intent
        # (written before every On) is what prevents a stale ownership claim.
        self._cut_changed = self._switch_changed()
        self._cut_request_wall = None
        if self._latched or self._cut_owned or self._emergency_ac_latched or self._manual_recovery_required:
            self._latched = True
            try:
                self._trip_wall = float(data["trip_wall"])
                age = max(0, min(COOLDOWN_SECONDS, self._wall_clock() - self._trip_wall))
            except (KeyError, TypeError, ValueError, OverflowError):
                age = 0
                self._trip_wall = self._wall_clock()
            self._tripped_at = self._clock() - age
            self._reason = "Recovering a persisted heater power cutoff"
        if self._manual_recovery_required:
            self._reason = "Heater power recovery requires explicit operator acknowledgement"
        self._publish()

    def async_start(self):
        """Register inexpensive cached-state checks on HA's loop."""
        if self._active or self._closed:
            return
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

        @callback
        def changed(_event=None):
            if _event and self._aggregate_baseline is not None:
                entity = _event.data.get("entity_id")
                state = _event.data.get("new_state")
                now = self._wall_clock()
                if entity == self.bindings.heater and not self._aggregate_heating(state, now):
                    # Invalidate immediately even if a later valid state has
                    # already replaced this event's state before _observe().
                    self._aggregate_baseline.observe(now, None, None)
                elif entity == self.bindings.output_power:
                    # Preserve all known output changes/interruptions rather
                    # than only the final value of coalesced state events.
                    value, reported = (self._aggregate_sample(state)
                        if state is not None and self._aggregate_heating(self._state(self.bindings.heater), now)
                        else (None, None))
                    self._aggregate_baseline.observe(now, value, reported)
            if (_event and _event.data.get("entity_id") == self.bindings.heater_switch
                    and (state := _event.data.get("new_state")) is not None
                    and state.state == "on" and self._cut_owned
                    and not self._restoring and not self._cutting
                    and not self._positive_tasks and not self._compensation_pending):
                self._manual_power_seen = True
            self._schedule_check()

        @callback
        def interval_elapsed(_now):
            # HA interval listeners receive a UTC datetime, not a state Event.
            self._schedule_check()

        self._active = True
        self._unsubscribers = [
            async_track_state_change_event(self.hass, self.entity_ids, changed),
            async_track_time_interval(self.hass, interval_elapsed, timedelta(seconds=CHECK_SECONDS)),
        ]
        self._schedule_check()

    def check_activation(self, entity_id, body=None):
        """Recheck immediately before dispatch; callers always permit Off."""
        if entity_id != self.bindings.heater:
            return
        self._observe()
        if self.blocked:
            from .controller.ha import HAActivationDeferred
            raise HAActivationDeferred(self._reason or "Heater power is not available")

    def _schedule_check(self):
        if self._closed:
            return
        self._recheck = True
        if self._check_task is None or self._check_task.done():
            self._check_task = self.hass.async_create_task(self._async_check())

    async def _async_check(self):
        while self._recheck and not self._closed:
            self._recheck = False
            self._observe()
            await asyncio.sleep(0)

    def _heater_off(self, *, fresh=False):
        state, attrs, reported = _state_parts(self.hass.states.get(self.bindings.heater))
        if state != "off" or attrs.get("hvac_action") in ("heating", "cooling"):
            return False
        if not fresh:
            return True
        try:
            stamp = _timestamp(reported)
            boundary = self._off_boundary()
            return (boundary is not None and stamp > boundary
                    and not attrs.get("restored") and not attrs.get("assumed_state")
                    and -5 <= self._wall_clock() - stamp <= TELEMETRY_MAX_AGE_SECONDS)
        except (ValueError, TypeError, OverflowError):
            return False

    def _switch_observed(self, desired, *, after=None):
        state, _, reported = _state_parts(self._state(self.bindings.heater_switch))
        if state != desired:
            return False
        try:
            return after is None or _timestamp(reported) > after
        except (ValueError, TypeError, OverflowError):
            return False

    def _switch_changed(self):
        state = self._state(self.bindings.heater_switch)
        value = state.get("last_changed") if isinstance(state, dict) else getattr(state, "last_changed", None)
        try:
            return _timestamp(value)
        except (ValueError, TypeError, OverflowError):
            return None

    def _ac_observed(self, desired, *, after=None):
        entity = getattr(self.bindings, "emergency_ac_switch", None)
        state, attrs, reported = _state_parts(self._state(entity))
        if state != desired or attrs.get("restored") or attrs.get("assumed_state"):
            return False
        try:
            stamp = _timestamp(reported)
            return (-5 <= self._wall_clock() - stamp <= TELEMETRY_MAX_AGE_SECONDS
                    and (after is None or stamp > after))
        except (ValueError, TypeError, OverflowError):
            return False

    def _emergency_cut_observed(self):
        return (self._emergency_ac_latched and self._emergency_ac_request_wall is not None
                and self._ac_observed("off", after=self._emergency_ac_request_wall))

    def _heater_current_stopped(self, *, after=None):
        entity = self.bindings.heater_current
        boundary = self._off_boundary() if after is None else after
        if entity is None or boundary is None:
            return None
        try:
            observed = self._state(entity)
            value = _measurement({entity: observed}, entity, {"A": 1, "mA": .001}, self._wall_clock())
            _, attrs, reported = _state_parts(observed)
            if attrs.get("restored") or _timestamp(reported) <= boundary:
                return None
            return value < 1
        except (ValueError, TypeError, OverflowError):
            return None

    def _aggregate_sample(self, observed=None):
        entity = self.bindings.output_power
        observed = self._state(entity) if observed is None else observed
        try:
            value = _measurement({entity: observed}, entity, {"W": 1, "kW": 1000}, self._wall_clock())
            _, attrs, reported = _state_parts(observed)
            if attrs.get("restored") or attrs.get("assumed_state"):
                raise ValueError("restored or assumed output")
            return value, _timestamp(reported)
        except (ValueError, TypeError, OverflowError):
            return None, None

    def _aggregate_heating(self, observed, now):
        state, attrs, reported = _state_parts(observed)
        try:
            stamp = _timestamp(reported)
            policy = self.bindings.aggregate_stop_confirmation
            return (state == "heat" and attrs.get("hvac_action") == "heating"
                    and not attrs.get("restored") and not attrs.get("assumed_state")
                    and 0 <= now - stamp <= policy.max_age_seconds)
        except (ValueError, TypeError, OverflowError):
            return False

    def _observe_aggregate_baseline(self):
        """Keep bounded pre-command reports; never wait before sending Off."""
        if self._aggregate_baseline is None:
            return
        now = self._wall_clock()
        value, reported = (self._aggregate_sample()
            if self._aggregate_heating(self._state(self.bindings.heater), now) else (None, None))
        self._aggregate_baseline.observe(now, value, reported)

    def _observe_aggregate_stop(self):
        if self._aggregate_evidence is None:
            return
        value, reported = self._aggregate_sample()
        native_off = None
        if (self._heater_off(fresh=True) and not self._shutdown_contradicted
                and self._heater_current_stopped() is not False):
            try:
                native_off = _timestamp(_state_parts(self._state(self.bindings.heater))[2])
            except (ValueError, TypeError, OverflowError):
                pass
        self._aggregate_outcome = self._aggregate_evidence.observe(
            self._wall_clock(), value, reported, native_off_after=native_off)
        if (self._off_confirmed and self._heater_off(fresh=True)
                and not self._shutdown_contradicted):
            self._shutdown_confirmation = ("aggregate_corroborated"
                if self._aggregate_outcome.status == "corroborated" else "state_only")
        elif self._shutdown_confirmation in {"state_only", "aggregate_corroborated"}:
            # The headline must not retain proof that the current nested
            # evidence and shutdown_confirmed property have already lost.
            self._shutdown_confirmation = "unconfirmed"

    async def async_acknowledge_emergency(self):
        """Acknowledge manual recovery; never energize an outlet or shared AC."""
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = self.hass.async_create_task(self._acknowledge_emergency())
        return await asyncio.shield(self._recovery_task)

    def _manual_recovery_evidence(self):
        """Validate current recovery inputs, including after a storage await."""
        if not (self._emergency_ac_latched or self._manual_recovery_required):
            raise RuntimeError("No emergency or wiring recovery is pending")
        if (not self.live or self._closed or self._positive_tasks
                or self._service_tasks or self._compensation_pending
                or self.pending_external_jobs()
                or (self._save_task is not None and not self._save_task.done())
                or (self._action_task is not None and not self._action_task.done())):
            raise RuntimeError("Wait for all actual heater and AC service jobs to finish")
        if self._emergency_ac_latched and not self._ac_observed("on", after=self._emergency_ac_request_wall):
            raise RuntimeError("Manually restore and verify AC output before acknowledging recovery")
        state, attrs, reported = _state_parts(self._state(self.bindings.heater))
        barrier = self._manual_recovery_wall or self._emergency_ac_request_wall or 0
        if self._emergency_ac_latched:
            ac_state = self._state(getattr(self.bindings, "emergency_ac_switch", None))
            changed = (ac_state.get("last_changed") if isinstance(ac_state, dict)
                       else getattr(ac_state, "last_changed", None))
            try:
                barrier = max(barrier, _timestamp(changed))
            except (ValueError, TypeError, OverflowError):
                raise RuntimeError("Fresh AC restoration feedback is required") from None
        try:
            stamp = _timestamp(reported)
            fresh = (stamp > barrier and not attrs.get("restored") and not attrs.get("assumed_state")
                     and -5 <= self._wall_clock() - stamp <= TELEMETRY_MAX_AGE_SECONDS)
        except (ValueError, TypeError, OverflowError):
            fresh = False
        reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
        if state != "off" or attrs.get("hvac_action") in ("heating", "cooling") or not fresh:
            raise RuntimeError("A fresh thermostat Off report is required after manual recovery")
        if not reading.safe_to_rearm:
            raise RuntimeError("Fresh, safe electrical readings and battery above 40% are required")
        if self.bindings.heater_current is not None and self._heater_current_stopped(after=barrier) is not True:
            raise RuntimeError("Fresh low heater current after restoration is required")
        return barrier, reading

    def _manual_recovery_generation(self):
        return (self._manual_recovery_wall, self._emergency_ac_request_wall,
                self._emergency_ac_attempts, self._emergency_ac_latched,
                self._manual_recovery_required, self._identity_mismatch,
                self._latched, self._trip_wall, self._cut_owned,
                self._off_request_wall, self._shutdown_contradicted)

    async def _acknowledge_emergency(self):
        barrier, reading = self._manual_recovery_evidence()
        generation = self._manual_recovery_generation()
        # Publish/retain inhibition until the prospective cleared state is
        # durable. A cancelled button caller cannot cancel this actual save.
        prospective = {**self._saved_state(), "emergency_ac_latched": False,
                       "manual_recovery_required": False, "switch_cut_owned": False,
                       "binding_identity": self._binding_identity(),
                       "emergency_ac_request_wall": None, "emergency_ac_attempts": 0,
                       "emergency_ac_reason": None}
        revalidation_error = None
        try:
            if self._store is not None:
                async with self._store_lock:
                    await self._store.async_save(prospective)
                    try:
                        current_barrier, reading = self._manual_recovery_evidence()
                        if (current_barrier != barrier
                                or self._manual_recovery_generation() != generation):
                            raise RuntimeError("Recovery changed during acknowledgement; verify it again")
                    except RuntimeError as exc:
                        # HA events and actual service work can advance during
                        # the write. Retain inhibition in memory and storage;
                        # an older acknowledgement must not erase a new fault.
                        revalidation_error = exc
                        await self._store.async_save(self._saved_state())
        except Exception as exc:
            self._persistence_error = "Cannot persist manual heater recovery acknowledgement"
            self._publish()
            raise RuntimeError(self._persistence_error) from exc
        if revalidation_error is not None:
            self._publish()
            raise revalidation_error
        self._emergency_ac_latched = self._manual_recovery_required = self._identity_mismatch = False
        self._saved_binding_identity = self._binding_identity()
        self._cut_owned = False
        self._shutdown_contradicted = False
        self._persistence_error = None
        self._emergency_ac_status = "acknowledged"
        self._emergency_ac_request_wall = None
        self._emergency_ac_attempts = 0
        self._emergency_ac_reason = None
        self._manual_recovery_wall = None
        self._off_confirmed = True
        self._manual_off_wall = barrier
        self._safe_since = None
        self._reading = reading
        self._schedule_check()
        self._publish()
        return True

    async def async_request_heater_off(self):
        """Apply configured stop stages to an explicitly requested heater stop."""
        if not self.live or self._closed:
            raise RuntimeError("Heater power guard is not available for live shutdown")
        if self._emergency_ac_latched:
            return self.shutdown_confirmed
        if self._action_task is None or self._action_task.done():
            self._action_task = self.hass.async_create_task(self._shed())
        # A caller's timeout/cancellation is not cancellation of an actual stop.
        await asyncio.shield(self._action_task)
        return self.shutdown_confirmed

    def _start_action(self, coroutine):
        if self._closed or not self.live or (self._action_task is not None and not self._action_task.done()):
            coroutine.close()
            return
        self._action_task = self.hass.async_create_task(coroutine)

    def _trip(self, reason):
        if not self._latched:
            self._latched = True
            self._tripped_at = self._clock()
            self._trip_wall = self._wall_clock()
            self._restore_attempts = 0
            self._off_confirmed = False
            _LOGGER.warning("Living-room heater power guard: %s; heater Off requested", reason)
            self._save()
        self._blocked = True
        self._reason = reason
        self._safe_since = None

    def _observe(self):
        if self._closed:
            return
        self._reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
        self._observe_aggregate_baseline()
        if self._persistence_error and self._clock() >= self._save_retry_at:
            self._save_retry_at = self._clock() + 60
            self._save()
        self._observe_aggregate_stop()
        if self._heater_off(fresh=True) and self._heater_current_stopped() is False:
            self._shutdown_contradicted = True
            self._off_confirmed = False
            self._shutdown_confirmation = "unconfirmed"
            if self.live and not self._emergency_ac_latched and not self._closing:
                self._trip("Heater current contradicts the requested Off state")
                self._start_action(self._followup_shed())
        if self._emergency_ac_latched:
            self._blocked = self._manual_recovery_required = True
            if self._emergency_cut_observed() and not self._emergency_service_tasks:
                self._emergency_ac_status = "confirmed_off"
                self._shutdown_confirmation = "emergency_ac_off"
            elif self._emergency_ac_status != "requested" or not self._emergency_service_tasks:
                self._emergency_ac_status = "unconfirmed"
            self._reason = ("Emergency shared AC cutoff latched; manual recovery required"
                            if self._emergency_ac_status == "confirmed_off"
                            else "Emergency shared AC cutoff requested but unconfirmed; manual verification required")
            self._publish()
            return
        if self._closing:
            self._blocked = True
            self._reason = "Waiting for heater power restoration to stop"
            if self.live and self._cut_owned and not self.power_cut_confirmed:
                self._schedule_compensating_cut()
            self._publish()
            return
        if (self._cut_owned and not self._restoring and not self._cutting
                and self._switch_observed("on") and not self._positive_tasks
                and not self._compensation_pending):
            self._manual_power_seen = True
        if (self._cut_owned and not self._restoring and not self._cutting
                and self._switch_observed("off") and self._manual_power_seen):
            # An observed user On→Off after startup relinquishes our power
            # restoration claim. Recreated Off feedback on reconnect does not.
            self._cut_owned = False
            self._cut_changed = None
            self._manual_power_seen = False
            self._save()
        r = self._reading
        if self._manual_recovery_required and not r.issue:
            self._blocked = True
            self._reason = "Heater wiring/ownership changed; explicit manual recovery acknowledgement required"
            self._publish()
            return
        if r.issue:
            self._blocked = True
            self._reason = r.issue
            self._safe_since = None
            # A delayed sensor during startup inhibits a new call immediately,
            # but does not manufacture a five-minute trip if the heater is Off.
            if not r.telemetry_missing or not self._heater_off() or self._latched:
                self._trip(r.issue)
                if self.live and not self._restoring:
                    if self._off_confirmed and (not self._heater_off() or self._heater_current_stopped() is False):
                        self._off_confirmed = False
                        self._start_action(self._followup_shed())
                    elif not self._off_confirmed and not self.power_cut_confirmed:
                        self._start_action(self._shed())
        elif self._latched:
            self._blocked = True
            self._reason = "Heater held off until battery >40%, stable safe load, and cooldown"
            if not self._restoring and not self._off_confirmed and not self.power_cut_confirmed:
                self._start_action(self._shed())
            if (self._off_confirmed and not self._restoring
                    and (not self._heater_off() or self._heater_current_stopped() is False)):
                self._off_confirmed = False
                self._start_action(self._followup_shed())
            if r.safe_to_rearm:
                self._safe_since = self._safe_since if self._safe_since is not None else self._clock()
                ready = (self._clock() - self._safe_since >= SAFE_LOAD_SECONDS
                         and self._clock() - self._tripped_at >= COOLDOWN_SECONDS)
                if (ready and self.live and not self._restoring and not self._positive_tasks
                        and not self._compensation_pending and not self._persistence_error):
                    switch = _state_parts(self._state(self.bindings.heater_switch))[0]
                    if self._cut_owned and switch == "off":
                        if self.bindings.heater_current is None:
                            self._reason = "Dedicated outlet recovery requires manual verification without a heater current meter"
                        elif self._restore_attempts < MAX_RESTORE_ATTEMPTS:
                            self._start_action(self._restore())
                        else:
                            self._reason = "Heater power restoration failed three times; check the dedicated plug and Dyson"
                    elif self._heater_off(fresh=True):
                        self._clear()
            else:
                self._safe_since = None
        else:
            self._blocked = False
            if self.bindings.heater_switch is None:
                self._reason = (None if self._emergency_enabled and self._ac_observed("on")
                                else "Direct heater path requires an enabled, available emergency AC cutoff")
            else:
                self._reason = (None if self._switch_observed("on")
                                else "Heater dedicated plug is off or unavailable")
        self._publish()

    def _clear(self):
        if self._emergency_ac_latched or self._manual_recovery_required:
            return
        self._latched = self._blocked = self._cut_owned = False
        self._restore_intent = False
        self._reason = None
        self._tripped_at = self._trip_wall = self._safe_since = None
        self._restore_attempts = 0
        self._off_request_wall = None
        self._manual_off_wall = None
        self._shutdown_contradicted = False
        self._save()
        _LOGGER.info("Living-room heater power guard recovered; normal room demand may request heat")

    def _save(self):
        if self._store is None or not self.live:
            return
        self._save_dirty = True
        if self._save_task is None or self._save_task.done():
            self._save_task = self.hass.async_create_task(self._async_save())

    async def _async_save(self):
        while self._save_dirty:
            self._save_dirty = False
            try:
                async with self._store_lock:
                    await self._store.async_save(self._saved_state())
                self._persistence_error = None
            except Exception:
                self._persistence_error = "Cannot persist heater cutoff; automatic power restoration inhibited"
                self._save_retry_at = self._clock() + 60
                _LOGGER.exception("Could not persist living-room heater power guard")
            self._publish()

    def _saved_state(self):
        return {"latched": self._latched,
                "switch_cut_owned": self._cut_owned and not self._restore_intent,
                "restoring": self._restore_intent, "trip_wall": self._trip_wall,
                "restore_attempts": self._restore_attempts,
                "binding_identity": self._saved_binding_identity,
                "manual_recovery_required": self._manual_recovery_required,
                "emergency_ac_latched": self._emergency_ac_latched,
                "emergency_ac_reason": self._emergency_ac_reason,
                "emergency_ac_request_wall": self._emergency_ac_request_wall,
                "emergency_ac_attempts": self._emergency_ac_attempts}

    async def _persist_restore_intent(self):
        """Relinquish durable auto-On ownership BEFORE energizing the plug.

        If a later clear write fails and the user switches the restored plug Off,
        a restart must not reinterpret that manual Off as our old cutoff. A
        failed/interrupted restoration only regains durable ownership after a
        new, observed guard cutoff.
        """
        self._restore_intent = True
        if self._store is None:
            return True
        try:
            async with self._store_lock:
                await self._store.async_save(self._saved_state())
            self._persistence_error = None
            return True
        except Exception:
            self._persistence_error = "Cannot persist heater restoration intent; power remains off"
            self._save_retry_at = self._clock() + 60
            _LOGGER.exception("Could not persist heater power restoration intent")
            return False

    def _service(self, domain, service, entity, **data):
        if not self.live:
            raise RuntimeError("Shadow power guard forbids service calls")
        shared = entity == getattr(self.bindings, "emergency_ac_switch", None)
        if shared and (domain != "switch" or service != "turn_off"):
            raise RuntimeError("The emergency guard never turns shared AC On")
        task = self.hass.async_create_task(self.hass.services.async_call(
            domain, service, {"entity_id": entity, **data}, blocking=True))
        self._service_tasks.add(task)
        if shared:
            self._emergency_service_tasks.add(task)
        positive = domain == "switch" and service == "turn_on"
        if positive:
            self._positive_tasks.add(task)

        def finished(done):
            self._service_tasks.discard(done)
            self._emergency_service_tasks.discard(done)
            self._positive_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve errors even after the guard abandoned a hung call.
            if positive and (done in self._invalid_positive or self._closing or self._closed):
                self._invalid_positive.discard(done)
                self._schedule_compensating_cut()
            self._publish()

        task.add_done_callback(finished)
        return task

    async def _bounded_service(self, domain, service, entity, **data):
        task = self._service(domain, service, entity, **data)
        done, _ = await asyncio.wait((task,), timeout=SERVICE_TIMEOUT_SECONDS)
        if task not in done:
            if task in self._positive_tasks:
                # Keep tracking actual completion; cancelling an integration's
                # coroutine is not proof its delayed power-On was abandoned.
                self._invalid_positive.add(task)
                self._compensation_pending = True
            elif task not in self._emergency_service_tasks:
                task.cancel()
            return False
        try:
            task.result()
            return True
        except (Exception, asyncio.CancelledError):
            return False

    async def _off_ack(self, seconds):
        contradictory = False
        self._shutdown_contradicted = False
        self._manual_off_wall = None
        policy = getattr(self.bindings, "aggregate_stop_confirmation", None)
        if policy is not None:
            self._observe_aggregate_baseline()
            self._aggregate_evidence = policy.create_evidence()
            value, reported = self._aggregate_sample()
            now = self._wall_clock()
            baseline = (self._aggregate_baseline.snapshot(now)
                        if self._aggregate_baseline is not None else None)
            self._aggregate_outcome = self._aggregate_evidence.begin(
                now, value, reported, qualified_baseline_w=baseline)
        else:
            self._aggregate_evidence = self._aggregate_outcome = None
        self._shutdown_stage = "dyson_off"
        self._shutdown_confirmation = "unconfirmed"
        self._off_request_wall = self._wall_clock()
        task = self._service("climate", "set_hvac_mode", self.bindings.heater, hvac_mode="off")
        deadline = time.monotonic() + seconds
        while not self._closed and time.monotonic() < deadline:
            if self._restoring and evaluate_power(self._states(), self.bindings, self._wall_clock()).issue:
                task.cancel()
                return False
            if self._heater_off(fresh=True):
                meter_stop = self._heater_current_stopped()
                if meter_stop is not None:
                    contradictory = not meter_stop
                    self._shutdown_contradicted = contradictory
                self._observe_aggregate_stop()
                if not contradictory:
                    self._off_confirmed = True
                    self._shutdown_confirmation = "state_only"
                    if self._aggregate_outcome and self._aggregate_outcome.status == "corroborated":
                        self._shutdown_confirmation = "aggregate_corroborated"
                    # Corroboration continues on ordinary state/timer updates.
                    # Waiting here could outlive native service/stop budgets.
                    self._publish()
                    return True
            if task.done() and (task.cancelled() or task.exception()):
                break
            await asyncio.sleep(min(.1, seconds / 5))
        if not task.done():
            task.cancel()  # Do not await cancellation: integrations can ignore it.
        # Inconclusive aggregate evidence is not evidence of continued heating.
        # A fresh native Off acknowledgement remains state-only confirmation.
        if self._heater_off(fresh=True) and not contradictory:
            self._off_confirmed = True
            self._shutdown_confirmation = "state_only"
            return True
        return False

    async def _shed(self):
        if self._emergency_ac_latched:
            return
        if self.power_cut_confirmed:
            return
        if await self._off_ack(OFF_ACK_SECONDS):
            self._publish()
            return
        if not self._latched:
            self._trip("Requested heater shutdown remains unconfirmed")
        if self.bindings.heater_switch is not None and await self._cut_switch():
            self._shutdown_confirmation = "dedicated_cutoff"
        else:
            await self._cut_emergency_ac()
        self._publish()

    async def _followup_shed(self):
        # Preserve the dedicated plug's immediate response to late heat after
        # an acknowledged stop. Shared AC still follows its own final stage.
        if self.bindings.heater_switch is None:
            await self._shed()
        elif not await self._cut_switch():
            await self._cut_emergency_ac()

    async def _cut_switch(self, *, force=False):
        if not self.live or self.bindings.heater_switch is None:
            return False
        self._shutdown_stage = "dedicated_off"
        self._cutting = True
        switch = _state_parts(self._state(self.bindings.heater_switch))[0]
        if switch == "on":
            self._cut_owned = True
            self._save()
        if switch != "off" or force:
            self._cut_request_wall = self._wall_clock()
            _LOGGER.warning("Living-room heater did not confirm Off; cutting its dedicated plug")
            await self._bounded_service("switch", "turn_off", self.bindings.heater_switch)
            deadline = time.monotonic() + SERVICE_TIMEOUT_SECONDS
            while not self._switch_observed("off", after=self._cut_request_wall) and time.monotonic() < deadline:
                await asyncio.sleep(min(.1, SERVICE_TIMEOUT_SECONDS / 5))
            if not self._switch_observed("off", after=self._cut_request_wall):
                self._reason = "Heater Off and dedicated plug cutoff are unconfirmed; check the heater"
                _LOGGER.error(self._reason)
            else:
                self._cut_changed = self._switch_changed()
                self._save()
        self._publish()
        self._manual_power_seen = False
        self._cutting = False
        return self.power_cut_confirmed

    async def _cut_emergency_ac(self):
        """One disruptive request per emergency; no AC restore or retry loop."""
        if not self.live or not self._emergency_enabled:
            self._shutdown_confirmation = "unconfirmed"
            self._reason = "Heater shutdown unconfirmed; shared AC fallback is not enabled"
            return False
        if self._emergency_ac_latched:
            return self._emergency_cut_observed() and not self._emergency_service_tasks
        self._emergency_ac_latched = self._manual_recovery_required = self._blocked = True
        self._emergency_ac_status = "requested"
        self._shutdown_stage = "emergency_ac_off"
        self._shutdown_confirmation = "unconfirmed"
        self._emergency_ac_reason = self._reason or "Heater shutdown could not be confirmed"
        self._emergency_ac_request_wall = self._wall_clock()
        self._manual_recovery_wall = self._emergency_ac_request_wall
        self._emergency_ac_attempts += 1
        self._invalid_positive.update(self._positive_tasks)
        self._save()
        if self._save_task is not None and not self._save_task.done():
            await asyncio.wait((self._save_task,), timeout=SERVICE_TIMEOUT_SECONDS)
            if not self._save_task.done():
                self._persistence_error = "Emergency cutoff persistence pending; manual verification required"
        _LOGGER.warning("Heater shutdown unconfirmed; requesting explicitly enabled shared AC output Off")
        self._publish()
        await self._bounded_service("switch", "turn_off", self.bindings.emergency_ac_switch)
        deadline = time.monotonic() + SERVICE_TIMEOUT_SECONDS
        while not self._closed and time.monotonic() < deadline:
            if self._emergency_cut_observed() and not self._emergency_service_tasks:
                self._emergency_ac_status = "confirmed_off"
                self._shutdown_confirmation = "emergency_ac_off"
                break
            await asyncio.sleep(min(.1, SERVICE_TIMEOUT_SECONDS / 5))
        else:
            self._emergency_ac_status = "unconfirmed"
            _LOGGER.error("Shared AC cutoff requested but unconfirmed; verify equipment locally")
        self._publish()
        return self._emergency_ac_status == "confirmed_off"

    def _schedule_compensating_cut(self):
        self._blocked = self._latched = self._compensation_pending = True
        self._force_cut_again = True
        if self._compensating_task is None or self._compensating_task.done():
            self._compensating_task = self.hass.async_create_task(self._compensate())

    async def _compensate(self):
        while self._force_cut_again:
            self._force_cut_again = False
            await self._cut_switch(force=True)
        self._compensation_pending = bool(self._positive_tasks) or not self._switch_cut_observed()
        self._publish()

    async def _restore(self):
        if (self.bindings.heater_switch is None or self._emergency_ac_latched
                or self._manual_recovery_required):
            return
        self._restoring = True
        self._restore_attempts += 1
        self._save()
        self._publish()
        success = False
        try:
            reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
            if not reading.safe_to_rearm or self._closed:
                return
            if not await self._persist_restore_intent() or self._closing:
                return
            requested = self._wall_clock()
            if not await self._bounded_service("switch", "turn_on", self.bindings.heater_switch):
                return
            deadline = time.monotonic() + RESTORE_ACK_SECONDS
            while (not self._switch_observed("on", after=requested)
                   and not self._closing and time.monotonic() < deadline):
                if evaluate_power(self._states(), self.bindings, self._wall_clock()).issue:
                    return
                await asyncio.sleep(min(.1, RESTORE_ACK_SECONDS / 5))
            if not self._switch_observed("on", after=requested) or self._closing:
                return
            # Power recovery never means heat recovery.  Supersede a retained
            # thermostat mode, then require fresh Off before opening the gate.
            if await self._off_ack(RESTORE_ACK_SECONDS):
                reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
                if (reading.safe_to_rearm and not self._closed and not self._closing
                        and self._switch_observed("on", after=requested)):
                    self._reading = reading
                    self._clear()
                    success = True
        finally:
            if not success:
                await self._cut_switch(force=True)
                self._restore_intent = False
                self._tripped_at = self._clock()
                self._trip_wall = self._wall_clock()
                self._safe_since = None
                self._save()
            self._restoring = False
            self._publish()

    async def async_close(self):
        """Stop observation; never restore power or enable heat during teardown."""
        if self._closed:
            return True
        first = not self._closing
        self._closing = True
        self._blocked = True
        tasks = [t for t in (self._check_task, self._action_task) if t and not t.done()]
        if first:
            if self._check_task is not None and not self._check_task.done():
                self._check_task.cancel()
            # Negative shutdown must finish even if unload arrives between
            # persisting emergency intent and dispatching the actual AC Off.
            # Positive restoration still gets cancelled and compensated.
            if self._restoring and self._action_task is not None:
                self._action_task.cancel()
            self._invalid_positive.update(self._positive_tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=SERVICE_TIMEOUT_SECONDS + .5)
        if any(not task.done() for task in tasks):
            self._publish()
            return False
        if self._positive_tasks or (self._compensating_task and not self._compensating_task.done()):
            self._schedule_compensating_cut()
            self._publish()
            return False
        if self._emergency_service_tasks:
            # Timed-out negative calls can ignore cancellation and finish late.
            # Do not release ownership while a shared AC Off is still in flight.
            self._publish()
            return False
        if self._recovery_task is not None and not self._recovery_task.done():
            return False
        if self._emergency_ac_latched and not self.shutdown_confirmed:
            self._publish()
            return False
        if self._emergency_ac_latched and self._persistence_error:
            self._save()
            return False
        if self.live and self._cut_owned and not self._switch_cut_observed():
            await self._cut_switch(force=True)
            if not self._switch_cut_observed():
                return False
        self._compensation_pending = False
        self._closed = True
        for remove in self._unsubscribers:
            remove()
        self._unsubscribers.clear()
        for task in tuple(self._service_tasks):
            task.cancel()
        if self._save_task and not self._save_task.done():
            await asyncio.wait((self._save_task,), timeout=1)
            if not self._save_task.done():
                self._closed = False
                return False
        self._publish()
        return True
