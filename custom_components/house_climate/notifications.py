"""Bounded, incident-based alerts independent of actuator execution.

Acceptance by a HA service is not proof of delivery to, display on, or sound
from a phone. Critical routing remains visibly unverified without an explicitly
configured capable route; a normal notification and HA notice are still useful.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import math
import re
import time

from .controller.notifications import NotificationSettings
from .handoff import HANDOFF_RETRY_SECONDS

_LOGGER = logging.getLogger(__name__)
SERVICE_TIMEOUT_SECONDS = 5.0
UNAVAILABLE_RETRY_SECONDS = 300.0
MAX_UNAVAILABLE_ATTEMPTS = 3


@dataclass
class Incident:
    key: str
    title: str
    message: str
    severity: str
    active: bool = True
    generation: int = 1
    attempts: int = 0
    retry_at: float = 0.0
    delivery_state: str = "pending"
    critical_route_capable: bool = False
    error: str | None = None
    local_error: str | None = None
    notice_signature: tuple | None = None


class AlertManager:
    """Publish promptly once per incident, with bounded safe retries.

    Only a service or entity target known to be unavailable is automatically
    retried (at most three checks, five minutes apart). A call exception or timeout has uncertain
    delivery and is not blindly repeated. Clearing and later reappearing is a
    new incident; severity escalation is independently actionable.
    """

    def __init__(self, hass, entry_id, settings=None, notify=None, *, clock=time.monotonic):
        self.hass = hass
        self.entry_id = entry_id
        self.settings = NotificationSettings.from_dict(settings)
        self.notify = notify or (lambda: None)
        self._clock = clock
        self._incidents: dict[str, Incident] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._service_tasks: set[asyncio.Task] = set()
        self._status_keys: set[str] = set()
        self._status_fault = None
        self._recovery_identity = None
        self._recovery_grace_until = None
        self._closed = False

    @property
    def diagnostics(self):
        active = [{"key": item.key, "severity": item.severity,
                   "title": item.title, "message": item.message,
                   "delivery_state": item.delivery_state,
                   "critical_route_capable": item.critical_route_capable,
                   "critical_route_unverified": item.severity == "critical" and not item.critical_route_capable,
                   "critical_delivery_unverified": item.severity == "critical",
                   "endpoint_delivery": "unknown",
                   "attempts": item.attempts, "error": item.error,
                   "local_notice_error": item.local_error}
                  for item in self._incidents.values() if item.active]
        return {"active": active, "pending": sum(not task.done() for task in self._tasks.values()),
                "critical_delivery_unverified": any(item["critical_delivery_unverified"] for item in active),
                "critical_route_unverified": any(item["critical_route_unverified"] for item in active),
                "normal_route_configured": self.settings.normal is not None,
                "critical_route_configured": self.settings.critical is not None,
                "delivery_evidence": "HA service acceptance does not prove endpoint delivery"}

    def _publish(self):
        try:
            self.notify()
        except Exception:
            _LOGGER.exception("Could not publish Home Climate notification diagnostics")

    def _notice_id(self, key):
        return f"home_climate_{self.entry_id}_{key}"

    @staticmethod
    def _status_key(kind, identity):
        # Stable IDs without collisions from replacing punctuation in entity IDs.
        return f"{kind}_{hashlib.sha256(str(identity).encode()).hexdigest()[:16]}"

    def _actionable_recovery_problems(self, handoff):
        """Give initial ownership settings one bounded retry interval to settle.

        Start before capability validation or dispatch, not at the first error.
        Persisted wall time bounds restored plans; a monotonic deadline prevents
        wall-clock changes or repeated status reports extending this grace.
        Safety/stop failures and subsequent attempts never receive this grace.
        """
        problems = handoff.get("problems") or {}
        if not handoff:
            self._recovery_identity = self._recovery_grace_until = None
            return problems
        if handoff.get("stage") != "applying":
            return problems
        identity = (handoff.get("applying_started"), handoff.get("stop_started"))
        if self._recovery_grace_until is None or identity != self._recovery_identity:
            self._recovery_identity = identity
            self._recovery_grace_until = self._clock() + HANDOFF_RETRY_SECONDS
        if handoff.get("interrupted") or not isinstance(problems, dict):
            return problems
        started = handoff.get("applying_started")
        if started is not None:
            if (isinstance(started, bool) or not isinstance(started, (int, float))
                    or not math.isfinite(started)):
                return problems
            age = time.time() - started
            if age < 0:
                return problems
            self._recovery_grace_until = min(
                self._recovery_grace_until,
                self._clock() + max(0, HANDOFF_RETRY_SECONDS - age))
        if self._clock() >= self._recovery_grace_until:
            return problems
        devices = set(handoff.get("heads") or ()) | set(handoff.get("fans") or ())
        requests = handoff.get("requests") or {}
        actionable = {}
        for entity, reason in problems.items():
            request = requests.get(entity)
            if entity not in devices:
                actionable[entity] = reason
                continue
            if request is None:
                continue  # Capability validation can fail before reservation.
            if not isinstance(request, dict):
                actionable[entity] = reason
                continue
            first_at = request.get("first_at", request.get("at"))
            attempts = request.get("attempts", 1)
            if (type(attempts) is not int or attempts != 1
                    or any(request.get(flag) for flag in (
                        "uncertain", "cancelled_before_dispatch", "operator_changed"))
                    or isinstance(first_at, bool) or not isinstance(first_at, (int, float))
                    or not math.isfinite(first_at)
                    or not 0 <= time.time() - first_at < HANDOFF_RETRY_SECONDS):
                actionable[entity] = reason
        return actionable

    def observe_status(self, status):
        """Route actionable controller evidence; ordinary timers remain quiet."""
        if self._closed:
            return
        conditions = {}
        handoff = status.get("handoff") or {}
        problems = self._actionable_recovery_problems(handoff)
        dispositions = status.get("handoff_notification_disposition") or {}
        if isinstance(problems, dict):
            problems = {entity: reason for entity, reason in problems.items()
                        if dispositions.get(entity) != "recovering"}
        if problems or handoff.get("stage") == "failed":
            detail = ("; ".join(f"{device}: {reason}" for device, reason in problems.items())
                      if isinstance(problems, dict) else str(problems))
            conditions["recovery_handoff"] = (
                "critical", "Home Climate recovery needs attention",
                "Recovery settings have not been confirmed for every affected device. " + detail)
        failed_heads = set()
        for entity, detail in (status.get("shared_stop") or {}).items():
            if isinstance(detail, dict) and detail.get("failed") and not detail.get("confirmed"):
                failed_heads.add(entity)
                conditions[self._status_key("head_stop", entity)] = (
                    "critical", "Home Climate head stop unconfirmed",
                    f"{entity}: {detail.get('reason') or 'Off was not confirmed after the bounded attempts'}. "
                    "Shared-system changeover remains blocked.")
        live = status.get("event") == "live" and status.get("control_enabled") is not False
        if status.get("event") in ("fault", "worker_failed"):
            self._status_fault = status.get("error") or status.get("last_error") or "Controller operation failed"
        elif live:
            self._status_fault = None
        if self._status_fault:
            conditions["controller_fault"] = (
                "normal", "Home Climate stopped after a fault", str(self._status_fault))
        if live:
            for entity, issue in (status.get("device_issues") or {}).items():
                if (entity not in failed_heads and issue
                        and (status.get("recovery_details") or {}).get(entity, {}).get(
                            "notification_disposition") != "recovering"):
                    conditions[self._status_key("device_issue", entity)] = (
                        "normal", "Home Climate device needs attention", f"{entity}: {issue}")
            # room_targets names every configured room in the current plan;
            # rooms contains only successfully qualified measurements.
            for room in set(status.get("room_targets") or {}) - set(status.get("rooms") or {}):
                conditions[self._status_key("room_source", room)] = (
                    "normal", "Home Climate room temperature unavailable",
                    f"{room}: no qualified room-temperature input is available; affected conditioning is inhibited.")
            for source, issue in (status.get("source_issues") or {}).items():
                if issue:
                    conditions[self._status_key("source_issue", source)] = (
                        "normal", "Home Climate temperature source needs attention", f"{source}: {issue}")
            seasonal = status.get("seasonal") or {}
            if seasonal.get("degraded") and (
                    seasonal.get("selected_role") == "fallback"
                    or seasonal.get("evidence_status") in ("unavailable", "unverified")):
                conditions["outdoor_source"] = (
                    "normal", "Home Climate outdoor source needs attention",
                    seasonal.get("reason") or "Local outdoor measurements are unavailable; seasonal operation is degraded.")
        current_keys = set(conditions)
        for key in self._status_keys - current_keys:
            self.update(key, False, "", "")
        self._status_keys = current_keys
        for key, (severity, title, message) in conditions.items():
            self.update(key, True, title, message, severity)

    def _local_notice(self, incident):
        try:
            from homeassistant.components import persistent_notification
            if not incident.active:
                persistent_notification.async_dismiss(self.hass, self._notice_id(incident.key))
                incident.notice_signature = None
                incident.local_error = None
                return
            message = incident.message
            if incident.severity == "critical" and not incident.critical_route_capable:
                message += ("\n\nCritical delivery is not verified. This condition requires urgent attention; "
                            "the configured normal route cannot be assumed to override device notification settings.")
            if incident.error:
                message += "\n\nNotification delivery: " + incident.error
            signature = (incident.title, message)
            if signature != incident.notice_signature:
                persistent_notification.async_create(
                    self.hass, message, title=incident.title,
                    notification_id=self._notice_id(incident.key))
                incident.notice_signature = signature
            incident.local_error = None
        except Exception:
            incident.local_error = "Could not update the local Home Climate notification"
            _LOGGER.exception(incident.local_error)

    def update(self, key, active, title, message, severity="normal"):
        """Queue a notification without delaying actuator or shutdown work."""
        if self._closed:
            return
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise ValueError("Alert key must be a stable lowercase incident identifier")
        if severity not in ("normal", "critical"):
            raise ValueError("Alert severity must be normal or critical")
        incident = self._incidents.get(key)
        if not active:
            if incident is not None and incident.active:
                incident.active = False
                incident.generation += 1
                incident.delivery_state = "resolved"
                self._local_notice(incident)
                self._publish()
            return
        is_new = incident is None or not incident.active
        escalated = bool(incident and incident.active and incident.severity != "critical" and severity == "critical")
        if is_new:
            incident = Incident(key, str(title)[:255], str(message)[:4096], severity,
                                generation=incident.generation + 1 if incident else 1)
            self._incidents[key] = incident
        else:
            incident.title, incident.message, incident.severity = str(title)[:255], str(message)[:4096], severity
            if escalated:
                incident.generation += 1
                incident.attempts = 0
                incident.delivery_state = "pending"
                incident.error = None
        _, incident.critical_route_capable = self.settings.route_for(severity)
        self._local_notice(incident)
        task = self._tasks.get(key)
        retry = (incident.delivery_state in {"service_unavailable", "target_unavailable"}
                 and incident.attempts < MAX_UNAVAILABLE_ATTEMPTS
                 and self._clock() >= incident.retry_at)
        if is_new or escalated or retry:
            # A queued old-severity job checks its generation before dispatch.
            # Already dispatched notification calls cannot be unsent.
            if task is None or task.done() or escalated or is_new:
                generation = incident.generation
                task = self.hass.async_create_task(self._send(incident, generation), eager_start=False)
                self._tasks[key] = task
                task.add_done_callback(self._task_finished)
        self._publish()

    def _task_finished(self, task):
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass
        self._publish()

    def _current(self, incident, generation):
        return (not self._closed and incident.active and incident.generation == generation
                and self._incidents.get(incident.key) is incident)

    async def _send(self, incident, generation):
        if not self._current(incident, generation):
            return
        route, critical = self.settings.route_for(incident.severity)
        incident.critical_route_capable = critical
        if route is None:
            incident.delivery_state = "not_configured"
            incident.error = "No applicable external notification route is configured; see the HA notification"
            self._local_notice(incident)
            self._publish()
            return
        domain, service = route.action.split(".", 1)
        incident.attempts += 1
        if not self.hass.services.has_service(domain, service):
            incident.delivery_state = "service_unavailable"
            incident.error = f"Configured notification action is unavailable: {route.action}"
            incident.retry_at = self._clock() + UNAVAILABLE_RETRY_SECONDS
            self._local_notice(incident)
            self._publish()
            return
        unavailable = [entity for entity in route.entity_ids
                       if (state := self.hass.states.get(entity)) is None or state.state == "unavailable"]
        if unavailable:
            incident.delivery_state = "target_unavailable"
            incident.error = "Configured notification target is missing or unavailable: " + ", ".join(unavailable)
            incident.retry_at = self._clock() + UNAVAILABLE_RETRY_SECONDS
            self._local_notice(incident)
            self._publish()
            return
        incident.delivery_state = "dispatching"
        payload = route.service_data(incident.message, incident.title)
        if incident.severity == "critical" and not critical:
            payload["message"] = "URGENT: " + payload["message"]
        job = self.hass.async_create_task(self.hass.services.async_call(
            domain, service, payload, blocking=True))
        self._service_tasks.add(job)

        def completed(task):
            self._service_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        job.add_done_callback(completed)
        error = None
        outcome = "service_accepted"
        try:
            done, _ = await asyncio.wait((job,), timeout=SERVICE_TIMEOUT_SECONDS)
            if job not in done:
                job.cancel()
                outcome, error = "unknown", "Service deadline expired; endpoint delivery is unknown"
            else:
                job.result()
        except asyncio.CancelledError:
            job.cancel()
            outcome, error = "unknown", "Notification task interrupted; endpoint delivery is unknown"
        except Exception as exc:
            outcome, error = "unknown", f"Notification service failed; endpoint delivery is unknown ({type(exc).__name__})"
        if self._current(incident, generation):
            incident.delivery_state, incident.error = outcome, error
            self._local_notice(incident)
            self._publish()

    async def async_close(self):
        self._closed = True
        for task in {*self._tasks.values(), *self._service_tasks}:
            if not task.done():
                task.cancel()
        # Cancellation of a notification call does not prove it was not sent.
        # Do not wait indefinitely for a provider that suppresses cancellation.
        for incident in self._incidents.values():
            if incident.active and incident.delivery_state in {"pending", "dispatching"}:
                incident.delivery_state = "unknown"
                incident.error = "Integration stopped; endpoint delivery is unknown"
        self._publish()
