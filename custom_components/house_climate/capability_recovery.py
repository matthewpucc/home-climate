"""Verified capability-loss incidents and scoped native owner repair.

Metadata refreshes are explicit rechecks, not independent votes from cached
worker polls. A reload is never an actuator acknowledgement.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import logging
import time

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from .capabilities import CapabilityEvidence

_LOGGER = logging.getLogger(__name__)
RECHECK_SECONDS = 60
FAILED_RECHECKS = 3
PERSISTENCE_SECONDS = 120
POLL_SECONDS = 1
OFF_RETRY_SECONDS = 10


@dataclass
class Incident:
    entity: str
    owner: str
    identity: str
    missing: frozenset
    since: float
    failures: int = 0
    next_check: float = 0
    result: str = "Waiting for explicit capability refresh"
    last_refresh: dict = field(default_factory=dict)


class CapabilityRecovery:
    """Learn capabilities passively; only repair a live, owned native scope."""

    def __init__(self, manager):
        self.manager = manager
        self.hass, self.runtime = manager.hass, manager.runtime
        self.clock = manager._clock
        self.evidence = CapabilityEvidence(self.hass, clock=self.clock)
        self.incidents = {}
        self.refreshes = {}
        self.status = {}
        self.detail = None
        self.lease = None
        self.reload_job = None
        self.closed = False
        self._charged_owners = set()
        self.report_revision = 0

    async def async_load(self, entry_id):
        await self.evidence.async_load(entry_id)

    def snapshot(self):
        rows = {entity: {
            "missing_modes": sorted(row.missing), "failed_rechecks": row.failures,
            "required": self.required(row), "reason": row.result,
            "last_refresh": dict(row.last_refresh),
        } for entity, row in self.incidents.items()}
        result = {"capability_incidents": rows}
        if self.evidence.storage_warning:
            result["capability_storage_warning"] = self.evidence.storage_warning
        if self.detail:
            result["capability_repair"] = dict(self.detail)
        return result

    def observe_entity(self, entity):
        item = er.async_get(self.hass).async_get(entity)
        owner = (self.hass.config_entries.async_get_entry(item.config_entry_id)
                 if item and item.config_entry_id else None)
        if owner is not None and owner.domain == "house_climate":
            return owner, None
        return owner, self.evidence.observe(item, owner, self.hass.states.get(entity))

    def owned(self, entities):
        controls = self.runtime.controls
        return bool(self.manager._enabled() and controls is not None
                    and set(entities) <= set(getattr(controls, "owned", ())))

    def required(self, row):
        """Arbitration wins over an opposite room's unavailable-mode request."""
        key = next((key for key, entity in self.runtime.config.heads.items() if entity == row.entity), None)
        requests = self.status.get("unavailable_mode_requests", {}).get(key, {})
        if not isinstance(requests, dict):
            return False
        selected = self.status.get("mode")
        if selected not in ("heat", "cool"):
            actual = {state.state for entity in self.runtime.config.heads.values()
                      if (state := self.hass.states.get(entity)) is not None and state.state in ("heat", "cool")}
            if len(actual) == 1:
                selected = next(iter(actual))
            elif actual:
                return False
        for mode, request in requests.items():
            if (mode not in ("heat", "cool") or not isinstance(request, dict)
                    or not ({mode, "off"} & row.missing)):
                continue
            if selected in ("heat", "cool"):
                if selected == mode:
                    return True
            else:
                return True
        return False

    def affected(self, owner):
        heads = set(self.runtime.config.heads.values())
        return tuple(sorted(item.entity_id for item in er.async_entries_for_config_entry(
            er.async_get(self.hass), owner) if item.entity_id in heads))

    def wants_owner(self, scope):
        keys = {key for key, entity in self.runtime.config.heads.items() if entity in scope}
        return bool(keys & set(self.status.get("head_targets_f", {})))

    def obsolete(self, row, admission_required, scope):
        required = self.required(row)
        return (not self.valid(row) or admission_required and not required
                or not required and self.wants_owner(scope))

    def valid(self, row):
        owner, report = self.observe_entity(row.entity)
        return bool(owner and owner.entry_id == row.owner and report
                    and report["identity"] == row.identity and row.missing <= report["missing"])

    def input_signature(self):
        from .controller.config import Settings
        from .controller.recovery_inputs import signature
        states = {state["entity_id"]: state for state in self.runtime.api._read_states()}
        config = replace(self.runtime.config, timezone=self.hass.config.time_zone)
        now = datetime.now(timezone.utc)
        comfort = getattr(self.runtime, "comfort", None)
        settings = comfort.settings(states, config, now) if comfort else Settings.read(states, config)
        return signature(config, states, now, settings, self.hass.config.units.temperature_unit)

    def fresh_inputs(self):
        marker = self.status.get("recovery_inputs")
        return bool(marker and marker == self.input_signature())

    def observe(self, status):
        if self.closed:
            return
        self.status = status
        self.report_revision += 1
        now = self.clock()
        for entity in self.runtime.config.heads.values():
            owner, report = self.observe_entity(entity)
            previous = self.incidents.get(entity)
            if report is None:
                # Do not mistake unavailable/restored/setup metadata for a
                # confirmed capability loss. Preserve only an active repair's
                # frozen incident through its intentional integration unload.
                if not (self.lease and entity in self.lease.entities):
                    self.incidents.pop(entity, None)
                continue
            missing = report["missing"]
            if not missing:
                if previous:
                    previous.failures = 0
                    previous.result = "Modes restored; waiting for stable fresh reports"
                    if report["stable"]:
                        self.incidents.pop(entity, None)
                continue
            if (previous is None or previous.identity != report["identity"]
                    or previous.missing != missing):
                previous = self.incidents[entity] = Incident(
                    entity, owner.entry_id, report["identity"], missing, now)
            if (self.owned((entity,)) and now >= previous.next_check
                    and not self.lease and entity not in self.refreshes):
                previous.next_check = now + RECHECK_SECONDS
                task = self.hass.async_create_background_task(
                    self._async_recheck(previous), "Home Climate capability recheck", eager_start=False)
                self.refreshes[entity] = task
                task.add_done_callback(lambda _task, key=entity: self.refreshes.pop(key, None))
        # Only stable actual restoration replenishes attempts. Cooldown survives.
        incident_owners = {row.owner for row in self.incidents.values()}
        generic_owners = {item.config_entry_id for entity in self.manager._first_seen
                          if (item := er.async_get(self.hass).async_get(entity)) is not None}
        for owner in tuple(self._charged_owners):
            if owner in incident_owners or owner in generic_owners:
                continue
            affected = self.affected(owner)
            reports = [self.observe_entity(entity)[1] for entity in affected]
            if reports and all(report and report["stable"] for report in reports):
                self.manager._attempts.pop(owner, None)
                self._charged_owners.discard(owner)
        if not self.manager._enabled():
            self._release_stopped_lease()
            return
        if self.lease:
            return
        if self.manager._task is not None and not self.manager._task.done():
            return
        for row in self.incidents.values():
            scope = self.affected(row.owner)
            if (not self.owned(scope) or not self.fresh_inputs() or row.failures < FAILED_RECHECKS
                    or now - row.since < PERSISTENCE_SECONDS or not self.valid(row)):
                continue
            if self.manager._attempts.get(row.owner, 0) >= 3:
                row.result = "Automatic reload paused after three attempts; manual recovery needed"
                continue
            if now < self.manager._next_retry.get(row.owner, now):
                row.result = "Waiting for integration reload cooldown"
                continue
            if not self.required(row) and (self.wants_owner(scope) or any(
                (state := self.hass.states.get(entity)) is None or state.state != "off"
                for entity in scope
            )):
                row.result = "Unused mode loss is advisory while shared heads are active"
                continue
            self.manager._active_entry = row.owner
            self.manager._task = self.hass.async_create_background_task(
                self._async_repair(row), "Home Climate coordinated capability repair", eager_start=False)
            break

    async def _async_recheck(self, row):
        row.next_check = self.clock() + RECHECK_SECONDS
        before = self.marker(self.hass.states.get(row.entity))
        timed_out = False
        error_type = None
        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call("homeassistant", "update_entity",
                                                   {"entity_id": row.entity}, blocking=True)
            result = "Explicit refresh still reports missing modes"
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            error_type = type(exc).__name__
            result = f"Explicit refresh failed ({error_type}); modes remain missing"
        after = self.marker(self.hass.states.get(row.entity))
        row.last_refresh = {"fresh_report": after != before, "timeout": timed_out,
                            "error_type": error_type, "no_change": after == before}
        if self.incidents.get(row.entity) is row and self.valid(row):
            row.failures += 1
            row.result = result
        elif self.incidents.get(row.entity) is row:
            row.failures = 0
            row.result = "Refresh did not confirm the same capability loss"
        self.manager._notify()

    def _progress(self, row, phase, reason):
        self.detail = {"owner": row.owner, "entities": list(self.lease.entities) if self.lease else [],
                       "missing_modes": sorted(row.missing), "phase": phase, "reason": reason,
                       "attempts": self.manager._attempts.get(row.owner, 0),
                       "last_result": self.manager._last_result.get(row.owner)}
        if self.detail != getattr(self, "_published", None):
            self._published = dict(self.detail)
            self.manager._notify()

    @staticmethod
    def marker(state):
        return state.last_reported if state is not None else None

    def fresh_off(self, entity, baseline):
        state = self.hass.states.get(entity)
        _owner, report = self.observe_entity(entity)
        return bool(report and "off" in report["available"] and state and state.state == "off" and self.marker(state) != baseline
                    and state.attributes.get("hvac_action") not in ("heating", "cooling")
                    and self.runtime.controls._is_off(entity))

    async def _off(self, entity):
        api = self.runtime.api
        await api._async_request(
            "services/climate/set_hvac_mode", {"entity_id": entity, "hvac_mode": "off"},
            time.monotonic() + 10, generation=self.runtime.controls.ownership_generation,
            repair_epoch=api.repair_gate.capture(entity, activation=False))

    async def _quiesce(self, row, required, phase):
        """Retain the first report boundary through ordinary stop retries."""
        scope = self.lease.entities
        baselines = {entity: self.marker(self.hass.states.get(entity)) for entity in scope}
        next_off = {entity: 0 for entity in scope}
        while True:
            if self.closed or not self.owned(scope):
                return False
            if any((item := er.async_get(self.hass).async_get(entity)) is None
                   or item.config_entry_id != row.owner for entity in scope):
                self._progress(row, "blocked", "Head ownership changed during repair; manual reconciliation required")
                return False
            if self.runtime.api.uncertain_service_jobs(scope):
                self._progress(row, "blocked", "A device service has uncertain completion; calls remain fenced")
                await asyncio.sleep(POLL_SECONDS)
                continue
            else:
                for entity in scope:
                    if self.fresh_off(entity, baselines[entity]):
                        continue
                    if self.runtime.api.service_jobs((entity,)) or self.clock() < next_off[entity]:
                        continue
                    next_off[entity] = self.clock() + OFF_RETRY_SECONDS
                    try:
                        await self._off(entity)
                    except Exception as exc:
                        self._progress(row, phase, f"Off command awaiting recovery ({type(exc).__name__})")
                if (not self.runtime.api.service_jobs(scope)
                        and not self.runtime.api.uncertain_service_jobs(scope)
                        and all(self.fresh_off(entity, baselines[entity]) for entity in required)):
                    return True
            self._progress(row, phase, "Planned repair Off; awaiting fresh device acknowledgement")
            await asyncio.sleep(POLL_SECONDS)

    async def _async_reload_checked(self, row, scope, manual, admission_required):
        """Recheck at the actual queued task boundary, then invoke without yielding."""
        entry = self.hass.config_entries.async_get_entry(row.owner)
        if self.runtime.api.service_jobs(scope) or self.runtime.api.uncertain_service_jobs(scope):
            self.manager._last_result[row.owner] = "Repair deferred before actual reload admission: owner device services are still running or uncertain"
            return None
        if (self.closed or not self.owned(scope) or tuple(scope) != self.affected(row.owner)
                or entry is None or entry.domain == "house_climate" or entry.disabled_by
                or entry.state is not ConfigEntryState.LOADED
                or not manual and (not self.fresh_inputs() or self.obsolete(row, admission_required, scope))):
            self.manager._last_result[row.owner] = "Repair cancelled before actual reload admission: decision or ownership changed"
            return None
        if not manual:
            attempts = self.manager._attempts.get(row.owner, 0)
            self._charged_owners.add(row.owner)
            self.manager._attempts[row.owner] = attempts + 1
            self.manager._next_retry[row.owner] = self.clock() + (900, 1800, 3600)[min(attempts, 2)]
        for entity in scope:
            self.evidence._break_candidate(er.async_get(self.hass).async_get(entity))
        _LOGGER.warning("Planned capability repair: reloading owning integration %s; affected head calls are paused", entry.domain)
        return await self.hass.config_entries.async_reload(row.owner)

    async def _async_repair(self, row, *, manual=False):
        api = self.runtime.api
        reload_finished = False
        try:
            scope = self.affected(row.owner)
            # Repeat identity/demand/ownership checks after queued task admission.
            if (not self.owned(scope) or not manual and (not self.fresh_inputs() or not self.valid(row)
                    or (not self.required(row) and (self.wants_owner(scope) or any(
                        (state := self.hass.states.get(entity)) is None or state.state != "off"
                        for entity in scope))))):
                return False
            admission_required = self.required(row)
            self.lease = api.repair_gate.begin(row.owner, scope)
            self._progress(row, "draining", "Planned shared-entry repair; new starts fenced")
            while api.service_jobs(scope) or api.uncertain_service_jobs(scope):
                if self.closed or not self.owned(scope):
                    return False
                self._progress(row, "draining", "Blocked until actual device service jobs have completed")
                await asyncio.sleep(POLL_SECONDS)
            broken = {other.entity for other in self.incidents.values()
                      if other.owner == row.owner and self.valid(other)}
            if manual:
                broken.add(row.entity)
            healthy = set(scope) - broken
            before_off_report = self.report_revision
            if not await self._quiesce(row, healthy, "quiescing"):
                return False
            # Own Off is not evidence that the owner was idle. Wait for a
            # genuinely newer worker decision using the post-pause inputs.
            if not manual:
                while self.report_revision <= before_off_report or not self.fresh_inputs():
                    if self.closed or not self.owned(scope):
                        return False
                    self._progress(row, "decision", "Awaiting fresh post-pause demand and room constraints")
                    await asyncio.sleep(POLL_SECONDS)
            cancelled = not manual and self.obsolete(row, admission_required, scope)
            entry = self.hass.config_entries.async_get_entry(row.owner)
            if not self.owned(scope) or entry is None or entry.state is not ConfigEntryState.LOADED:
                return False
            result = False
            if cancelled:
                self.manager._last_result[row.owner] = "Repair cancelled before reload: demand or device context changed"
                self._progress(row, "verifying", self.manager._last_result[row.owner])
            else:
                # A queued watchdog stop may have appeared while awaiting the
                # new report; never unload its still-running owning handler.
                while api.service_jobs(scope) or api.uncertain_service_jobs(scope):
                    if self.closed or not self.owned(scope):
                        return False
                    self._progress(row, "draining", "Waiting for final owner service-job drain")
                    await asyncio.sleep(POLL_SECONDS)
                # Revalidate again at the actual reload admission boundary.
                if not manual and (not self.fresh_inputs() or self.obsolete(row, admission_required, scope)):
                    cancelled = True
                    self.manager._last_result[row.owner] = "Repair cancelled before reload: decision inputs changed"
                else:
                    self._progress(row, "reloading", "Reload queued; rechecking owning integration and decision inputs")
                    self.reload_job = self.hass.async_create_background_task(
                        self._async_reload_checked(row, scope, manual, admission_required),
                        "Home Climate owning integration reload", eager_start=False)
                    self.reload_job.add_done_callback(
                        lambda job: None if job.cancelled() else job.exception())
                    reload_started = self.clock()
                    while not self.reload_job.done():
                        if self.clock() - reload_started >= 60:
                            self._progress(row, "blocked", "Owning integration reload is still running; calls remain fenced")
                        await asyncio.sleep(POLL_SECONDS)
                    try:
                        result = self.reload_job.result()
                    except Exception as exc:
                        result = False
                        self.manager._last_result[row.owner] = f"Reload failed ({type(exc).__name__}); verifying safe Off before rejoin"
                    else:
                        if result is not None:
                            self.manager._last_result[row.owner] = ("Reload completed; verifying fresh Off reports" if result
                                                                   else "Reload failed; verifying safe Off before rejoin")
                    reload_finished = True
            self._progress(row, "verifying", self.manager._last_result[row.owner])
            if not await self._quiesce(row, set(scope), "verifying"):
                return False
            off_since = time.monotonic()
            wait_started = self.clock()
            minimum = max(self.runtime.config.head_minimum_off_seconds, self.runtime.config.off_settle_seconds)
            while (self.clock() - wait_started < minimum or api.service_jobs(scope)
                   or api.uncertain_service_jobs(scope)):
                if self.closed or not self.owned(scope):
                    return False
                if any(not self.runtime.controls._is_off(entity) for entity in scope):
                    if not await self._quiesce(row, set(scope), "verifying"):
                        return False
                    off_since, wait_started = time.monotonic(), self.clock()
                self._progress(row, "minimum_off", "Fresh Off confirmed; waiting normal minimum-Off time")
                await asyncio.sleep(POLL_SECONDS)
            # No await separates the final actual-job test and epoch release.
            api.repair_gate.release(self.lease, off_since=off_since)
            self.lease = None
            self._progress(row, "ready", "Safe Off rejoin complete; unresolved modes remain filtered independently")
            return bool(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.manager._last_result[row.owner] = f"Capability repair blocked: {type(exc).__name__}: {exc}"
            self._progress(row, "blocked", self.manager._last_result[row.owner])
            _LOGGER.warning("%s", self.manager._last_result[row.owner])
            return False
        finally:
            # Never erase a running reload or uncertain actuator job by dropping
            # its waiter. Disable/unload can stop the devices independently.
            if self.lease and (self.reload_job is None or self.reload_job.done()):
                if (not api.service_jobs(self.lease.entities)
                        and not api.uncertain_service_jobs(self.lease.entities)
                        and (not self.manager._enabled() and all(
                                 self.runtime.controls._is_off(entity) for entity in self.lease.entities))):
                    api.repair_gate.release(self.lease)
                    self.lease = None
            if self.reload_job and self.reload_job.done():
                if not self.reload_job.cancelled():
                    self.reload_job.exception()
                self.reload_job = None
            if self.lease is None:
                self.manager._active_entry = None
            elif not reload_finished and (self.detail or {}).get("phase") != "blocked":
                self._progress(row, "blocked", "Repair remains fenced; shutdown or service completion is not confirmed")
            self.manager._notify()

    def _release_stopped_lease(self):
        if (self.lease and (self.reload_job is None or self.reload_job.done())
                and not self.manager._enabled()
                and not self.runtime.api.service_jobs(self.lease.entities)
                and not self.runtime.api.uncertain_service_jobs(self.lease.entities)
                and all(self.runtime.controls._is_off(entity) for entity in self.lease.entities)):
            self.runtime.api.repair_gate.release(self.lease)
            self.lease = None
            self.manager._active_entry = None
            if self.detail:
                self.detail.update(phase="stopped", reason="Repair stopped after actuator cleanup")

    async def async_close(self):
        self.closed = True
        for task in self.refreshes.values():
            task.cancel()
        if self.refreshes:
            await asyncio.gather(*self.refreshes.values(), return_exceptions=True)
        await self.evidence.async_close()
        self._release_stopped_lease()
        if self.reload_job is not None and not self.reload_job.done():
            return False
        return True
