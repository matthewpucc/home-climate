"""Fixed, heater-only load shedding for the living-room battery-backed circuit.

Emporia reports real power, not RMS branch current.  The 1350 W input budget is
deliberately below 12 A at nominal US voltage; it is a conservative automation
budget, not a claim to measure branch amps or replace circuit protection.  The
Elite's input overlaps the Emporia reading: they are compared, never added.

Only the Dyson and its dedicated plug may be switched.  The Elite AC output and
network equipment are never actuators.  This module imports HA lazily so its
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
                and self.output_w < OUTPUT_SPIKE_W * .8 and self.heater_a < 1
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

    @property
    def blocked(self):
        return (self._blocked or self._closing or self._closed or bool(self._persistence_error)
                or _state_parts(self.hass.states.get(self.bindings.heater_switch))[0] != "on")

    @property
    def switch_cut_owned(self):
        return self._cut_owned

    @property
    def power_cut_confirmed(self):
        """Actual dedicated-plug feedback, not completion of a service call."""
        return (self._switch_cut_observed() and not self._positive_tasks
                and not self._compensation_pending and not self._restoring)

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
                "persistence_error": self._persistence_error,
                "circuit_power_w": r.circuit_w, "elite_input_w": r.input_w,
                "elite_output_w": r.output_w, "battery_percent": r.battery_percent,
                "heater_current_a": r.heater_a, "input_budget_w": r.input_budget_w,
                "branch_current_measured": False, "live": self.live}

    def _publish(self):
        current = self.diagnostics
        stable = {key: current[key] for key in (
            "blocked", "reason", "latched", "switch_cut_owned", "power_cut_confirmed",
            "restoring", "restore_attempts", "power_restore_inflight",
            "power_restore_cleanup_pending", "persistence_error", "live")}
        if stable != self._last_diagnostics:
            self._last_diagnostics = stable
            self.notify()

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
        if self._latched or self._cut_owned:
            self._latched = True
            try:
                self._trip_wall = float(data["trip_wall"])
                age = max(0, min(COOLDOWN_SECONDS, self._wall_clock() - self._trip_wall))
            except (KeyError, TypeError, ValueError, OverflowError):
                age = 0
                self._trip_wall = self._wall_clock()
            self._tripped_at = self._clock() - age
            self._reason = "Recovering a persisted heater power cutoff"
        self._publish()

    def async_start(self):
        """Register inexpensive cached-state checks on HA's loop."""
        if self._active or self._closed:
            return
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

        @callback
        def changed(_event=None):
            if (_event and _event.data.get("entity_id") == self.bindings.heater_switch
                    and (state := _event.data.get("new_state")) is not None
                    and state.state == "on" and self._cut_owned
                    and not self._restoring and not self._cutting
                    and not self._positive_tasks and not self._compensation_pending):
                self._manual_power_seen = True
            self._schedule_check()

        self._active = True
        self._unsubscribers = [
            async_track_state_change_event(self.hass, self.entity_ids, changed),
            async_track_time_interval(self.hass, changed, timedelta(seconds=CHECK_SECONDS)),
        ]
        self._schedule_check()

    def check_activation(self, entity_id, body=None):
        """Recheck immediately before dispatch; callers always permit Off."""
        if entity_id != self.bindings.heater:
            return
        self._observe()
        switch_state = _state_parts(self.hass.states.get(self.bindings.heater_switch))[0]
        if self.blocked or switch_state != "on":
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
            return self._off_request_wall is not None and _timestamp(reported) > self._off_request_wall
        except (ValueError, TypeError, OverflowError):
            return False

    def _switch_observed(self, desired, *, after=None):
        state, _, reported = _state_parts(self.hass.states.get(self.bindings.heater_switch))
        if state != desired:
            return False
        try:
            return after is None or _timestamp(reported) > after
        except (ValueError, TypeError, OverflowError):
            return False

    def _switch_changed(self):
        state = self.hass.states.get(self.bindings.heater_switch)
        value = state.get("last_changed") if isinstance(state, dict) else getattr(state, "last_changed", None)
        try:
            return _timestamp(value)
        except (ValueError, TypeError, OverflowError):
            return None

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
        if self._persistence_error and self._clock() >= self._save_retry_at:
            self._save_retry_at = self._clock() + 60
            self._save()
        r = self._reading
        if r.issue:
            self._blocked = True
            self._reason = r.issue
            self._safe_since = None
            # A delayed sensor during startup inhibits a new call immediately,
            # but does not manufacture a five-minute trip if the heater is Off.
            if not r.telemetry_missing or not self._heater_off() or self._latched:
                self._trip(r.issue)
                if self.live and not self._restoring:
                    if self._off_confirmed and not self._heater_off():
                        self._start_action(self._cut_switch())
                    elif not self._off_confirmed and not self.power_cut_confirmed:
                        self._start_action(self._shed())
        elif self._latched:
            self._blocked = True
            self._reason = "Heater held off until battery >40%, stable safe load, and cooldown"
            if not self._restoring and not self._off_confirmed and not self.power_cut_confirmed:
                self._start_action(self._shed())
            if self._off_confirmed and not self._heater_off() and not self._restoring:
                self._start_action(self._cut_switch())
            if r.safe_to_rearm:
                self._safe_since = self._safe_since if self._safe_since is not None else self._clock()
                ready = (self._clock() - self._safe_since >= SAFE_LOAD_SECONDS
                         and self._clock() - self._tripped_at >= COOLDOWN_SECONDS)
                if (ready and self.live and not self._restoring and not self._positive_tasks
                        and not self._compensation_pending and not self._persistence_error):
                    switch = _state_parts(self.hass.states.get(self.bindings.heater_switch))[0]
                    if self._cut_owned and switch == "off":
                        if self._restore_attempts < MAX_RESTORE_ATTEMPTS:
                            self._start_action(self._restore())
                        else:
                            self._reason = "Heater power restoration failed three times; check the dedicated plug and Dyson"
                    elif self._heater_off(fresh=True):
                        self._clear()
            else:
                self._safe_since = None
        else:
            self._blocked = False
            self._reason = (None if self._switch_observed("on")
                            else "Heater dedicated plug is off or unavailable")
        self._publish()

    def _clear(self):
        self._latched = self._blocked = self._cut_owned = False
        self._restore_intent = False
        self._reason = None
        self._tripped_at = self._trip_wall = self._safe_since = None
        self._restore_attempts = 0
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
                "restore_attempts": self._restore_attempts}

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
        task = self.hass.async_create_task(self.hass.services.async_call(
            domain, service, {"entity_id": entity, **data}, blocking=True))
        self._service_tasks.add(task)
        positive = domain == "switch" and service == "turn_on"
        if positive:
            self._positive_tasks.add(task)

        def finished(done):
            self._service_tasks.discard(done)
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
            else:
                task.cancel()
            return False
        try:
            task.result()
            return True
        except (Exception, asyncio.CancelledError):
            return False

    async def _off_ack(self, seconds):
        self._off_request_wall = self._wall_clock()
        task = self._service("climate", "set_hvac_mode", self.bindings.heater, hvac_mode="off")
        deadline = time.monotonic() + seconds
        while not self._closed and time.monotonic() < deadline:
            if self._restoring and evaluate_power(self._states(), self.bindings, self._wall_clock()).issue:
                task.cancel()
                return False
            if self._heater_off(fresh=True):
                self._off_confirmed = True
                return True
            if task.done() and (task.cancelled() or task.exception()):
                break
            await asyncio.sleep(min(.1, seconds / 5))
        if not task.done():
            task.cancel()  # Do not await cancellation: integrations can ignore it.
        return False

    async def _shed(self):
        if self._cut_owned and _state_parts(self.hass.states.get(self.bindings.heater_switch))[0] == "off":
            return
        if not await self._off_ack(OFF_ACK_SECONDS):
            await self._cut_switch()
        self._publish()

    async def _cut_switch(self, *, force=False):
        if not self.live:
            return False
        self._cutting = True
        switch = _state_parts(self.hass.states.get(self.bindings.heater_switch))[0]
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
            for task in tasks:
                task.cancel()
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
        self._publish()
        return True
