"""Opt-in, one-call blower trial dispatch through the normal native interlocks.

The journal is written before each attempt. Cached HA matches are never physical
acknowledgments. Interrupted trials use the separately armed native Auto handoff, never replay.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

from homeassistant.helpers.storage import Store
from homeassistant.core import callback
from homeassistant.const import EVENT_CALL_SERVICE

from .controller.ha import HAActivationDeferred, HAError
from .controller.head_fan_command import FanCommand, FanContext
from .controller.head_fan_investigation import experiment_window, fan_evidence
from .controller.head_fan_trial import FanTrial

TERMINAL = {"complete", "cancelled", "needs_attention"}


class HeadFanTrials:
    def __init__(self, runtime, entry_id):
        self.runtime = runtime
        self.hass = runtime.hass
        self.store = Store(self.hass, 1, f"house_climate.{entry_id}.head_fan_trial")
        self.trial = None
        self.error = None
        self.command = None
        self._lock = asyncio.Lock()
        self._journal_lock = asyncio.Lock()
        self._fallback_resolution = None
        self._ticket = None
        self._call_mode = None
        self._cancel = False
        self._restore_started = None
        self._restore_monotonic = None
        self._trial_deadline_monotonic = None
        self._external_change = False
        self._target_seen = False
        self._remove_listener = None
        self._lifecycle_task = None

    @property
    def diagnostics(self):
        return {"trial": asdict(self.trial) if self.trial else None,
                "error": self.error, "physical_confirmation": "unknown",
                "evidence": self.command.result.__dict__ if self.command else None}

    def binding(self, entity):
        cfg = self.runtime.config
        rooms = sorted(name for name, room in cfg.zones.items()
                       if cfg.heads.get(room.head) == entity)
        fingerprint = hashlib.sha256(json.dumps([entity, [
            [name, cfg.zones[name].head, sorted(p.entity_id for p in cfg.zones[name].probes)]
            for name in rooms]], separators=(",", ":")).encode()).hexdigest()
        return rooms, fingerprint

    @callback
    def _service_event(self, event):
        t = self.trial
        if not t or event.data.get("domain") != "climate":
            return
        controls = self.runtime.controls
        plan = getattr(controls, "handoff", None) or {}
        recovery = plan.get("blower_recovery", {}).get(t.entity_id)
        if t.stage in {"complete", "cancelled"} and not recovery:
            return
        if event.data.get("service") not in {"set_fan_mode", "set_hvac_mode", "set_temperature", "turn_off", "turn_on"}:
            return
        # Only explicit user HVAC commands are manual takeover; ordinary
        # controller HVAC calls must not cancel their own fan experiment.
        if event.data.get("service") != "set_fan_mode" and getattr(event.context, "user_id", None) is None:
            return
        if recovery and event.context.id == recovery.get("context_id"):
            return
        if self._ticket and event.context.id == self._ticket[0]:
            return
        data = event.data.get("service_data", {})
        entities = data.get("entity_id", [])
        if isinstance(entities, str):
            entities = [entities]
        if t.entity_id in entities or "all" in entities or not entities:
            self._external_change = True
            if recovery:
                # In-memory revocation happens before any persistence await.
                recovery["stage"] = "superseded"
                if event.data.get("service") != "set_fan_mode":
                    recovery["manual_hvac"] = True
                    plan["stage"] = "blocked"
                self.hass.async_create_background_task(
                    controls._save(controls.owned, controls.pending_fans),
                    "Home Climate manual fan supersession", eager_start=False)
            self.runtime.wake_event.set()

    async def async_load(self):
        self.runtime.controls.head_fan_trials = self
        self._remove_listener = self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._service_event)
        try:
            raw = await self.store.async_load()
        except Exception:
            self.error = "Trial journal unavailable; enrollment and dispatch blocked"
            return
        if raw is not None:
            try:
                self.trial = FanTrial.from_dict(raw).after_restart()
                await self.store.async_save(asdict(self.trial))
            except (ValueError, TypeError, KeyError):
                self.error = "Invalid trial journal; enrollment and dispatch blocked"
            except Exception:
                self.error = "Trial restart journal write failed; experimental dispatch blocked"

    async def _save(self, trial):
        async with self._journal_lock:
            resolved = self._fallback_resolution
            if resolved is not None and resolved.trial_id == trial.trial_id:
                trial = resolved
            await self._save_unlocked(trial)

    async def _save_unlocked(self, trial):
        # Never change in-memory authority before durable intent succeeds.
        try:
            await self.store.async_save(asdict(trial))
        except Exception:
            self.error = "Trial journal write failed; dispatch blocked"
            raise
        self.trial = trial
        if trial.stage in {"complete", "cancelled"}:
            controls = self.runtime.controls
            plan = getattr(controls, "handoff", None) or {}
            recovery = plan.get("blower_recovery", {}).get(trial.entity_id)
            if recovery and recovery.get("trial_id") == trial.trial_id:
                recovery["stage"] = "complete" if trial.stage == "complete" else "superseded"
                await controls._save(controls.owned, controls.pending_fans)
        storage = getattr(self.runtime, "thermal_storage", None)
        if storage is not None:
            storage.observe("fan_trial", {"timestamp": datetime.now(timezone.utc).isoformat(),
                                          "head_fan_trial": self.diagnostics})

    def storage_healthy(self):
        storage = getattr(self.runtime, "thermal_storage", None)
        if storage is None:
            return False
        status = storage.diagnostics
        try:
            recent = 0 <= time.time() - float(status["last_commit"]) <= 180
        except (TypeError, ValueError, KeyError):
            recent = False
        return (status.get("status") == "recording" and recent
                and not status.get("backup_paused") and status.get("queue_depth", 128) < 64)

    async def async_enroll(self, *, entity_id, room_ids, mode, duration_minutes,
                           sleep_sensitive=True, overnight_authorized=False):
        async with self._lock:
            if self.error or (self.trial and self.trial.stage not in {"complete", "cancelled"}):
                raise ValueError("Resolve the existing trial/journal before enrolling another")
            if type(duration_minutes) is not int or not 10 <= duration_minutes <= 720:
                raise ValueError("Trial duration must be 10–720 whole minutes")
            r = self.runtime
            if not self.storage_healthy():
                raise ValueError("Healthy recent thermal recording is required for a trial")
            controls = r.controls
            if (not r.live or r.stop_event.is_set() or controls is None
                    or entity_id not in r.config.heads.values()):
                raise ValueError("A live, owned configured heat pump is required")
            controls.check_activation(entity_id, {})
            rooms, fingerprint = self.binding(entity_id)
            if sorted(room_ids) != rooms or not rooms:
                raise ValueError("Acknowledge every room served by this heat pump")
            state = self.hass.states.get(entity_id)
            evidence = fan_evidence(state.as_dict() if state else {})
            if mode not in evidence["advertised_modes"] or evidence["reported_mode"] is None:
                raise ValueError("Current fan capability/baseline is unavailable")
            if mode == evidence["reported_mode"]:
                raise ValueError("Requested mode already reported; no intervention needed")
            now = time.time()
            trial = FanTrial(trial_id=uuid4().hex, entity_id=entity_id, mode=mode,
                baseline=evidence["reported_mode"], starts_at=now,
                ends_at=now + duration_minutes * 60, sleep_sensitive=sleep_sensitive,
                overnight_authorized=overnight_authorized,
                ownership_generation=controls.command_generation(entity_id),
                binding_fingerprint=fingerprint)
            trial.validate(now)
            window = experiment_window(datetime.fromtimestamp(now, timezone.utc),
                self.hass.config.time_zone, trial_seconds=duration_minutes * 60)
            if not window["eligible"] and not (overnight_authorized and not sleep_sensitive):
                raise ValueError("Trial and restoration must fit outside sleeping hours")
            self.command = None
            self._trial_deadline_monotonic = time.monotonic() + duration_minutes * 60
            self._cancel = False
            self._external_change = False
            self._target_seen = False
            await self._save(trial)
            r.wake_event.set()
            return self.diagnostics

    async def async_cancel(self):
        async with self._lock:
            if self.trial and self.trial.stage == "enrolled":
                await self._save(replace(self.trial, stage="cancelled", reason="user_cancelled_before_dispatch"))
            elif self.trial and self.trial.stage not in TERMINAL:
                self._cancel = True
                # Persist expiry, so cancellation survives restart.
                await self._save(replace(self.trial, ends_at=min(self.trial.ends_at, max(self.trial.starts_at + .001, time.time())), reason="user_cancelled_restore_pending"))
            self.runtime.wake_event.set()
            return self.diagnostics

    async def async_acknowledge_restoration(self):
        """Explicit human review, not an equipment acknowledgment or a write."""
        async with self._lock:
            t = self.trial
            if self.error or t is None or t.stage != "needs_attention":
                raise ValueError("An intact trial requiring review is needed")
            state = self.hass.states.get(t.entity_id)
            if fan_evidence(state.as_dict() if state else {})["reported_mode"] != t.baseline:
                raise ValueError("Restore the baseline fan setting before acknowledging review")
            api = self.runtime.api
            if api.service_jobs({t.entity_id}) or api.uncertain_service_jobs({t.entity_id}):
                raise ValueError("A device service is still pending or uncertain")
            await self._save(replace(t, stage="cancelled", reason="human_review_baseline_reported_unverified"))
            return self.diagnostics

    def validate_dispatch(self, token, entity, mode):
        """Recheck authority on HA's loop immediately before service dispatch."""
        t, r = self.trial, self.runtime
        if (self.error or self._external_change or self._fallback_resolution is not None
                and t and self._fallback_resolution.trial_id == t.trial_id
                or not t or self._ticket != (token, entity, mode)):
            raise HAActivationDeferred("Fan trial ticket is no longer current")
        if r.stop_event.is_set() or self.binding(entity)[1] != t.binding_fingerprint:
            raise HAActivationDeferred("Fan trial configuration or lifecycle changed")
        if r.controls.command_generation(entity) != t.ownership_generation:
            raise HAActivationDeferred("Fan trial ownership changed")
        state = self.hass.states.get(entity)
        evidence = fan_evidence(state.as_dict() if state else {})
        if (mode not in evidence["advertised_modes"] or evidence["reported_mode"] not in (t.baseline, t.mode)):
            raise HAActivationDeferred("Fan trial capability/report changed")
        if t.stage == "request_reserved":
            if not self.storage_healthy():
                raise HAActivationDeferred("Thermal recording is no longer healthy")
            window = experiment_window(datetime.now(timezone.utc), self.hass.config.time_zone,
                                       trial_seconds=60)
            if (t.ends_at - time.time() <= 360 or self._cancel
                    or self._trial_deadline_monotonic is None
                    or self._trial_deadline_monotonic - time.monotonic() <= 360
                    or not (window["eligible"] or (t.overnight_authorized and not t.sleep_sensitive))
                    or state.state != self._call_mode):
                raise HAActivationDeferred("Fan trial window or natural call ended")
        elif t.stage != "restore_reserved":
            raise HAActivationDeferred("Fan trial is not reserved for dispatch")

    async def async_fallback_resolved(self, record):
        """Blower cleanup evidence; native HVAC handoff remains independent."""
        t = self.trial
        if t and t.trial_id == record.get("trial_id") and t.stage not in {"complete", "cancelled"}:
            self._fallback_resolution = replace(t,
                stage="complete" if record["stage"] == "complete" else "cancelled",
                reason="blower_auto_reported_native_handoff_independent" if record["stage"] == "complete"
                       else "manual_setting_preserved_native_handoff_independent")
            await self._save(self._fallback_resolution)

    async def _arm_fallback(self):
        """Persist native recovery authority before any experimental write.

        This is a separate safety obligation, not permission to replay a trial.
        The native watchdog owns all Off/Auto ordering and command tracking.
        """
        t, controls = self.trial, self.runtime.controls
        async with controls._check_lock:
            if (not controls.enabled or controls.recovering or controls.takeover_pending
                    or t.entity_id not in controls.owned
                    or controls.command_generation(t.entity_id) != t.ownership_generation):
                raise HAActivationDeferred("Ownership changed before fallback was armed")
            plan = controls.handoff
            if not plan or not plan.get("authorized") or plan.get("stage") != "armed":
                raise HAActivationDeferred("A native Auto recovery plan is required")
            state = self.hass.states.get(t.entity_id)
            if (not state or "auto" not in state.attributes.get("fan_modes", ())
                    or "heat_cool" not in state.attributes.get("hvac_modes", ())):
                raise HAActivationDeferred("Blower and HVAC Auto recovery capabilities are required")
            from .recovery_capability import mode_issue
            if issue := mode_issue(self.hass, t.entity_id):
                raise HAActivationDeferred(issue)
            plan.setdefault("blower_recovery", {})[t.entity_id] = {
                "trial_id": t.trial_id, "mode": t.mode, "baseline": t.baseline,
                "armed_at": time.time(), "stage": "pending", "attempts": 0}
            await controls._save(controls.owned, controls.pending_fans)

    async def _dispatch(self, mode, *, restoring=False):
        t, r = self.trial, self.runtime
        if not restoring:
            await self._arm_fallback()
        await self._save(replace(t, stage="restore_reserved" if restoring else "request_reserved",
                                 reason="durable_attempt_reserved"))
        token = uuid4().hex
        self._ticket = (token, t.entity_id, mode)
        def send():
            return r.api.request("services/climate/set_fan_mode",
                {"entity_id": t.entity_id, "fan_mode": mode},
                activation_deadline=time.monotonic() + 5, fan_trial_token=token)
        try:
            await self.hass.async_add_executor_job(send)
        except (HAError, TimeoutError):
            # An exception does not prove that the equipment rejected the write.
            await self._save(replace(self.trial, stage="needs_attention",
                                     reason="restore_outcome_uncertain" if restoring else "request_outcome_uncertain"))
            return False
        finally:
            self._ticket = None
        if self.trial.stage in {"complete", "cancelled"}:
            # Native cleanup or a newer manual action won while the service ran.
            return False
        if r.stop_event.is_set() or not r.controls.enabled:
            await self._save(replace(self.trial, stage="needs_attention",
                                     reason="native_auto_handoff_pending"))
            return False
        self._restore_started = time.time() if restoring else None
        self._restore_monotonic = time.monotonic() if restoring else None
        await self._save(replace(self.trial, stage="active" if not restoring else "restore_reserved",
                                 reason="awaiting_restore_report" if restoring else "service_returned_unverified"))
        return True

    async def async_tick(self, *, pending, safe, plan):
        async with self._lock:
            t, r = self.trial, self.runtime
            if not t or t.stage in TERMINAL or self.error:
                return
            if time.time() < t.starts_at:
                await self._save(replace(t, stage="needs_attention", reason="wall_clock_reversed_no_write"))
                return
            if self._external_change:
                await self._save(replace(t, stage="cancelled", reason="external_fan_service_superseded_trial_no_restore"))
                return
            controls = r.controls
            if (not controls or r.stop_event.is_set() or t.entity_id not in controls.owned
                    or controls.command_generation(t.entity_id) != t.ownership_generation
                    or self.binding(t.entity_id)[1] != t.binding_fingerprint):
                await self._save(replace(t, stage="needs_attention", reason="ownership_or_configuration_changed_no_write"))
                return
            state_obj = self.hass.states.get(t.entity_id)
            state = state_obj.as_dict() if state_obj else {}
            evidence = fan_evidence(state)
            if evidence["reported_mode"] not in (t.baseline, t.mode):
                await self._save(replace(t, stage="needs_attention", reason="external_or_unavailable_report_no_write"))
                return
            if t.stage == "active":
                if self._target_seen and evidence["reported_mode"] == t.baseline:
                    await self._save(replace(t, stage="cancelled", reason="baseline_returned_externally_no_write"))
                    return
                self._target_seen |= evidence["reported_mode"] == t.mode
            if t.stage == "restore_reserved":
                if self._restore_started is None or self._restore_monotonic is None:
                    await self._save(replace(t, stage="needs_attention", reason="restoration_receipt_missing"))
                elif (evidence["reported_mode"] == t.baseline
                      and state_obj.last_updated.timestamp() > self._restore_started
                      and time.monotonic() - self._restore_monotonic >= 10):
                    await self._save(replace(t, stage="complete", reason="baseline_reported_physical_unverified"))
                elif time.monotonic() - self._restore_monotonic >= 60:
                    await self._save(replace(t, stage="needs_attention", reason="restoration_report_unconfirmed"))
                return
            key = next(k for k, entity in r.config.heads.items() if entity == t.entity_id)
            ended = (t.expired(time.time()) or self._cancel or not self.storage_healthy()
                     or (self._trial_deadline_monotonic is not None
                         and time.monotonic() >= self._trial_deadline_monotonic) or
                     (t.stage == "active" and (state.get("state") != self._call_mode
                      or key not in plan.head_targets_f)))
            if t.stage == "enrolled" and ended:
                await self._save(replace(t, stage="cancelled", reason="expired_before_dispatch"))
                return
            if t.stage == "active" and ended:
                if pending or not safe:
                    # Safety/Off always wins. Never claim cleanup happened.
                    if time.time() > t.ends_at + 300:
                        await self._save(replace(t, stage="needs_attention", reason="restoration_blocked"))
                    return
                await self._dispatch(t.baseline, restoring=True)
                return
            if pending or not safe:
                return
            if t.stage == "enrolled" and evidence["reported_mode"] != t.baseline:
                await self._save(replace(t, stage="cancelled", reason="baseline_changed_before_dispatch"))
                return
            rooms, _ = self.binding(t.entity_id)
            if (state.get("state") != plan.mode or key not in plan.head_targets_f or key in plan.inhibited_heads
                    or any(plan.rooms.get(room, {}).get("sources", 0) < 2 for room in rooms)):
                return
            now = datetime.now(timezone.utc)
            context = FanContext(generation=t.trial_id, authorized=True, owned=True,
                safe=True, hvac_pending=False, sleep_sensitive=t.sleep_sensitive,
                overnight_authorized=t.overnight_authorized,
                trial_ends_at=datetime.fromtimestamp(t.ends_at, timezone.utc))
            if t.stage == "enrolled":
                self.command = FanCommand()
                payload = self.command.request(t.mode, state, context, elapsed=time.monotonic(),
                    wall_now=now, timezone=self.hass.config.time_zone,
                    sample_id=str(state_obj.last_updated))
                if payload:
                    self._call_mode = state.get("state")
                    if await self._dispatch(t.mode):
                        self.command.dispatched(outcome="accepted", elapsed=time.monotonic())
            elif self.command:
                # A changed HA state event is still cached evidence, not a poll.
                age = max(0, time.time() - state_obj.last_updated.timestamp())
                self.command.observe(state, context, elapsed=time.monotonic(), wall_now=now,
                    timezone=self.hass.config.time_zone, sample_id=str(state_obj.last_updated),
                    event_at=time.monotonic() - age)

    @callback
    def check_lifecycle(self):
        t, r = self.trial, self.runtime
        if (not t or t.stage in TERMINAL or self._lifecycle_task is not None
                or (r.controls and r.controls.enabled and not r.stop_event.is_set())):
            return
        async def interrupt():
            try:
                async with asyncio.timeout(1):
                    async with self._lock:
                        if self.trial and self.trial.stage not in TERMINAL:
                            stage = "cancelled" if self.trial.stage == "enrolled" else "needs_attention"
                            await self._save(replace(self.trial, stage=stage,
                                reason="ownership_released_no_experimental_write"))
            except Exception:
                self.error = "Trial interrupted; journal unresolved and review required"
            finally:
                self._lifecycle_task = None
        self._lifecycle_task = self.hass.async_create_background_task(
            interrupt(), "Home Climate trial lifecycle", eager_start=False)

    async def async_close(self):
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
        # Never postpone emergency Off/handoff to send experimental writes.
        async with self._lock:
            if self.trial and self.trial.stage not in TERMINAL:
                stage = "cancelled" if self.trial.stage == "enrolled" else "needs_attention"
                await self._save(replace(self.trial, stage=stage,
                                         reason="controller_stopped_no_experimental_write"))
