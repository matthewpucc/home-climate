"""Auxiliary supply eligibility and independently verified shutdown/recovery.

Battery reserve and corroborated AC availability govern automated heat. Input,
aggregate output and circuit watts have no software ceilings. Aggregate output
corroborates supplying loads, never heater-only draw or a failed shutdown.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller.bindings import PowerBindings

_LOGGER = logging.getLogger(__name__)

HEATER_OFF_MAX_W = 100.0  # Non-heating evidence, not an amp or appliance-rating conversion.
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
COMMISSIONING_MAX_SECONDS = 600.0
COMMISSIONING_METER_ACQUIRE_SECONDS = 30.0
COMMISSIONING_REPORT_GAP_SECONDS = 30.0
COMMISSIONING_RECONNECT_SECONDS = 300.0
COMMISSIONING_METER_CONFIRM_SECONDS = 30.0


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


def _diagnostic_timestamp(value):
    """Retain safe numeric history metadata without granting any control lease."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


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
    input_budget_w: float | None = None
    issue: str | None = None
    telemetry_missing: bool = False
    wall_load_only: bool = False
    heater_w: float | None = None
    circuit_issue: str | None = None

    ac_enabled: bool | None = None
    supply_reported_at: float | None = None
    meter_issue: str | None = None
    diagnostic_issues: tuple = ()
    supply_evidence: str = "unverified"

    @property
    def supplying_loads(self):
        return self.ac_enabled is True and self.output_w is not None and self.output_w > 0

    @property
    def safe_to_rearm(self):
        return (not self.issue and self.ac_enabled is True
                and self.battery_percent > BATTERY_RESERVE_PERCENT
                and not self.meter_issue
                and (self.heater_a is None or self.heater_a < 1)
                and (self.heater_w is None or self.heater_w < HEATER_OFF_MAX_W))


def _device_feedback(state, now, *, after=None):
    try:
        from .controller.device_feedback import device_feedback
    except ImportError:
        from house_climate.device_feedback import device_feedback
    return device_feedback(state, now, after=after)


def _corroborated_ac(states, bindings, now, *, output_valid):
    """Read AC intent with independent, current supply telemetry.

    An unchanged event-driven switch need not manufacture a new report. Its
    available On state is corroborated by the separately bound battery/output
    reports; this is not a claim of a physical AC read or acquisition time.
    Explicit, current physical feedback contradicting the HA state wins.
    """
    entity = bindings.ac_output_switch or bindings.emergency_ac_switch
    state = states.get(entity)
    value, attrs, reported = _state_parts(state)
    if value not in ("on", "off") or attrs.get("restored") or attrs.get("assumed_state"):
        return None, None, "unverified"
    try:
        stamp = _timestamp(reported)
        if stamp > now + 5:
            return None, None, "unverified"
    except (ValueError, TypeError, OverflowError):
        return None, None, "unverified"
    physical, physical_stamp = _device_feedback(state, now)
    proof = attrs.get("device_report")
    if isinstance(proof, Mapping) and proof.get("state") in ("on", "off"):
        try:
            age = now - _timestamp(proof.get("reported_at"))
            if -5 <= age <= TELEMETRY_MAX_AGE_SECONDS and proof["state"] != value:
                return None, None, "contradictory"
        except (ValueError, TypeError, OverflowError):
            pass
    if physical is not None:
        return physical, physical_stamp, "device_report"
    if value == "off":
        return value, stamp, "reported_off"
    if not output_valid:
        return None, None, "unverified"
    # Zero watts is inconclusive before startup, not proof that AC is Off.
    _, _, output_report = _state_parts(states.get(bindings.output_power))
    return value, _timestamp(output_report), "corroborating_reports"


def evaluate_power(states, bindings: PowerBindings, now=None):
    """Eligibility uses corroborating supply signals, with no watt ceilings."""
    now = time.time() if now is None else now
    values, missing, advisory, meter_issues = {}, [], [], []
    specs = (
        ("battery_percent", bindings.battery, {"%": 1}, BATTERY_MAX_AGE_SECONDS, missing),
        ("output_w", bindings.output_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS, missing),
        ("input_w", bindings.input_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS, advisory),
        ("circuit_w", bindings.circuit_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS, advisory),
        ("heater_a", bindings.heater_current, {"A": 1, "mA": .001}, TELEMETRY_MAX_AGE_SECONDS, meter_issues),
        ("heater_w", bindings.heater_power, {"W": 1, "kW": 1000}, TELEMETRY_MAX_AGE_SECONDS, meter_issues),
    )
    for key, entity, units, age, errors in specs:
        if entity is None and key in {"heater_a", "heater_w"}:
            continue
        try:
            _, attrs, _ = _state_parts(states.get(entity))
            if attrs.get("restored") or attrs.get("assumed_state"):
                raise ValueError(f"{entity}: restored or assumed report")
            values[key] = _measurement(states, entity, units, now, maximum_age=age)
        except ValueError as exc:
            errors.append(str(exc))
            if key == "circuit_w":
                values["circuit_issue"] = str(exc)
    phases = []
    for entity in (bindings.wall_phase_a_voltage, bindings.wall_phase_b_voltage):
        try:
            phases.append(_measurement(states, entity, {"V": 1}, now))
        except ValueError as exc:
            advisory.append(str(exc))
    values["voltage_v"] = min(phases) if len(phases) == 2 else None
    ac, stamp, evidence = _corroborated_ac(states, bindings, now,
        output_valid="output_w" in values and "battery_percent" in values)
    values.update(ac_enabled=(ac == "on" if ac is not None else None), supply_reported_at=stamp,
                  supply_evidence=evidence, diagnostic_issues=tuple(advisory),
                  meter_issue="; ".join(meter_issues) or None)
    if ac is None:
        missing.append("AC output requires an available On report corroborated by current battery and output telemetry")
    battery = values.get("battery_percent")
    if battery is not None and battery > 100:
        missing.append("Battery percentage outside plausible bounds")
    issue = ("Battery is at or below the 40% network reserve" if battery is not None and battery <= 40 else
             "Battery AC output is disabled" if ac == "off" else
             "Power telemetry unavailable: " + "; ".join(missing) if missing else None)
    return PowerReading(**values, issue=issue, telemetry_missing=bool(missing))


