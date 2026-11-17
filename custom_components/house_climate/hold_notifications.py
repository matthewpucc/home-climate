"""Durable, non-critical reminders for uninterrupted room Holds.

The comfort store owns episode age and the at-most-once dispatch claim. A
successful service call means HA accepted it, not that a phone displayed it.
This observer never changes room intent or controller/device state.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import logging
import re

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import entity_registry as er

from .controller.notifications import NotificationSettings
from .notifications import SERVICE_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)
RECONCILE_INTERVAL = timedelta(seconds=60)


class HoldNotificationManager:
    """Reconcile per-room local notices and one normal-route dispatch per Hold."""

    def __init__(self, hass, entry_id, comfort, settings=None):
        self.hass, self.entry_id, self.comfort = hass, entry_id, comfort
        self.settings = NotificationSettings.from_dict(settings)
        self._closed = False
        self._lock = asyncio.Lock()
        self._task = None
        self._dirty = False
        self._remove_timer = None
        self._remove_listener = None
        self._signatures = {}
        self._send_tasks = set()
        self.delivery = {}

    def _notice_id(self, room_id):
        identity = hashlib.sha256(room_id.encode()).hexdigest()[:24]
        return f"home_climate_{self.entry_id}_hold_{identity}"

    @callback
    def start(self):
        """Start after durable comfort state has loaded, including without a route."""
        if self._closed or self._remove_timer is not None:
            return
        self._remove_timer = async_track_time_interval(
            self.hass, self._timer_due, RECONCILE_INTERVAL)
        self._remove_listener = persistent_notification.async_register_callback(
            self.hass, self._notice_updated)
        self.request_reconcile()

    @callback
    def _timer_due(self, _now):
        self.request_reconcile()

    @callback
    def _notice_updated(self, kind, notifications):
        if kind != persistent_notification.UpdateType.REMOVED or self._closed:
            return
        removed = set(notifications) & set(self._signatures)
        for notice_id in removed:
            self._signatures.pop(notice_id, None)
        if removed:
            self.request_reconcile()

    @callback
    def request_reconcile(self):
        """Coalesce callbacks without losing an edit made while storage is pending."""
        if self._closed:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(
                self._run(), "Home Climate Hold reminders", eager_start=False)

    async def _run(self):
        while self._dirty and not self._closed:
            self._dirty = False
            try:
                await self.async_reconcile()
            except Exception:
                _LOGGER.exception("Could not reconcile Home Climate Hold reminders")

    def _current(self, room_id, started_at):
        if self._closed:
            return False
        episode = self.comfort.overdue_holds().get(room_id)
        return episode is not None and episode["started_at"] == started_at

    def _text(self, room_id):
        name = self.comfort.config.zones[room_id].name or room_id
        return (f"Home Climate: {name} is still on Hold",
                f"{name} has been on Hold for more than 24 hours. "
                "Its schedule is not being followed. Resume Schedule or choose "
                "another comfort mode to clear this reminder.")

    def _reconcile_local(self):
        # Snapshot/read/create/dismiss are all synchronous on HA's event loop.
        # No comfort edit can interleave between the check and the local action.
        overdue = self.comfort.overdue_holds()
        expected_ids = {self._notice_id(room_id) for room_id in overdue}
        # HA currently exposes no public getter. Keep this pinned compatibility
        # read narrow: remove only our exact entry-scoped hashed room IDs. This
        # also clears an old room notice after that room leaves configuration.
        own_id = re.compile(re.escape(f"home_climate_{self.entry_id}_hold_") + r"[0-9a-f]{24}")
        existing = persistent_notification._async_get_or_create_notifications(self.hass)
        for notice_id in tuple(existing):
            if own_id.fullmatch(notice_id) and notice_id not in expected_ids:
                self._signatures.pop(notice_id, None)
                persistent_notification.async_dismiss(self.hass, notice_id)
        for room_id in self.comfort.config.zones:
            notice_id = self._notice_id(room_id)
            if room_id not in overdue:
                self._signatures.pop(notice_id, None)
                persistent_notification.async_dismiss(self.hass, notice_id)
                self.delivery.pop(room_id, None)
                continue
            title, message = self._text(room_id)
            signature = (overdue[room_id]["started_at"], title, message)
            if self._signatures.get(notice_id) != signature:
                persistent_notification.async_create(
                    self.hass, message, title=title, notification_id=notice_id)
                self._signatures[notice_id] = signature
        return overdue

    def _route_ready(self):
        route = self.settings.normal  # Never consult or fall back to critical.
        # Hold reminders are non-critical. Provider payloads can encode urgency
        # without declaring critical_capable, so accept only payload-free normal
        # routes rather than guessing, stripping or rewriting provider options.
        if route is None or route.critical_capable or route.data:
            return None
        domain, service = route.action.split(".", 1)
        if not self.hass.services.has_service(domain, service):
            return None
        registry = er.async_get(self.hass)
        if any((entry := registry.async_get(entity)) is not None and entry.disabled
               for entity in route.entity_ids):
            return None
        if any((state := self.hass.states.get(entity)) is None
               or state.state in ("unavailable", "disabled") for entity in route.entity_ids):
            return None
        return route

    async def async_reconcile(self):
        async with self._lock:
            if self._closed:
                return
            overdue = self._reconcile_local()
            for room_id, episode in overdue.items():
                if episode["notification_claimed"]:
                    continue
                route = self._route_ready()
                if route is None:
                    continue  # No claim consumed: a later available route can send.
                started_at = episode["started_at"]
                try:
                    claimed = await self.comfort.async_claim_hold_notification(room_id, started_at)
                except Exception:
                    _LOGGER.exception("Could not persist Hold reminder dispatch claim for %s", room_id)
                    continue
                if not claimed or not self._current(room_id, started_at):
                    continue
                # Recheck route after storage yields. A lost route does not allow
                # replay of a durable claim, preserving at-most-once semantics.
                if self._route_ready() != route:
                    self.delivery[room_id] = "claimed_route_became_unavailable"
                    continue
                title, message = self._text(room_id)
                job = self.hass.async_create_task(
                    self._send(room_id, started_at, route, title, message),
                    "Home Climate Hold reminder delivery", eager_start=False)
                self._send_tasks.add(job)
                job.add_done_callback(self._send_tasks.discard)
            # A comfort edit could have completed while a claim was saved.
            if not self._closed:
                self._reconcile_local()

    async def _send(self, room_id, started_at, route, title, message):
        if not self._current(room_id, started_at):
            return
        if self._route_ready() != route:
            self.delivery[room_id] = "claimed_route_became_unavailable"
            return
        domain, service = route.action.split(".", 1)
        self.delivery[room_id] = "dispatching"
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT_SECONDS):
                await self.hass.services.async_call(
                    domain, service, route.service_data(message, title), blocking=True)
            outcome = "service_accepted_endpoint_delivery_unknown"
        except asyncio.CancelledError:
            if self._current(room_id, started_at):
                self.delivery[room_id] = "interrupted_endpoint_delivery_unknown"
            raise
        except Exception:
            outcome = "failed_or_timed_out_endpoint_delivery_unknown"
            _LOGGER.warning("Hold reminder service result uncertain for %s; not retrying", room_id)
        if self._current(room_id, started_at):
            self.delivery[room_id] = outcome

    async def async_close(self):
        self._closed = True
        for remove in (self._remove_timer, self._remove_listener):
            if remove is not None:
                remove()
        self._remove_timer = self._remove_listener = None
        for task in (self._task, *self._send_tasks):
            if task is not None and not task.done():
                task.cancel()
        # Reconciliation may be draining a shielded, verified claim write. Its
        # completion is part of closing, so no old write can outlive this owner.
        # Do not await provider jobs: third-party services may suppress cancel.
        if self._task is not None and self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)
        # Keep local notices: unloading does not resolve a durable Hold.