class HeaterPowerGuard:
    """HA-loop guard independent of comfort ownership and the worker heartbeat."""

    def __init__(self, hass, bindings: PowerBindings, *, live, entry_id, notify, clock=time.monotonic, wall_clock=time.time):
        if not is_dataclass(bindings):
            raise TypeError("A validated PowerBindings dataclass is required")
        for role, domain in (("heater", "climate"), ("heater_switch", "switch"),
                             *((role, "sensor") for role in ("battery", "circuit_power", "input_power",
                                "output_power", "heater_current", "heater_power",
                                "wall_phase_a_voltage", "wall_phase_b_voltage"))):
            entity = getattr(bindings, role, None)
            if entity is None and role in ("heater_switch", "heater_current", "heater_power"):
                continue
            if not isinstance(entity, str) or not entity.startswith(domain + "."):
                raise ValueError(f"Invalid {role} power binding")
        self.bindings = bindings
        if bindings.heater_current is not None and getattr(bindings, "heater_power", None) is not None:
            raise ValueError("Choose either a heater current or heater power meter")
        self.entity_ids = bindings.entity_ids
        self.hass, self.live, self.entry_id, self.notify = hass, live, entry_id, notify
        self.manual_override = lambda: False
        self.alerts = None
        self._low_battery_alert_active = False
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
        self._wall_observed_off_wall = None
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
        self._recovery_feedback = {}
        self.async_read_recovery_feedback = None
        self._shutdown_fault = False
        self._legacy_cooldown = False
        self._restore_approved_until = None
        self._store_lock = asyncio.Lock()
        self._restore_intent = False
        self._manual_power_seen = False
        self._cutting = False
        # A confirmed relay cut can outlive its meter's reporting period.
        # Keep observed isolation separate from electrical sample freshness;
        # never persist this process-local continuity proof across a restart.
        self._isolation_after = self._wall_clock()
        self._isolation_report = None
        self._isolation_changed = None
        self._isolation_revision = 0
        self._last_observation_wall = None
        self._restore_interrupted = False
        self._restore_on_requested = False
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
        # A loaded ownership claim is not fresh proof that the relay is Off.
        # This process-local boundary permits explicit, non-actuating recovery
        # if the relay is already On; it never grants an automatic On lease.
        self._retained_cut_claim_after = None
        self._identity_mismatch = False
        self._saved_binding_identity = self._binding_identity()
        self._shutdown_confirmation = "unconfirmed"
        self._shutdown_contradicted = False
        self._fan_only_authorized = False
        self._fan_only_task = None
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
        # Runtime callbacks distinguish stopped commissioning from stopped auxiliary
        # work during recovery. A standalone guard fails closed.
        self.commissioning_controller_off = lambda: False
        self.recovery_controller_off = lambda: False
        self._commissioning_task = None
        self._commissioning_preparing = False
        self._commissioning_started_clock = None
        self._commissioning_caller_cancelled = False
        # Process-local supervision; a restart must never resume an energized
        # commissioning wait from cached low meter data.
        self._supervised_restore = None
        self._commissioning = {"active": False, "status": "idle", "reason": None,
            "started_at": None, "finished_at": None,
            "restore_window_seconds": RESTORE_ACK_SECONDS,
            "maximum_seconds": COMMISSIONING_MAX_SECONDS}

    def _state(self, entity):
        state = self.hass.states.get(entity) if entity is not None else None
        proof = self._recovery_feedback.get(entity)
        if proof is None or state is None:
            return state
        value, attrs, reported = _state_parts(state)
        changed = state.get("last_changed") if isinstance(state, dict) else state.last_changed
        if value != proof["state"] or changed != proof["last_changed"]:
            self._recovery_feedback.pop(entity, None)
            return state
        return {"state": value, "attributes": {**attrs, "device_report": proof},
                "last_reported": reported, "last_changed": changed}

    def _binding_identity(self):
        identity = {key: getattr(self.bindings, key, None) for key in (
            "heater", "heater_switch", "heater_current", "emergency_ac_switch")}
        # An absent optional watt meter must compare identically with legacy
        # four-role identities. A real meter change still fences old claims.
        power = getattr(self.bindings, "heater_power", None)
        if power is not None:
            identity["heater_power"] = power
        return identity

    def _heater_meter(self):
        """Select one dedicated electrical signal without converting W to A."""
        entity = getattr(self.bindings, "heater_power", None)
        if entity is not None:
            return entity, {"W": 1, "kW": 1000}, HEATER_OFF_MAX_W, "power"
        return self.bindings.heater_current, {"A": 1, "mA": .001}, 1.0, (
            "current" if self.bindings.heater_current is not None else None)

    @property
    def _heater_meter_configured(self):
        return self._heater_meter()[0] is not None

    def _invalidate_isolation(self, now):
        self._isolation_after = max(self._isolation_after, now)
        self._isolation_report = self._isolation_changed = None
        self._isolation_revision += 1
        self._safe_since = None

    def _observe_isolation(self, now):
        """Remember a real owned Off while its uninterrupted state remains Off."""
        value, attrs, reported = _state_parts(self._state(self.bindings.heater_switch))
        if (not self._cut_owned or self._identity_mismatch or self._manual_power_seen
                or self._manual_recovery_required or self._emergency_ac_latched
                or value != "off" or attrs.get("restored") or attrs.get("assumed_state")):
            if self._isolation_report is not None:
                self._invalidate_isolation(now)
            return False
        changed = self._switch_changed()
        if self._isolation_report is not None:
            if changed != self._isolation_changed:
                self._invalidate_isolation(now)
                return False
            return True
        try:
            _, physical_stamp = _device_feedback(self._state(self.bindings.heater_switch), now)
            stamp = physical_stamp if physical_stamp is not None else _timestamp(reported)
            after = max(self._isolation_after, self._cut_request_wall or 0)
            if stamp <= after or not -5 <= now - stamp <= TELEMETRY_MAX_AGE_SECONDS:
                return False
        except (ValueError, TypeError, OverflowError):
            return False
        self._isolation_report, self._isolation_changed = stamp, changed
        self._isolation_revision += 1
        return True

    def _external_power(self):
        """Read reserve/supply eligibility without inventing an absent heater value."""
        bindings = replace(self.bindings, heater_current=None, heater_power=None)
        return evaluate_power(self._states(), bindings, self._wall_clock())

    def _isolated_recovery_reading(self):
        if (not self._heater_meter_configured or not self._observe_isolation(self._wall_clock())
                or self._positive_tasks or self._service_tasks or self._compensation_pending
                or self.pending_external_jobs() or self._persistence_error):
            return None
        # Actual post-cut high load contradicts isolation. Missing/expired
        # samples stay unknown; the relay, rather than a fabricated zero,
        # supplies the separate isolation evidence.
        if self._heater_meter_stopped(after=self._isolation_report) is False:
            return None
        return self._external_power()

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

    def _fan_recovery_ready(self):
        """Only a proved ordinary stop can become fan-only during a heat hold."""
        if (not self._latched or self.manual_control or self._closing or self._closed
                or self._persistence_error or self._shutdown_fault or self._shutdown_contradicted
                or self._manual_recovery_required or self._emergency_ac_latched or self._identity_mismatch
                or self._cut_owned or self._legacy_cooldown or self.commissioning_busy
                or self._restoring or self._cutting or self._positive_tasks or self._service_tasks
                or self._compensation_pending or (self._action_task and not self._action_task.done())
                or self._external_power().issue
                or (self.bindings.heater_switch is not None and not self._switch_observed("on"))
                or (self.bindings.heater_switch is None and not self._emergency_enabled)
                or (self._emergency_enabled and not self._ac_observed("on"))):
            return False
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if any(job is not current and job is not self._fan_only_task
               for job in self.pending_external_jobs()):
            return False
        if self._heater_meter_stopped() is not True:
            return False
        if self._heater_off(fresh=True) and self.shutdown_confirmed:
            return True
        state, attrs, reported = _state_parts(self._state(self.bindings.heater))
        try:
            return (self._fan_only_authorized and state == "fan_only"
                    and attrs.get("hvac_action") not in ("heating", "cooling")
                    and not attrs.get("restored") and not attrs.get("assumed_state")
                    and 0 <= self._wall_clock() - _timestamp(reported) <= TELEMETRY_MAX_AGE_SECONDS)
        except (ValueError, TypeError, OverflowError):
            return False

    @property
    def fan_blocked(self):
        return self.blocked and not self._fan_recovery_ready()

    @property
    def switch_cut_owned(self):
        return self._cut_owned

    @property
    def commissioning_active(self):
        return self._commissioning["active"]

    @property
    def commissioning_busy(self):
        return bool(self.commissioning_active or (
            self._commissioning["status"] != "idle" and (
                self._restoring or self._positive_tasks or self._service_tasks or self._compensation_pending
                or (self._commissioning_task is not None and not self._commissioning_task.done())
                or (self._action_task is not None and not self._action_task.done())
                or (self._save_task is not None and not self._save_task.done())
                or (self._compensating_task is not None and not self._compensating_task.done()))))

    @property
    def power_cut_confirmed(self):
        """Actual dedicated-plug feedback, not completion of a service call."""
        return (not self._identity_mismatch
                and ((self._switch_cut_observed()
                      and self._heater_meter_stopped(after=self._cut_request_wall) is not False)
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
        if self._off_boundary() is None and self._wall_observed_off_wall is None:
            return self._heater_off()
        if (self._off_boundary() is None and self._wall_observed_off_wall is not None
                and self._heater_meter_configured
                and self._heater_meter_stopped() is not True):
            return False
        return self._off_confirmed and self._heater_off(fresh=True)

    def _off_boundary(self):
        """A verified manual recovery is evidence, not a fabricated command."""
        return max((stamp for stamp in (self._off_request_wall, self._manual_off_wall)
                    if stamp is not None), default=None)

    def _switch_cut_observed(self):
        # Physical isolation and restoration ownership are independent. A user's
        # manual Off is valid isolation proof but never a right to turn it On.
        return self._switch_observed("off", after=self._cut_request_wall)

    def _wall_diagnostics(self):
        return {"enforced_sources": [], "qualified_sources": [], "pending_sources": [], "sources": {
            role: {"enforced": False, "status": "advisory", "latest_w": value,
                   "reading_status": "unavailable" if value is None else "available",
                   "reason": "Diagnostic only; no watt ceiling"}
            for role, value in (("input", self._reading.input_w), ("circuit", self._reading.circuit_w))}}

    def _heater_draw_status(self):
        r = self._reading
        if r.meter_issue or (r.heater_w is None and r.heater_a is None):
            return "unknown"
        substantial = r.heater_w >= HEATER_OFF_MAX_W if r.heater_w is not None else r.heater_a >= 1
        mode, _, _ = _state_parts(self._state(self.bindings.heater))
        if substantial:
            return "heating_draw_observed" if mode == "heat" else "load_without_heat_mode"
        return "idle" if mode == "heat" else "low_load"

    @property
    def manual_control(self):
        """Only explicit durable ownership, never an inferred app adjustment."""
        try:
            return self.manual_override() is True
        except Exception:
            return False

    @property
    def _manual_policy_control(self):
        """A policy-only saved claim does not reclaim deliberate manual heat.

        Actual failed stops, changed bindings and uncertain device work retain
        their existing cleanup obligations even during manual ownership.
        """
        return self.manual_control and not (
            self._shutdown_fault or self._shutdown_contradicted
            or self._manual_recovery_required or self._emergency_ac_latched
            or self._identity_mismatch or self.commissioning_active)

    @property
    def manual_return_issue(self):
        """Returning policy ownership cannot acknowledge electrical faults."""
        if self._persistence_error:
            return self._persistence_error
        if (self._identity_mismatch or self._shutdown_fault or self._shutdown_contradicted
                or self._manual_recovery_required or self._emergency_ac_latched or self._cut_owned):
            return "Complete explicit heater shutdown/cutoff recovery before returning automated control"
        if (self._restoring or self._cutting or self._restore_intent or self._compensation_pending
                or self._positive_tasks or self._service_tasks or self.pending_external_jobs()
                or (self._action_task is not None and not self._action_task.done())
                or (self._save_task is not None and not self._save_task.done())):
            return "Wait for pending heater service and persistence work before returning control"
        return None

    def _shutdown_evidence(self):
        if self._shutdown_contradicted:
            return "contradictory"
        if self.power_cut_confirmed:
            return "corroborated_off"
        if self._heater_off(fresh=True):
            if self._heater_meter_stopped() is True:
                return "corroborated_off"
            return "reported_off_unverified"
        return "not_observed"

    def _shutdown_confirmation_diagnostic(self):
        """Show current acknowledged recovery without inventing a stop command."""
        if (self._manual_off_wall is not None
                and self._off_boundary() == self._manual_off_wall
                and not (self._manual_recovery_required or self._emergency_ac_latched
                         or self._shutdown_fault)):
            return ("manual_recovery_verified"
                    if not self._shutdown_contradicted
                    and self.shutdown_confirmed and self._heater_off(fresh=True)
                    and self._heater_meter_stopped() is True else "unconfirmed")
        return self._shutdown_confirmation

    def _update_alerts(self):
        if self.alerts is None:
            return
        reading = self._reading
        reserve = reading.battery_percent
        if reserve is not None and 0 <= reserve <= 100:
            self._low_battery_alert_active = reserve <= BATTERY_RESERVE_PERCENT
        try:
            self.alerts.update("auxiliary_low_battery", self._low_battery_alert_active,
                "Home Climate: battery reserve low",
                ("The last qualified battery reading is at or below the 40% reserve. Dyson is under manual control; "
                 "Home Climate will not apply the automated reserve cutoff." if self.manual_control else
                 "The last qualified battery reading is at or below the 40% reserve. Automated Dyson heat and circulation are inhibited."),
                severity="critical")
            self.alerts.update("auxiliary_shutdown_fault", bool(self._shutdown_fault or self._shutdown_contradicted
                               or self._emergency_ac_latched),
                "Home Climate: heater shutdown needs attention",
                self._reason or "Heater shutdown or isolation requires explicit recovery.", severity="critical")
            self.alerts.update("auxiliary_retained_recovery", self._retained_cut_recovery_pending,
                "Home Climate: heater recovery pending",
                "A saved heater cutoff claim requires supported recovery before automated use.")
        except Exception:
            # Notification problems must never interrupt stop/isolation work.
            _LOGGER.debug("Power-guard alert update failed", exc_info=True)

    @property
    def diagnostics(self):
        r = self._reading
        blocked = self.blocked
        # A completed request leaves internal evidence available to the control
        # code. Do not display its later expiry as a new active shutdown. This
        # projection never clears, refreshes or otherwise changes that evidence.
        shutdown_active = bool(blocked or self._latched or self._cut_owned
            or self._restoring or self._cutting or self._restore_intent
            or self._manual_recovery_required or self._emergency_ac_latched
            or self.commissioning_active or self._shutdown_contradicted
            or self._off_boundary() is not None or self._wall_observed_off_wall is not None
            or self._service_tasks or self._positive_tasks or self._compensation_pending
            or (self._action_task is not None and not self._action_task.done()))
        return {"blocked": blocked, "reason": self._reason,
                "latched": self._latched, "switch_cut_owned": self._cut_owned,
                "power_cut_confirmed": self.power_cut_confirmed,
                "restoring": self._restoring, "restore_attempts": self._restore_attempts,
                "power_restore_inflight": bool(self._positive_tasks),
                "power_restore_cleanup_pending": self._compensation_pending,
                "heater_cutoff_configured": self.bindings.heater_switch is not None,
                "heater_current_configured": self.bindings.heater_current is not None,
                "heater_power_configured": getattr(self.bindings, "heater_power", None) is not None,
                "heater_meter_kind": self._heater_meter()[3],
                "electrical_stop_confirmed": shutdown_active and self._heater_meter_stopped() is True,
                "shutdown_evidence": self._shutdown_evidence(),
                "shutdown_contradicted": self._shutdown_contradicted,
                "manual_override": self.manual_control,
                "shutdown_active": shutdown_active,
                "shutdown_stage": self._shutdown_stage if shutdown_active else "idle",
                "shutdown_confirmation": self._shutdown_confirmation_diagnostic() if shutdown_active else "inactive",
                "wall_power_budget": self._wall_diagnostics(),
                "circuit_enforced": False,
                "watt_limits_enforced": False,
                "ac_output_enabled": r.ac_enabled, "supply_reported_at": r.supply_reported_at,
                "supply_evidence": r.supply_evidence,
                "supplying_loads": r.supplying_loads, "heater_draw_status": self._heater_draw_status(),
                "heater_meter_issue": r.meter_issue, "diagnostic_issues": r.diagnostic_issues,
                "aggregate_stop": (self._aggregate_outcome.as_dict()
                                   if shutdown_active and self._aggregate_outcome else None),
                "emergency_ac_enabled": self._emergency_enabled,
                "emergency_ac_latched": self._emergency_ac_latched,
                "emergency_ac_status": self._emergency_ac_status,
                "emergency_ac_attempts": self._emergency_ac_attempts,
                "emergency_ac_inflight": bool(self._emergency_service_tasks),
                "emergency_notification_error": self._notice_error,
                "manual_recovery_required": self._manual_recovery_required,
                "shutdown_fault": self._shutdown_fault,
                "restore_approval_pending": self._restore_approved_until is not None,
                "retained_cut_recovery_pending": self._retained_cut_recovery_pending,
                "binding_identity_mismatch": self._identity_mismatch,
                "persistence_error": self._persistence_error,
                "commissioning": dict(self._commissioning),
                "circuit_power_w": r.circuit_w, "elite_input_w": r.input_w,
                "elite_output_w": r.output_w, "battery_percent": r.battery_percent,
                "heater_current_a": r.heater_a, "heater_power_w": r.heater_w,
                "input_budget_w": r.input_budget_w,
                "branch_current_measured": False, "live": self.live}

    def _publish(self):
        self._update_alerts()
        self._publish_emergency_notice()
        current = self.diagnostics
        stable = {key: current[key] for key in (
            "blocked", "reason", "latched", "switch_cut_owned", "power_cut_confirmed",
            "restoring", "restore_attempts", "power_restore_inflight",
            "power_restore_cleanup_pending", "persistence_error", "live",
            "shutdown_active", "shutdown_stage", "shutdown_confirmation", "aggregate_stop",
            "diagnostic_issues", "shutdown_evidence", "shutdown_contradicted", "manual_override", "supply_evidence",
            "emergency_ac_enabled", "emergency_ac_latched", "emergency_ac_status",
            "emergency_ac_attempts", "emergency_ac_inflight", "manual_recovery_required",
            "retained_cut_recovery_pending", "shutdown_fault", "restore_approval_pending",
            "binding_identity_mismatch", "emergency_notification_error", "commissioning")}
        stable["ac_output_enabled"] = self._reading.ac_enabled
        stable["heater_draw_status"] = self._heater_draw_status()
        stable["heater_meter_issue"] = self._reading.meter_issue
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
        return {entity: self._state(entity) for entity in self.entity_ids}

    async def async_load(self):
        from homeassistant.helpers.storage import Store
        loaded_at = self._wall_clock()
        self._invalidate_isolation(loaded_at)
        self._store = Store(self.hass, 1, f"house_climate.{self.entry_id}.power_guard")
        data = await self._store.async_load() or {}
        if not isinstance(data, dict):
            data = {"latched": True}
        self._latched = data.get("latched") is True
        self._shutdown_fault = data.get("shutdown_fault") is True
        self._cut_owned = data.get("switch_cut_owned") is True
        self._legacy_cooldown = bool(self._latched and (data.get("supply_policy_version") != 1
                                                      or data.get("legacy_cooldown") is True))
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
        if isinstance(prior_identity, dict):
            prior_identity = dict(prior_identity)
            if prior_identity.get("heater_power") is None:
                prior_identity.pop("heater_power", None)
        has_claim = self._cut_owned or self._latched or self._emergency_ac_latched
        if (has_claim and prior_identity != self._binding_identity()):
            # Ownership belongs to actual wiring, not just the config-entry ID.
            # Legacy claims have no identity and cannot authorize a new plug On.
            self._identity_mismatch = True
            self._manual_recovery_required = True
            self._saved_binding_identity = prior_identity
            self._cut_owned = False
        self._manual_recovery_required |= data.get("manual_recovery_required") is True
        self._manual_recovery_required |= self._emergency_ac_latched or self._shutdown_fault
        self._retained_cut_claim_after = loaded_at if self._cut_owned else None
        prior_test = data.get("commissioning")
        valid_test = (isinstance(prior_test, dict) and type(prior_test.get("active")) is bool
                      and prior_test.get("status") in {"idle", "preparing", "waiting_for_recovery",
                                                       "restoring", "passed", "failed", "interrupted"})
        interrupted_test = ("commissioning" in data and (not valid_test or prior_test["active"]
                            or prior_test["status"] in {"preparing", "waiting_for_recovery", "restoring"}))
        if valid_test and (interrupted_test or prior_test["status"] in {"passed", "failed", "interrupted"}):
            # Only historical timestamps are restored. Saved phase deadlines,
            # timing values and activity never establish a new control lease.
            self._commissioning["started_at"] = _diagnostic_timestamp(prior_test.get("started_at"))
        if interrupted_test:
            # A process restart is not permission to resume a maintenance On.
            self._manual_recovery_required = True
            self._commissioning.update(status="interrupted",
                reason="Restart interrupted relay commissioning; manual recovery required",
                finished_at=self._wall_clock())
        elif isinstance(prior_test, dict) and prior_test.get("status") in {"passed", "failed", "interrupted"}:
            self._commissioning.update(status=prior_test["status"],
                reason=str(prior_test.get("reason") or "Previous relay commissioning result")[:240],
                finished_at=_diagnostic_timestamp(prior_test.get("finished_at")))
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
        if interrupted_test:
            self._save()
        self._publish()

    def async_start(self):
        """Register inexpensive cached-state checks on HA's loop."""
        if self._active or self._closed:
            return
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

        @callback
        def changed(_event=None):
            if _event and self._supervised_restore is not None:
                self._supervised_restore_check(
                    entity=_event.data.get("entity_id"), state=_event.data.get("new_state"))
            if _event and _event.data.get("entity_id") == self.bindings.heater_switch:
                state = _event.data.get("new_state")
                value, attrs, _ = _state_parts(state)
                old_value, _, _ = _state_parts(_event.data.get("old_state"))
                if (self.commissioning_active and not self._cutting and not self._restoring
                        and (attrs.get("restored") or attrs.get("assumed_state")
                             or (value != "on" if self._commissioning_preparing else value != "off"))):
                    preparing = self._commissioning_preparing
                    self._commissioning_failed("Dedicated relay changed outside the commissioning operation",
                                               status="interrupted", relinquish=value == "on")
                    if not preparing and value == "on":
                        self._start_action(self._cut_switch(force=True))
                if ((self._cut_owned or self._isolation_report is not None or self._restoring)
                        and (value != "off" or attrs.get("restored") or attrs.get("assumed_state"))):
                    self._invalidate_isolation(self._wall_clock())
                elif (value == old_value == "off" and self._isolation_report is not None
                        and not attrs.get("restored") and not attrs.get("assumed_state")):
                    # A force-updated identical Off is new feedback, not an
                    # intervening On. Known bad events are fenced separately.
                    self._isolation_changed = self._switch_changed(state)
                if (self._restoring and not self._cutting
                        and ((old_value == "on" and value == "off")
                             or (value == "on" and not self._restore_on_requested))):
                    # An operator's Off during a positive transaction must not
                    # turn into a fresh automatic-On claim after compensation.
                    self._restore_interrupted = True
                    self._manual_recovery_required = True
                    self._manual_recovery_wall = self._wall_clock()
                    self._cut_owned = False
                    self._save()
                    self._commissioning_failed("Manual relay change interrupted restoration", status="interrupted")
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
        if self.manual_control:
            from .controller.ha import HAActivationDeferred
            raise HAActivationDeferred("Dyson is under manual control; return ownership before automated activation")
        if ((body or {}).get("hvac_mode") == "fan_only"
                and not any(key in (body or {}) for key in ("temperature", "target_temp_low", "target_temp_high"))
                and self._fan_recovery_ready()):
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            jobs = self.pending_external_jobs()
            if current is not None and current in jobs:
                if self._fan_only_task in jobs and self._fan_only_task is not current:
                    from .controller.ha import HAActivationDeferred
                    raise HAActivationDeferred("Previous fan-only service is still running")
                self._fan_only_task = current
            self._fan_only_authorized = True
            return
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
            after_boundary = (stamp > boundary if boundary is not None else
                self._wall_observed_off_wall is not None and stamp >= self._wall_observed_off_wall)
            return (after_boundary
                    and not attrs.get("restored") and not attrs.get("assumed_state")
                    and -5 <= self._wall_clock() - stamp <= TELEMETRY_MAX_AGE_SECONDS)
        except (ValueError, TypeError, OverflowError):
            return False

    def _current_off_report_stamp(self):
        """Fresh passive feedback, never an acknowledgement of a sent command."""
        state, attrs, reported = _state_parts(self._state(self.bindings.heater))
        if (state != "off" or attrs.get("hvac_action") in ("heating", "cooling")
                or attrs.get("restored") or attrs.get("assumed_state")):
            return None
        try:
            stamp = _timestamp(reported)
            return stamp if -5 <= self._wall_clock() - stamp <= TELEMETRY_MAX_AGE_SECONDS else None
        except (ValueError, TypeError, OverflowError):
            return None

    def _switch_observed(self, desired, *, after=None):
        state, _, reported = _state_parts(self._state(self.bindings.heater_switch))
        if state != desired:
            return False
        try:
            return after is None or _timestamp(reported) > after
        except (ValueError, TypeError, OverflowError):
            return False

    def _switch_changed(self, state=None):
        state = self._state(self.bindings.heater_switch) if state is None else state
        value = state.get("last_changed") if isinstance(state, dict) else getattr(state, "last_changed", None)
        try:
            return _timestamp(value)
        except (ValueError, TypeError, OverflowError):
            return None

    def _ac_observed(self, desired, *, after=None):
        entity = getattr(self.bindings, "emergency_ac_switch", None)
        if desired == "on" and after is None:
            # Ordinary availability follows the same corroborating contract as
            # admission. Explicit post-emergency restoration below still needs
            # new evidence after the emergency command boundary.
            ac, _, _ = _corroborated_ac(self._states(),
                replace(self.bindings, ac_output_switch=entity), self._wall_clock(),
                output_valid=self._reading.output_w is not None and self._reading.battery_percent is not None)
            return ac == "on"
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

    def _heater_meter_stopped(self, *, after=None):
        entity, units, low_limit, _ = self._heater_meter()
        boundary = self._off_boundary() if after is None else after
        passive = after is None and boundary is None and self._wall_observed_off_wall is not None
        if passive:
            boundary = self._wall_observed_off_wall
        if entity is None or boundary is None:
            return None
        try:
            observed = self._state(entity)
            value = _measurement({entity: observed}, entity, units, self._wall_clock())
            _, attrs, reported = _state_parts(observed)
            stamp = _timestamp(reported)
            if (attrs.get("restored") or attrs.get("assumed_state")
                    or (stamp < boundary if passive else stamp <= boundary)):
                return None
            return value < low_limit
        except (ValueError, TypeError, OverflowError):
            return None

    def _passive_off_evidence(self):
        """Joint fresh Off/low-current evidence without a fabricated command."""
        stamp = self._current_off_report_stamp()
        if stamp is None:
            return None
        entity, units, low_limit, _ = self._heater_meter()
        if entity is None:
            return stamp
        try:
            observed = self._state(entity)
            value = _measurement({entity: observed}, entity, units, self._wall_clock())
            _, attrs, reported = _state_parts(observed)
            if value >= low_limit or attrs.get("restored") or attrs.get("assumed_state"):
                return None
            return min(stamp, _timestamp(reported))
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
                and self._heater_meter_stopped() is not False):
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

    def _commissioning_failed(self, reason, *, status="failed", relinquish=False):
        """End a maintenance attempt without granting a future power-On claim."""
        if not self.commissioning_active:
            return
        self._commissioning.update(active=False, status=status, reason=str(reason)[:240],
                                   finished_at=self._wall_clock())
        self._latched = self._blocked = self._manual_recovery_required = True
        self._manual_recovery_wall = self._wall_clock()
        self._restore_interrupted = True
        self._safe_since = None
        if relinquish:
            self._cut_owned = False
        self._save()
        self._publish()

    def _supervised_restore_abort(self, reason):
        watch = self._supervised_restore
        if watch is None or watch["failure"] is not None:
            return False
        watch["failure"] = str(reason)[:240]
        self._commissioning_failed(watch["failure"])
        # A service call can be blocked while these event callbacks run. Do
        # not wait for its completion or for native Off before requesting cut.
        self._invalid_positive.update(self._positive_tasks)
        self._schedule_compensating_cut()
        return False

    def _supervised_restore_check(self, *, entity=None, state=None):
        """Consume every known event as well as snapshots; bad evidence sticks."""
        watch = self._supervised_restore
        if watch is None:
            return True
        if watch["failure"] is not None:
            return False
        now, wall = self._clock(), self._wall_clock()
        if (not math.isfinite(now) or not math.isfinite(wall)
                or now < watch["checked_clock"] or wall < watch["checked_wall"]
                or now - watch["checked_clock"] >= COMMISSIONING_REPORT_GAP_SECONDS
                or self._commissioning_expired()):
            return self._supervised_restore_abort("Supervised restoration observation deadline expired")
        watch["checked_clock"], watch["checked_wall"] = now, wall
        if (not self.live or self._closing or self._closed or self._restore_interrupted
                or self._manual_recovery_required or self._identity_mismatch
                or self._persistence_error or not self.commissioning_active
                or not self._commissioning_controller_stopped()
                or watch["identity"] != self._binding_identity()):
            return self._supervised_restore_abort("Supervised restoration was interrupted")
        # Test deadlines before accepting a newly arrived good packet. Late
        # data cannot erase a real gap merely by arriving ahead of a timer job.
        if (watch["last_report"] is None
                and now - watch["requested_clock"] >= COMMISSIONING_METER_ACQUIRE_SECONDS):
            return self._supervised_restore_abort("Fresh low heater watts did not arrive before the startup deadline")
        if (watch["last_report"] is not None
                and (now - watch["seen_clock"] >= COMMISSIONING_REPORT_GAP_SECONDS
                     or wall - watch["last_report"] >= COMMISSIONING_REPORT_GAP_SECONDS)):
            return self._supervised_restore_abort("Fresh low heater watt reports stopped during restoration")
        off_clock, off_task = watch.get("off_clock"), watch.get("off_task")
        if (not watch["off_acknowledged"] and watch.get("off_ack_deadline") is not None
                and time.monotonic() >= watch["off_ack_deadline"]):
            return self._supervised_restore_abort("Fresh native Off was not verified before its acknowledgement deadline")
        if off_clock is not None and now - off_clock >= COMMISSIONING_METER_CONFIRM_SECONDS:
            return self._supervised_restore_abort("Post-command low heater watts were not verified before the deadline")
        if off_task is not None and off_task.done() and (off_task.cancelled() or off_task.exception() is not None):
            return self._supervised_restore_abort("Native Off service failed during supervised restoration")
        states = self._states()
        if entity is not None:
            states[entity] = state
        external = replace(self.bindings, heater_current=None, heater_power=None)
        if evaluate_power(states, external, wall).issue:
            return self._supervised_restore_abort("Authoritative electrical protection interrupted restoration")
        for role in ("battery", "output_power"):
            _, attrs, _ = _state_parts(states.get(getattr(self.bindings, role)))
            if attrs.get("restored") or attrs.get("assumed_state"):
                return self._supervised_restore_abort("Authoritative electrical report is not trusted")
        native, attrs, _ = _state_parts(states.get(self.bindings.heater))
        if attrs.get("hvac_action") in ("heating", "cooling"):
            return self._supervised_restore_abort("Native heating contradicts supervised low-load restoration")
        if watch["off_acknowledged"]:
            _, _, native_report = _state_parts(states.get(self.bindings.heater))
            try:
                stamp = _timestamp(native_report)
                still_off = (native == "off" and not attrs.get("restored")
                             and not attrs.get("assumed_state")
                             and self._off_request_wall < stamp <= wall + 5
                             and wall - stamp <= TELEMETRY_MAX_AGE_SECONDS)
            except (ValueError, TypeError, OverflowError):
                still_off = False
            if not still_off:
                return self._supervised_restore_abort("Verified native Off was lost before restoration completed")
        relay, attrs, _ = _state_parts(states.get(self.bindings.heater_switch))
        if (attrs.get("restored") or attrs.get("assumed_state")
                or relay not in ("off", "on") or (watch["on_wall"] is not None and relay != "on")):
            return self._supervised_restore_abort("Dedicated relay feedback is not trusted during restoration")
        observed = states.get(self.bindings.heater_power)
        _, attrs, reported = _state_parts(observed)
        try:
            stamp = _timestamp(reported)
            # A cached cutoff zero is allowed only as unqualified startup data.
            # New invalid events must not be hidden by a following good event.
            new = stamp > watch["requested_wall"]
            if new or entity == self.bindings.heater_power or watch["last_report"] is not None:
                value = _measurement(states, self.bindings.heater_power,
                                     {"W": 1, "kW": 1000}, wall,
                                     maximum_age=COMMISSIONING_REPORT_GAP_SECONDS)
                if attrs.get("restored") or attrs.get("assumed_state"):
                    raise ValueError("Heater power report is not trusted")
                if value >= HEATER_OFF_MAX_W:
                    raise ValueError("Heater power reached the supervised low-load limit")
                if watch["last_report"] is not None:
                    if stamp < watch["last_report"]:
                        raise ValueError("Heater power reports arrived out of order")
                    if stamp == watch["last_report"] and value != watch["last_value"]:
                        raise ValueError("Heater power changed without a new report timestamp")
                if watch["on_wall"] is not None and stamp > watch["on_wall"]:
                    if watch["last_report"] is None or stamp > watch["last_report"]:
                        watch["last_report"], watch["last_value"] = stamp, value
                        watch["seen_clock"] = now
        except (ValueError, TypeError, OverflowError) as exc:
            if entity == self.bindings.heater_power or watch["last_report"] is not None:
                return self._supervised_restore_abort(f"Supervised heater power is invalid: {exc}")
            # No usable pre-On sample is expected with an isolated meter. A
            # new invalid post-On report, however, is an immediate failure.
            try:
                post_request = _timestamp(reported) > watch["requested_wall"]
            except (ValueError, TypeError, OverflowError):
                post_request = False
            if post_request:
                return self._supervised_restore_abort(f"Supervised heater power is invalid: {exc}")
        if (not watch["native_ready"]
                and now - watch["requested_clock"] >= COMMISSIONING_RECONNECT_SECONDS):
            return self._supervised_restore_abort("Native reconnect and stop verification deadline expired")
        return True

    def _supervised_phase(self, phase, reason):
        if not self.commissioning_active:
            return
        remaining = max(0, COMMISSIONING_MAX_SECONDS - (
            self._clock() - self._commissioning_started_clock))
        self._commissioning.update(phase=phase, reason=reason,
            phase_started_at=self._wall_clock(),
            session_deadline_at=self._wall_clock() + remaining)
        self._publish()

    async def _supervised_reconnect(self):
        """Watt-only, supervised commissioning. Never used by ordinary recovery."""
        watch = self._supervised_restore
        self._supervised_phase("meter_startup", "Waiting for fresh low dedicated watts after relay On")
        while self._supervised_restore_check() and watch["last_report"] is None:
            await asyncio.sleep(.1)
        if watch["failure"] is not None:
            return False
        self._supervised_phase("native_reconnect", "Fresh low watts supervised while native HA control reconnects")
        while self._supervised_restore_check():
            native, attrs, reported = _state_parts(self._state(self.bindings.heater))
            try:
                ready = (native not in (None, "unknown", "unavailable")
                         and not attrs.get("restored") and not attrs.get("assumed_state")
                         and watch["on_wall"] < _timestamp(reported) <= self._wall_clock() + 5
                         and self._wall_clock() - _timestamp(reported) <= TELEMETRY_MAX_AGE_SECONDS)
            except (ValueError, TypeError, OverflowError):
                ready = False
            if ready:
                watch["native_ready"] = True
                break
            await asyncio.sleep(.1)
        if watch["failure"] is not None:
            return False
        self._supervised_phase("native_off", "Requesting native Off before final electrical confirmation")
        # The watt watchdog remains live inside _off_ack, independently of the
        # native state. Native acknowledgement and meter cadence have separate
        # bounds; success of a service call is never an acknowledgement.
        if not await self._off_ack(RESTORE_ACK_SECONDS, require_meter=False):
            return self._supervised_restore_abort("Fresh native Off was not verified after reconnection")
        watch["off_acknowledged"] = True
        self._supervised_phase("meter_confirmation", "Waiting for new low watts after the native Off request")
        while self._supervised_restore_check():
            off_task = watch["off_task"]
            if off_task.done() and (off_task.cancelled() or off_task.exception() is not None):
                return self._supervised_restore_abort("Native Off service failed during supervised restoration")
            if self._heater_meter_stopped() is True and not self._service_tasks:
                # Let already queued HA state events establish contradictions
                # before a fast service result could clear the maintenance gate.
                await asyncio.sleep(0)
                if (self._supervised_restore_check() and self._heater_off(fresh=True)
                        and self._heater_meter_stopped() is True and not self._service_tasks):
                    return True
            if self._clock() - watch["off_clock"] >= COMMISSIONING_METER_CONFIRM_SECONDS:
                break
            await asyncio.sleep(.1)
        return self._supervised_restore_abort("Post-command low heater watts were not verified before the deadline")

    def _commissioning_prerequisites(self, *, preparing=False):
        if (not self.live or self._closed or self._closing or self._restoring
                or self._cutting or self._manual_recovery_required or self._identity_mismatch
                or self._emergency_ac_latched or self._persistence_error
                or self._cut_owned or self._positive_tasks or self._service_tasks
                or self._compensation_pending or self.pending_external_jobs()
                or (self._action_task is not None and not self._action_task.done())):
            raise RuntimeError("Live, idle heater protection without pending recovery or jobs is required")
        if (not preparing and (self.blocked or self._latched or self.commissioning_active
                or (self._save_task is not None and not self._save_task.done()))):
            raise RuntimeError("Wait for the heater power guard to be Ready")
        if preparing and (not self.commissioning_active or not self._commissioning_preparing):
            raise RuntimeError("Relay commissioning was interrupted")
        if not self._commissioning_controller_stopped():
            raise RuntimeError("Controller must be Off with no equipment ownership or pending jobs")
        if self._store is None:
            raise RuntimeError("Persistent heater protection storage is not ready")
        if self.bindings.heater_switch is None or not self._heater_meter_configured:
            raise RuntimeError("A dedicated heater relay and current or power meter are required")
        if self._emergency_enabled:
            raise RuntimeError("Relay commissioning requires shared AC fallback already disabled")
        state, attrs, reported = _state_parts(self._state(self.bindings.heater_switch))
        try:
            relay_stamp = _timestamp(reported)
            relay_time_valid = math.isfinite(relay_stamp) and relay_stamp <= self._wall_clock() + 5
        except (ValueError, TypeError, OverflowError):
            relay_time_valid = False
        if (state != "on" or attrs.get("restored") or attrs.get("assumed_state")
                or not relay_time_valid or self._switch_changed() is None):
            raise RuntimeError("The dedicated heater relay must report trusted On")
        reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
        if (not self._commissioning_external_trusted() or not reading.safe_to_rearm
                or self._passive_off_evidence() is None):
            raise RuntimeError("Fresh native Off, low heater load, safe electricals and battery above 40% are required")
        return self._switch_changed()

    def _commissioning_controller_stopped(self):
        try:
            return self.commissioning_controller_off() is True
        except Exception:
            return False

    def _recovery_controller_stopped(self):
        try:
            return self.recovery_controller_off() is True
        except Exception:
            return False

    def _commissioning_external_trusted(self):
        for role in ("battery", "output_power"):
            _, attrs, _ = _state_parts(self._state(getattr(self.bindings, role)))
            if attrs.get("restored") or attrs.get("assumed_state"):
                return False
        return True

    def _commissioning_expired(self):
        if not self.commissioning_active or self._commissioning_started_clock is None:
            return False
        elapsed = self._clock() - self._commissioning_started_clock
        return elapsed < 0 or elapsed >= COMMISSIONING_MAX_SECONDS

    async def _persist_commissioning(self):
        self._save()
        task = self._save_task
        if task is None:
            return False
        done, _ = await asyncio.wait((task,), timeout=SERVICE_TIMEOUT_SECONDS)
        if not done or task.cancelled() or task.exception() is not None:
            # The actual write remains tracked. A late completion must save the
            # subsequently queued interrupted state too, never license a retry.
            self._persistence_error = "Cannot persist relay commissioning before its deadline"
            return False
        return self._persistence_error is None

    async def async_commission_relay(self, *, confirm=False):
        """Explicit supervised maintenance; returns after cut, not after cooldown."""
        if type(confirm) is not bool or not confirm:
            raise RuntimeError("Explicit confirm: true is required for a physical relay test")
        if self._commissioning_task is not None and not self._commissioning_task.done():
            raise RuntimeError("Relay commissioning is already running")
        changed = self._commissioning_prerequisites()
        # Reserve before the first await, including before scheduling a child:
        # neither controller enable nor caller cancellation may slip between
        # validation and establishment of the maintenance lease.
        self._commissioning = {"active": True, "status": "preparing",
            "reason": "Supervised dedicated relay recovery test; no heat requested",
            "started_at": self._wall_clock(), "finished_at": None,
            "restore_window_seconds": RESTORE_ACK_SECONDS,
            "maximum_seconds": COMMISSIONING_MAX_SECONDS}
        if self._heater_meter()[3] == "power":
            self._commissioning.update(phase="preparing", staged_watts=True,
                meter_acquire_seconds=COMMISSIONING_METER_ACQUIRE_SECONDS,
                meter_report_gap_seconds=COMMISSIONING_REPORT_GAP_SECONDS,
                native_reconnect_seconds=COMMISSIONING_RECONNECT_SECONDS,
                post_off_meter_seconds=COMMISSIONING_METER_CONFIRM_SECONDS)
        self._commissioning_started_clock = self._clock()
        self._commissioning_caller_cancelled = False
        self._commissioning_preparing = True
        self._trip("Supervised dedicated heater relay commissioning")
        self._publish()
        try:
            self._commissioning_task = self.hass.async_create_task(self._commission_relay(changed), eager_start=False)
        except Exception:
            self._commissioning_failed("Cannot start relay commissioning; manual recovery required")
            raise
        # A cancelled HTTP/service caller does not abandon an actual Off job.
        self._commissioning_task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None)
        try:
            return await asyncio.shield(self._commissioning_task)
        except asyncio.CancelledError:
            self._commissioning_caller_cancelled = True
            self._commissioning_failed("Commissioning request cancelled; manual recovery required", status="interrupted")
            raise

    async def _commission_relay(self, changed):
        try:
            if not self.commissioning_active:
                raise RuntimeError("Relay commissioning was cancelled before dispatch")
            if not await self._persist_commissioning():
                raise RuntimeError("Cannot persist commissioning intent; no relay command issued")
            self._commissioning_prerequisites(preparing=True)
            if self._switch_changed() != changed:
                raise RuntimeError("Dedicated relay changed during commissioning preparation")
            # This is a deliberate maintenance cut, not a fabricated overload
            # or a failed native Off. Use the real ownership and isolation path.
            if not await self._cut_switch() or not self._observe_isolation(self._wall_clock()):
                raise RuntimeError("Dedicated relay cutoff is unconfirmed; verify it manually")
            if not self.commissioning_active:
                raise RuntimeError("Relay commissioning was interrupted")
            self._commissioning_preparing = False
            self._commissioning.update(status="waiting_for_recovery",
                reason="Dedicated relay isolated; normal safe-load interval and cooldown apply")
            if not await self._persist_commissioning():
                raise RuntimeError("Cannot persist commissioning cutoff; manual recovery required")
            if not self.commissioning_active:
                raise RuntimeError("Relay commissioning was interrupted")
            self._publish()
            return True
        except BaseException as exc:
            self._commissioning_failed("Relay commissioning did not complete; manual recovery required")
            if self._commissioning_caller_cancelled and isinstance(exc, RuntimeError):
                # There is no longer a service caller to receive this expected
                # interrupted result. Keep cleanup tracked without an orphaned
                # shield-future exception in HA's event loop.
                return False
            raise
        finally:
            self._commissioning_preparing = False
            self._schedule_check()

    async def async_acknowledge_emergency(self):
        """Acknowledge manual recovery; never energize an outlet or shared AC."""
        if (self._recovery_task is not None and not self._recovery_task.done()
                and getattr(self, "_recovery_operation", None) == "restore"):
            raise RuntimeError("A restoration approval is already in progress")
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_operation = "acknowledge"
            self._recovery_task = self.hass.async_create_task(self._acknowledge_emergency())
        return await asyncio.shield(self._recovery_task)

    @property
    def _retained_cut_recovery_pending(self):
        return (self._retained_cut_claim_after is not None and self._cut_owned
                and not self._identity_mismatch and not self._manual_recovery_required
                and not self._emergency_ac_latched and self._switch_observed("on"))

    def _manual_recovery_evidence(self):
        """Validate current recovery inputs, including after a storage await."""
        retained = self._retained_cut_recovery_pending
        if not (self._emergency_ac_latched or self._manual_recovery_required or retained):
            raise RuntimeError("No emergency, wiring or retained cutoff recovery is pending")
        if ((not self.live and not retained) or self._closed or self._positive_tasks
                or self._service_tasks or self._compensation_pending
                or self.pending_external_jobs()
                or (self._save_task is not None and not self._save_task.done())
                or (self._action_task is not None and not self._action_task.done())
                or (self._commissioning_task is not None and not self._commissioning_task.done())):
            raise RuntimeError("Wait for all actual heater and AC service jobs to finish")
        if self._emergency_ac_latched and not self._ac_observed("on", after=self._emergency_ac_request_wall):
            raise RuntimeError("Manually restore and verify AC output before acknowledging recovery")
        state, attrs, reported = _state_parts(self._state(self.bindings.heater))
        barrier = self._manual_recovery_wall or self._emergency_ac_request_wall or 0
        if retained or (self._shutdown_fault and self._cut_owned):
            if (self._closing or self._restoring or self._cutting or self._restore_intent
                    or self.commissioning_active or self._commissioning_preparing
                    or self._emergency_service_tasks
                    or (self._compensating_task is not None and not self._compensating_task.done())
                    or not self._recovery_controller_stopped()):
                raise RuntimeError("Stop auxiliary work and wait for all recovery jobs to finish")
            if self._store is None:
                raise RuntimeError("Durable storage is required to acknowledge a retained heater cutoff")
            if (self._saved_binding_identity != self._binding_identity()
                    or not self._heater_meter_configured):
                raise RuntimeError("Matching heater wiring and a dedicated meter are required")
            barrier = max(barrier, self._retained_cut_claim_after or 0)
            relay, relay_stamp = _device_feedback(self._state(self.bindings.heater_switch), self._wall_clock(), after=barrier)
            if self.bindings.heater_switch is not None and relay != "on":
                raise RuntimeError("Source-confirmed dedicated relay On feedback after recovery is required")
            # A subsequent manual relay cycle invalidates pre-restoration Off
            # and low-meter evidence, even if both samples remain recent.
            changed = self._switch_changed() if self.bindings.heater_switch is not None else barrier
            if changed is None or (relay_stamp is not None and changed > relay_stamp):
                raise RuntimeError("Fresh dedicated relay feedback is required") from None
            barrier = max(barrier, changed)
            if not self._commissioning_external_trusted():
                raise RuntimeError("Trusted electrical reports are required after restart")
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
        if self._heater_meter_configured and self._heater_meter_stopped(after=barrier) is not True:
            raise RuntimeError(f"Fresh low heater {self._heater_meter()[3]} after restoration is required")
        return barrier, reading

    def _manual_recovery_generation(self):
        return (self._manual_recovery_wall, self._emergency_ac_request_wall,
                self._emergency_ac_attempts, self._emergency_ac_latched,
                self._manual_recovery_required, self._identity_mismatch,
                self._latched, self._trip_wall, self._cut_owned,
                self._off_request_wall, self._shutdown_contradicted,
                self._retained_cut_claim_after, self._shutdown_fault, tuple(sorted(self._binding_identity().items())))

    async def _refresh_recovery_feedback(self):
        if self.async_read_recovery_feedback is None or self.bindings.heater_switch is None:
            return
        entity = self.bindings.heater_switch
        self._recovery_feedback.pop(entity, None)
        before = self.hass.states.get(entity)
        if before is None:
            return
        changed = before.get("last_changed") if isinstance(before, dict) else before.last_changed
        started = self._wall_clock()
        try:
            value = await self.async_read_recovery_feedback(entity)
        except Exception:
            return
        state = self.hass.states.get(entity)
        if value not in ("on", "off") or state is None:
            return
        current_changed = state.get("last_changed") if isinstance(state, dict) else state.last_changed
        if (entity != self.bindings.heater_switch or changed != current_changed
                or _state_parts(state)[0] != value):
            return
        # Date evidence conservatively at read dispatch, never at a delayed
        # callback. A relay transition during the read invalidates the reply.
        self._recovery_feedback[entity] = {"state": value,
            "reported_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "last_changed": changed}

    async def async_approve_restore(self, *, confirm=False):
        """Approve one bounded dedicated-relay recovery; never shared AC On."""
        if confirm is not True:
            raise RuntimeError("Explicit confirmation is required before restoring heater power")
        if self._recovery_task is not None and not self._recovery_task.done():
            raise RuntimeError("A heater recovery operation is already in progress")
        self._recovery_operation = "restore"
        self._recovery_task = self.hass.async_create_task(self._approve_restore())
        return await asyncio.shield(self._recovery_task)

    async def _approve_restore(self):
        if (not self.live or self._closed or self._closing or self.commissioning_active
                or self._emergency_ac_latched or self._identity_mismatch or not self._cut_owned
                or self._store is None or self._saved_binding_identity != self._binding_identity()
                or self._restoring or self._cutting or self._restore_intent
                or (self._save_task is not None and not self._save_task.done())
                or not self._heater_meter_configured or not self._recovery_controller_stopped()
                or self._action_task is not None and not self._action_task.done()
                or self._service_tasks or self._positive_tasks or self.pending_external_jobs()
                or self._compensation_pending or self._persistence_error):
            raise RuntimeError("Dedicated recovery requires matching ownership, storage and no pending auxiliary jobs")
        if self._tripped_at is None or self._clock() - self._tripped_at < COOLDOWN_SECONDS:
            raise RuntimeError("Wait for the five-minute cutoff cooldown before approving restoration")
        await self._refresh_recovery_feedback()
        def ready():
            barrier = max(self._retained_cut_claim_after or 0, self._cut_request_wall or 0)
            value, stamp = _device_feedback(self._state(self.bindings.heater_switch), self._wall_clock(), after=barrier)
            return (value == "off" and self._external_power().safe_to_rearm
                    and self._heater_meter_stopped(after=barrier) is not False
                    and self._recovery_controller_stopped() and not self.pending_external_jobs()
                    and not self._service_tasks and not self._positive_tasks
                    and not self._closing and not self._closed and not self._restoring
                    and not self._cutting and not self._restore_intent
                    and not self._persistence_error and not self._compensation_pending
                    and (self._action_task is None or self._action_task.done())
                    and (self._save_task is None or self._save_task.done()))
        if not ready():
            raise RuntimeError("Fresh physical relay Off and verified supply are required before restoring power")
        generation = self._manual_recovery_generation()
        # Save the retained obligation; approval itself is never a persisted
        # permission to turn On following a restart.
        try:
            async with self._store_lock:
                await self._store.async_save(self._saved_state())
        except Exception as exc:
            self._persistence_error = "Cannot persist heater restoration approval"
            self._publish()
            raise RuntimeError(self._persistence_error) from exc
        if generation != self._manual_recovery_generation() or not ready():
            raise RuntimeError("Recovery changed during approval; verify it again")
        self._restore_approved_until = self._clock() + 120
        self._manual_recovery_required = False
        self._safe_since = None
        self._schedule_check()
        self._publish()
        return True

    async def _acknowledge_emergency(self):
        await self._refresh_recovery_feedback()
        barrier, reading = self._manual_recovery_evidence()
        generation = self._manual_recovery_generation()
        # Publish/retain inhibition until the prospective cleared state is
        # durable. A cancelled button caller cannot cancel this actual save.
        prospective = {**self._saved_state(), "emergency_ac_latched": False,
                       "manual_recovery_required": False, "switch_cut_owned": False, "shutdown_fault": False,
                       "legacy_cooldown": self._legacy_cooldown or self._shutdown_fault,
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
        self._legacy_cooldown |= self._shutdown_fault
        self._shutdown_fault = False
        self._retained_cut_claim_after = None
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
        self._update_alerts()
        self._schedule_check()
        self._publish()
        return True

    async def async_request_heater_off(self):
        """Apply configured stop stages to an explicitly requested heater stop."""
        if not self.live or self._closed:
            raise RuntimeError("Heater power guard is not available for live shutdown")
        if self._emergency_ac_latched:
            return self.shutdown_confirmed
        self._start_action(self._shed())
        # A caller's timeout/cancellation is not cancellation of an actual stop.
        await asyncio.shield(self._action_task)
        return self.shutdown_confirmed

    def _start_action(self, coroutine):
        if self._closed or not self.live or (self._action_task is not None and not self._action_task.done()):
            coroutine.close()
            return
        # HA can eagerly enter a coroutine before async_create_task returns.
        # Publish the actual task before it can dispatch a service whose state
        # callbacks re-enter _observe and otherwise launch a duplicate stop.
        self._action_task = self.hass.async_create_task(coroutine, eager_start=False)

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
        if self._restore_approved_until is not None and self._clock() >= self._restore_approved_until:
            self._restore_approved_until = None
            self._manual_recovery_required = True
            self._save()
        now = self._wall_clock()
        if self.commissioning_active and self._commissioning_expired():
            self._commissioning_failed("Supervised relay test exceeded its time limit", status="interrupted")
        if (self.commissioning_active and self._last_observation_wall is not None
                and (now < self._last_observation_wall
                     or now - self._last_observation_wall > TELEMETRY_MAX_AGE_SECONDS)):
            self._commissioning_failed("Observation continuity was lost during relay commissioning", status="interrupted")
        if self.commissioning_active and not self._commissioning_controller_stopped():
            self._commissioning_failed("Comfort controller is no longer stopped", status="interrupted")
        self._supervised_restore_check()
        if (self.commissioning_active and not self._commissioning_preparing
                and not self._restoring and not self._cutting):
            value, attrs, _ = _state_parts(self._state(self.bindings.heater_switch))
            if value != "off" or attrs.get("restored") or attrs.get("assumed_state"):
                self._commissioning_failed("Dedicated relay isolation was interrupted",
                                           status="interrupted", relinquish=value == "on")
                if value == "on":
                    self._start_action(self._cut_switch(force=True))
        if (self._cut_owned and self._last_observation_wall is not None
                and (now < self._last_observation_wall
                     or now - self._last_observation_wall > TELEMETRY_MAX_AGE_SECONDS)):
            self._invalidate_isolation(now)
        self._last_observation_wall = now
        self._observe_isolation(now)
        states = self._states()
        reading = evaluate_power(states, self.bindings, now)
        if self.commissioning_active and (self._external_power().issue
                                         or not self._commissioning_external_trusted()):
            self._commissioning_failed("Authoritative electrical protection interrupted commissioning")
        self._reading = reading
        self._observe_aggregate_baseline()
        if self._persistence_error and self._clock() >= self._save_retry_at:
            self._save_retry_at = self._clock() + 60
            self._save()
        self._observe_aggregate_stop()
        if self.commissioning_active and self._commissioning_preparing:
            # The explicit maintenance task owns this initial negative action.
            # Healthy-state callbacks must not start a competing native stop.
            self._blocked = True
            self._reason = "Preparing supervised dedicated relay commissioning"
            self._publish()
            return
        if (self._wall_observed_off_wall is not None and not self.shutdown_confirmed
                and self._shutdown_confirmation in {"state_only", "aggregate_corroborated"}):
            self._shutdown_confirmation = "unconfirmed"
        if self._heater_off(fresh=True) and self._heater_meter_stopped() is False:
            self._shutdown_contradicted = True
            self._off_confirmed = False
            self._shutdown_confirmation = "unconfirmed"
            if self.live and not self._emergency_ac_latched and not self._closing:
                self._trip(f"Heater {self._heater_meter()[3]} contradicts the requested Off state")
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
            # Compensation itself keeps power_cut_confirmed false while its
            # job is pending. Requeue only for actual missing relay-Off proof,
            # or our own Off events can recursively request another cut.
            if (self.live and self._cut_owned and not self._switch_cut_observed()
                    and not self._manual_policy_control):
                self._schedule_compensating_cut()
            self._publish()
            return
        if (self._cut_owned and not self._restoring and not self._cutting
                and self._switch_observed("on") and not self._positive_tasks
                and not self._compensation_pending):
            self._manual_power_seen = True
        if (self._cut_owned and not self._restoring and not self._cutting
                and self._switch_observed("off") and self._manual_power_seen):
            # A manual relay cycle does not acknowledge a failed shutdown.
            # Retain the obligation until explicit, durable recovery.
            self._manual_recovery_required = True
            self._save()
        r = self._reading
        isolated = None if self._restoring else self._isolated_recovery_reading()
        if isolated is not None:
            # Diagnostics retain the real missing meter value. Only verified
            # isolation supplies the alternative pre-restoration safety path.
            r = isolated
        if self._manual_recovery_required:
            self._blocked = True
            if (self._shutdown_fault and self.live and not self.shutdown_confirmed
                    and not self._restoring and not self._cutting):
                self._start_action(self._shed())
            self._reason = ("Heater wiring/ownership changed; explicit manual recovery acknowledgement required"
                            if self._identity_mismatch else
                            "Heater power recovery requires explicit operator acknowledgement")
            self._publish()
            return
        if self._manual_policy_control:
            # Manual takeover suppresses policy-driven shutdown, not known
            # faults, uncertain service cleanup, or persisted ownership.
            # Keeping these claims intact is essential when control returns.
            self._blocked = True
            self._reason = "Dyson is under persistent manual control; automated reserve cutoff is suspended"
            self._safe_since = None
            self._restore_approved_until = None
            self._publish()
            return
        if r.issue:
            if (self._fan_only_authorized
                    and _state_parts(self._state(self.bindings.heater))[0] == "fan_only"):
                # This fan was deliberately authorized after a proved stop.
                # Loss of reserve/supply needs a new native Off, not an
                # escalation claiming the earlier stop failed.
                self._off_confirmed = False
            self._fan_only_authorized = False
            if self._restore_approved_until is not None:
                self._restore_approved_until = None
                self._manual_recovery_required = True
                self._save()
            self._blocked = True
            self._reason = r.issue
            self._safe_since = None
            # A delayed sensor during startup inhibits a new call immediately,
            # but does not manufacture a five-minute trip if the heater is Off.
            if not r.telemetry_missing or not self._heater_off() or self._latched:
                observed_off = None
                if (self._off_boundary() is None
                        and self._wall_observed_off_wall is None
                        and not self._shutdown_contradicted and not self._service_tasks
                        and not self._positive_tasks and not self.pending_external_jobs()
                        and not self._compensation_pending
                        and self._heater_meter_stopped() is not False):
                    observed_off = self._passive_off_evidence()
                self._trip(r.issue)
                if observed_off is not None:
                    # A charger-driven event can begin while an unowned heater
                    # is already Off. Preserve that trusted current feedback
                    # separately; do not dispatch a no-op stop and mistake its
                    # lack of a new report for a reason to cut shared power.
                    self._wall_observed_off_wall = observed_off
                    self._off_confirmed = True
                    self._shutdown_confirmation = "state_only"
                if self.live and not self._restoring:
                    if self._off_confirmed and (not self._heater_off() or self._heater_meter_stopped() is False):
                        self._off_confirmed = False
                        self._start_action(self._followup_shed())
                    elif not self._off_confirmed and not self.power_cut_confirmed:
                        self._start_action(self._shed())
        elif self._latched:
            self._blocked = True
            if (self._fan_only_authorized and self._fan_recovery_ready()
                    and _state_parts(self._state(self.bindings.heater))[0] == "fan_only"):
                # The original stop was proved before circulation was admitted.
                # Low dedicated load must remain current; heat is still blocked
                # until the ordinary stable-supply interval completes.
                self._safe_since = self._safe_since if self._safe_since is not None else self._clock()
                self._reason = "Fan-only circulation; auxiliary heat awaits stable supply"
                if (self.live and self._clock() - self._safe_since >= SAFE_LOAD_SECONDS
                        and not self.pending_external_jobs()):
                    self._clear()
                self._publish()
                return
            if (self._fan_only_authorized and not self._fan_recovery_ready()
                    and not self._heater_off()):
                # Withdraw circulation if its current evidence stops
                # qualifying. A newly requested Off keeps the normal deadline.
                self._fan_only_authorized = False
                self._off_confirmed = False
            if self._retained_cut_claim_after is not None and self._restore_approved_until is None:
                self._reason = "Retained heater cutoff requires explicit recovery acknowledgement"
                self._publish()
                return
            self._reason = ("Heater relay isolated; waiting for stable safe load and recovery cooldown"
                            if isolated is not None else
                            "Heater held off until battery >40% and verified supply is stable for 60 seconds")
            if not self._restoring and not self._off_confirmed and not self.power_cut_confirmed:
                self._start_action(self._shed())
            if (self._off_confirmed and not self._restoring
                    and (not self._heater_off() or self._heater_meter_stopped() is False)):
                self._off_confirmed = False
                self._start_action(self._followup_shed())
            if (r.safe_to_rearm and (not self._cut_owned or isolated is not None)
                    and (self._wall_observed_off_wall is None or self.shutdown_confirmed)
                    and (isolated is not None or self._heater_meter()[3] != "power" or self.power_cut_confirmed
                         or self._heater_meter_stopped() is True)):
                # Watts must prove low load after the relevant Off boundary,
                # not just be a recent pre-command number. Physical cut proof
                # can qualify an owned relay before its guarded restoration.
                self._safe_since = self._safe_since if self._safe_since is not None else self._clock()
                ready = (self._clock() - self._safe_since >= SAFE_LOAD_SECONDS
                         and (not (self._shutdown_fault or self.commissioning_active or self._legacy_cooldown) or self._clock() - self._tripped_at >= COOLDOWN_SECONDS))
                if (ready and self.live and not self._restoring and not self._positive_tasks
                        and not self._compensation_pending and not self._persistence_error):
                    switch = _state_parts(self._state(self.bindings.heater_switch))[0]
                    if self._cut_owned and switch == "off" and (self.commissioning_active or self._restore_approved_until is not None):
                        if not self._heater_meter_configured:
                            self._reason = "Dedicated outlet recovery requires manual verification without a heater current or power meter"
                        elif self._restore_attempts < MAX_RESTORE_ATTEMPTS:
                            self._start_action(self._restore())
                        else:
                            self._reason = "Heater power restoration failed three times; check the dedicated plug and Dyson"
                    elif (self._heater_off(fresh=True)
                          and (self._wall_observed_off_wall is None or self.shutdown_confirmed)
                          and (self._heater_meter()[3] != "power"
                               or self._heater_meter_stopped() is True)):
                        # Passive Off adoption has no command acknowledgement.
                        # Its configured meter must remain trustworthy through
                        # cooldown, not merely retain a numeric low value.
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
        if self.commissioning_active:
            self._commissioning.update(active=False, status="passed",
                reason="Dedicated relay restored with fresh native Off and low heater load",
                finished_at=self._wall_clock())
        self._latched = self._blocked = self._cut_owned = False
        self._fan_only_authorized = False
        self._fan_only_task = None
        self._shutdown_fault = False
        self._legacy_cooldown = False
        self._restore_approved_until = None
        self._retained_cut_claim_after = None
        self._restore_intent = False
        self._reason = None
        self._tripped_at = self._trip_wall = self._safe_since = None
        self._restore_attempts = 0
        self._off_request_wall = None
        self._manual_off_wall = None
        self._wall_observed_off_wall = None
        self._shutdown_contradicted = False
        self._invalidate_isolation(self._wall_clock())
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
        return {"supply_policy_version": 1, "latched": self._latched,
                "legacy_cooldown": self._legacy_cooldown,
                "switch_cut_owned": self._cut_owned and not self._restore_intent,
                "restoring": self._restore_intent, "trip_wall": self._trip_wall,
                "restore_attempts": self._restore_attempts,
                "binding_identity": self._saved_binding_identity,
                "manual_recovery_required": self._manual_recovery_required,
                "shutdown_fault": self._shutdown_fault,
                "emergency_ac_latched": self._emergency_ac_latched,
                "emergency_ac_reason": self._emergency_ac_reason,
                "emergency_ac_request_wall": self._emergency_ac_request_wall,
                "emergency_ac_attempts": self._emergency_ac_attempts,
                "commissioning": dict(self._commissioning)}

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

    def _service(self, domain, service, entity, *, _before_dispatch=None, **data):
        if not self.live:
            raise RuntimeError("Shadow power guard forbids service calls")
        shared = entity == getattr(self.bindings, "emergency_ac_switch", None)
        if shared and (domain != "switch" or service != "turn_off"):
            raise RuntimeError("The emergency guard never turns shared AC On")
        async def dispatch():
            if _before_dispatch is not None:
                _before_dispatch()
            return await self.hass.services.async_call(
                domain, service, {"entity_id": entity, **data}, blocking=True)

        call = (dispatch() if _before_dispatch is not None else
                self.hass.services.async_call(domain, service, {"entity_id": entity, **data}, blocking=True))
        task = self.hass.async_create_task(call, eager_start=False)
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

    async def _bounded_service(self, domain, service, entity, *, timeout=None, **data):
        task = self._service(domain, service, entity, **data)
        try:
            done, _ = await asyncio.wait((task,), timeout=(SERVICE_TIMEOUT_SECONDS
                if timeout is None else min(SERVICE_TIMEOUT_SECONDS, max(0, timeout))))
        except asyncio.CancelledError:
            if task in self._positive_tasks:
                self._invalid_positive.add(task)
                self._compensation_pending = True
            elif task not in self._emergency_service_tasks:
                task.cancel()
            raise
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

    async def _off_ack(self, seconds, *, require_meter=False):
        """Bound native Off; restoration can also require post-request watts."""
        contradictory = False
        self._shutdown_contradicted = False
        self._manual_off_wall = None
        self._wall_observed_off_wall = None
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
        watch = self._supervised_restore
        def dispatched():
            # A sample arriving while the service task was only queued cannot
            # confirm an Off command which has not actually been dispatched.
            self._off_request_wall = self._wall_clock()
            watch["off_clock"] = self._clock()
        task = self._service("climate", "set_hvac_mode", self.bindings.heater,
            hvac_mode="off", _before_dispatch=dispatched if watch is not None else None)
        if watch is not None:
            watch["off_task"] = task
        deadline = time.monotonic() + seconds
        if watch is not None:
            watch["off_ack_deadline"] = deadline
        while not self._closed and time.monotonic() < deadline:
            if not self._supervised_restore_check():
                task.cancel()
                return False
            if self._restoring and (self._restore_interrupted or self._closing
                    or self._manual_recovery_required or not self._switch_observed("on")
                    or self._external_power().issue):
                task.cancel()
                return False
            if self._heater_off(fresh=True) and (watch is None or watch.get("off_clock") is not None):
                meter_stop = self._heater_meter_stopped()
                if meter_stop is not None:
                    contradictory = not meter_stop
                    self._shutdown_contradicted = contradictory
                    if require_meter and contradictory:
                        return False
                self._observe_aggregate_stop()
                if not contradictory and (not require_meter or meter_stop is True):
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
        if (self._supervised_restore_check() and self._heater_off(fresh=True) and not contradictory
                and (watch is None or watch.get("off_clock") is not None)
                and (not require_meter or self._heater_meter_stopped() is True)):
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
        if not self.commissioning_active:
            self._restore_approved_until = None
            self._shutdown_fault = self._manual_recovery_required = True
            self._manual_recovery_wall = self._wall_clock()
            self._save()
        self._shutdown_stage = "dedicated_off"
        self._cutting = True
        switch = _state_parts(self._state(self.bindings.heater_switch))[0]
        if switch == "on":
            self._retained_cut_claim_after = None
            self._cut_owned = True
            self._save()
        if switch != "off" or force:
            self._cut_request_wall = self._wall_clock()
            self._invalidate_isolation(self._cut_request_wall)
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
        self._observe_isolation(self._wall_clock())
        return self.power_cut_confirmed

    async def _cut_emergency_ac(self):
        """One disruptive request per emergency; no AC restore or retry loop."""
        if not self.live or not self._emergency_enabled:
            self._shutdown_confirmation = "unconfirmed"
            self._reason = "Heater shutdown unconfirmed; shared AC fallback is not enabled"
            return False
        if self._emergency_ac_latched:
            return self._emergency_cut_observed() and not self._emergency_service_tasks
        self._shutdown_fault = True
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

    def _restore_authorized(self):
        return not self.manual_control and (self.commissioning_active or (
            self._restore_approved_until is not None and self._clock() < self._restore_approved_until))

    async def _restore(self):
        if not self._restore_authorized():
            return
        if self._commissioning_expired():
            self._commissioning_failed("Supervised relay test exceeded its time limit", status="interrupted")
        if (self.bindings.heater_switch is None or self._emergency_ac_latched
                or self._manual_recovery_required or self._identity_mismatch
                or not self._cut_owned or self._manual_power_seen
                or self._closing or self._closed or self._persistence_error
                or self._positive_tasks or self._service_tasks or self._compensation_pending
                or self.pending_external_jobs() or not self._switch_observed("off")
                or not self._heater_meter_configured
                or self._restore_attempts >= MAX_RESTORE_ATTEMPTS):
            return
        isolated = self._isolated_recovery_reading()
        # Fresh low meter data must never override a rejected relay identity,
        # restored/assumed Off state or missing post-start isolation proof.
        if isolated is None or not isolated.safe_to_rearm:
            return
        prior_meter_missing = bool(evaluate_power(
            self._states(), self.bindings, self._wall_clock()).meter_issue)
        generation = (self._isolation_revision, self._binding_identity(), self._cut_owned)
        commissioning = self.commissioning_active
        staged = commissioning and self._heater_meter()[3] == "power"
        if commissioning:
            self._commissioning.update(status="restoring",
                reason="One bounded restoration attempt; fresh Off and low load required")
        self._restoring = True
        self._restore_interrupted = False
        self._restore_on_requested = False
        self._restore_attempts += 1
        self._publish()
        success = False
        on_attempted = False
        try:
            if not await self._persist_restore_intent() or self._closing:
                return
            # Storage can yield to an operator, state event or electrical
            # change. A durable intent is not permission to use old evidence.
            current = self._isolated_recovery_reading()
            if self._commissioning_expired():
                self._commissioning_failed("Supervised relay test exceeded its time limit", status="interrupted")
            if commissioning and not self._commissioning_external_trusted():
                self._commissioning_failed("Authoritative electrical reports are not trusted")
            if commissioning and not self._commissioning_controller_stopped():
                self._commissioning_failed("Comfort controller is no longer stopped", status="interrupted")
            if (not self._restore_authorized() or current is None or not current.safe_to_rearm or self._closed or self._closing
                    or self._restore_interrupted or self._manual_recovery_required
                    or self._identity_mismatch or self._manual_power_seen
                    or self._service_tasks or self._positive_tasks or self._compensation_pending
                    or self.pending_external_jobs()
                    or generation != (self._isolation_revision, self._binding_identity(), self._cut_owned)
                    or not self._switch_observed("off")):
                return
            prior_meter_missing |= bool(evaluate_power(
                self._states(), self.bindings, self._wall_clock()).meter_issue)
            requested = self._wall_clock()
            # Boot may resume the heater's retained setting. There is one
            # bounded window for On feedback AND native Off/low-meter proof,
            # and comfort commands remain blocked throughout it.
            deadline = time.monotonic() + RESTORE_ACK_SECONDS
            if commissioning:
                remaining = COMMISSIONING_MAX_SECONDS - (self._clock() - self._commissioning_started_clock)
                deadline = min(deadline, time.monotonic() + max(0, remaining))
            on_attempted = True
            self._restore_on_requested = True
            if staged:
                self._supervised_restore = {
                    "requested_wall": requested, "requested_clock": self._clock(),
                    "checked_clock": self._clock(), "checked_wall": requested,
                    "on_wall": None, "last_report": None, "last_value": None,
                    "seen_clock": None, "failure": None, "identity": self._binding_identity(),
                    "native_ready": False, "off_acknowledged": False}
                self._supervised_phase("relay_on", "Requesting dedicated relay On under watt supervision")

            def before_restore_dispatch():
                # The service task is queued after the persistence recheck.
                # Revalidate at its actual dispatch, including time-only expiry
                # of supply evidence and explicit restoration approval. Its own tracked job means
                # _isolated_recovery_reading cannot be used at this boundary.
                if (not self._restore_authorized() or not self.live or self._closing or self._closed
                        or self._restore_interrupted or self._manual_recovery_required
                        or self._identity_mismatch or self._manual_power_seen
                        or self._persistence_error or self.pending_external_jobs()
                        or self._compensation_pending or not self._cut_owned
                        or not self._observe_isolation(self._wall_clock())
                        or generation != (self._isolation_revision, self._binding_identity(), self._cut_owned)
                        or not self._switch_observed("off")
                        or self._heater_meter_stopped(after=self._isolation_report) is False
                        or not self._external_power().safe_to_rearm
                        or (commissioning and (self._commissioning_expired()
                            or not self._commissioning_external_trusted()
                            or not self._commissioning_controller_stopped()))):
                    raise RuntimeError("Heater restoration evidence changed before relay dispatch")

            if not await self._bounded_service("switch", "turn_on", self.bindings.heater_switch,
                                              timeout=deadline - time.monotonic(),
                                              _before_dispatch=before_restore_dispatch):
                return
            while (not self._switch_observed("on", after=requested)
                   and not self._closing and time.monotonic() < deadline):
                if (not self._restore_authorized() or self._external_power().issue or self._restore_interrupted
                        or self._manual_recovery_required or not self._supervised_restore_check()):
                    return
                await asyncio.sleep(min(.1, RESTORE_ACK_SECONDS / 5))
            if (not self._switch_observed("on", after=requested) or self._closing
                    or self._restore_interrupted or self._manual_recovery_required
                    or time.monotonic() >= deadline):
                return
            # Power recovery never means heat recovery.  Supersede a retained
            # thermostat mode, then require fresh Off before opening the gate.
            # A watt-meter restoration needs new low-load feedback after this
            # Off request. Wait only within the existing restore deadline; a
            # cached pre-On low reading must not open the activation gate.
            require_meter = self._heater_meter()[3] == "power" or prior_meter_missing
            if staged:
                self._supervised_restore["on_wall"] = _timestamp(
                    _state_parts(self._state(self.bindings.heater_switch))[2])
                acknowledged = await self._supervised_reconnect()
            else:
                acknowledged = await self._off_ack(max(0, deadline - time.monotonic()), require_meter=require_meter)
            if acknowledged:
                while self._service_tasks and (self._supervised_restore_check() if staged else time.monotonic() < deadline):
                    if (self._closing or self._restore_interrupted
                            or self._manual_recovery_required or self._external_power().issue):
                        return
                    await asyncio.sleep(min(.1, RESTORE_ACK_SECONDS / 5))
                reading = evaluate_power(self._states(), self.bindings, self._wall_clock())
                if (self._restore_authorized() and reading.safe_to_rearm and not self._closed and not self._closing
                        and (self._supervised_restore_check() if staged else time.monotonic() < deadline)
                        and (not commissioning or (not self._commissioning_expired()
                             and self._commissioning_external_trusted()
                             and self._commissioning_controller_stopped()))
                        and not self._restore_interrupted and not self._manual_recovery_required
                        and not self._service_tasks and not self._positive_tasks and not self._compensation_pending
                        and not self.pending_external_jobs()
                        and self._switch_observed("on", after=requested)
                        and (not staged or self._heater_off(fresh=True))
                        and (not require_meter or self._heater_meter_stopped() is True)):
                    self._reading = reading
                    self._clear()
                    success = True
        finally:
            if not success:
                if commissioning:
                    self._commissioning_failed("Restoration was not verified within its deadline; manual recovery required")
                if on_attempted or self._positive_tasks:
                    if staged:
                        # Serialize early watchdog cuts and final cleanup.
                        if self._compensating_task is None or self._compensating_task.done():
                            self._schedule_compensating_cut()
                        await asyncio.wait((self._compensating_task,), timeout=2 * SERVICE_TIMEOUT_SECONDS + .1)
                    else:
                        await self._cut_switch(force=True)
                self._restore_intent = False
                self._tripped_at = self._clock()
                self._trip_wall = self._wall_clock()
                self._safe_since = None
                self._save()
            self._restoring = False
            self._restore_on_requested = False
            self._supervised_restore = None
            self._publish()

    async def async_close(self):
        """Stop observation; never restore power or enable heat during teardown."""
        if self._closed:
            return True
        self._commissioning_failed("Integration stopped during relay commissioning", status="interrupted")
        first = not self._closing
        self._closing = True
        self._blocked = True
        tasks = [t for t in (self._check_task, self._action_task, self._commissioning_task) if t and not t.done()]
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
        if (self.live and self._cut_owned and not self._switch_cut_observed()
                and not self._manual_policy_control):
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
