"""Persist dashboard comfort holds independently of controller ownership.

HA's loop serializes edits and persistence. The worker only reads an immutable
snapshot; it never writes storage or schedules callbacks.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from types import MappingProxyType

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store

from .controller.comfort import next_schedule_change
from .controller.config import Settings

_LOGGER = logging.getLogger(__name__)


class _VerifiedComfortStore(Store):
    """HA Store logs write errors; verify disk before publishing durable intent.

    Read the serialized file directly, never Store's pending/preload cache.
    Atomic replacement retains the previous envelope when a write fails.
    Cancellation drains the write/verification before the caller releases its
    room-edit lock, so an old executor write cannot land after a newer edit.
    """

    def _read_persisted(self):
        with open(self.path, encoding="utf-8") as stored:
            return json.load(stored)

    async def _save_verified(self, data):
        await super().async_save(data)
        persisted = await self.hass.async_add_executor_job(self._read_persisted)
        if (not isinstance(persisted, dict) or persisted.get("key") != self.key
                or persisted.get("version") != self.version
                or persisted.get("data") != data):
            raise OSError("Home Climate comfort save was not confirmed on disk")

    async def async_save(self, data):
        task = self.hass.async_create_task(
            self._save_verified(data), "Home Climate verified comfort save", eager_start=False)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        # Retrieve failure even if the caller was cancelled; never publish or
        # dispatch unless the normal await returns successfully.
        task.result()
        if cancelled:
            raise asyncio.CancelledError


async def async_migrate_house_holds(hass, entry_id, previous_config, fixed_config, *, now=None):
    """Preserve genuine dashboard holds while adopting the fixed house policy.

    A configuration fingerprint is intentionally strict during normal startup.
    This one-time entry migration checks the old fingerprint before replacing it,
    retains only still-valid user temperatures. Temporary overrides never extend
    beyond their original expiration or the next new schedule boundary; explicit
    persistent holds keep their non-expiring intent.
    """
    previous = RoomComfort(hass, previous_config, entry_id, lambda: None)
    fixed = RoomComfort(hass, fixed_config, entry_id, lambda: None)
    data = await fixed.store.async_load()
    if not isinstance(data, dict):
        return
    now = now or RoomComfort._now()
    rooms = data.get("rooms", {})
    if not isinstance(rooms, dict):
        return
    migrated = {}
    for room_id, value in rooms.items():
        try:
            if (not isinstance(value, dict) or room_id not in previous_config.zones
                    or room_id not in fixed_config.zones):
                continue
            old_fingerprint = previous._fingerprint(room_id)
            new_fingerprint = fixed._fingerprint(room_id)
            if value["fingerprint"] not in (old_fingerprint, new_fingerprint):
                continue
            low, high, target = fixed._validate(
                room_id, value["low_f"], value["high_f"], value["target_f"])
            mode = RoomComfort._stored_mode(value)
            expiry = value["expires_at"]
            if expiry is not None:
                expiry = datetime.fromisoformat(expiry)
                if expiry.tzinfo is None or expiry <= now:
                    continue
                expiry = expiry.astimezone(timezone.utc)
            settings, _ = fixed._base(room_id, now)
            next_change = next_schedule_change(
                fixed._room(room_id), settings, now, fixed._timezone)
            if mode == "temporary" and next_change is not None:
                expiry = min(expiry, next_change) if expiry is not None else next_change
            started, claimed = RoomComfort._hold_metadata(value, mode, now)
            migrated[room_id] = _Hold(low, high, target, expiry, new_fingerprint, mode, started, claimed)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    result = RoomComfort._data(migrated)
    if result != data:
        # Commit first. If storage fails, entry migration must not publish new
        # fingerprints and silently lose an otherwise valid dashboard hold.
        await fixed.store.async_save(result)


@dataclass(frozen=True)
class _Hold:
    low_f: float
    high_f: float
    target_f: float
    expires_at: datetime | None
    fingerprint: str
    mode: str = "temporary"
    started_at: datetime | None = None
    notification_claimed: bool = False


class RoomComfort:
    """Durable room targets and explicit persistent Protection Only intent."""

    def __init__(self, hass, config, entry_id, notify, wake_event=None):
        self.hass, self.config, self.notify = hass, config, notify
        self.wake_event = wake_event
        self.store = _VerifiedComfortStore(
            hass, 1, f"house_climate.{entry_id}.comfort", atomic_writes=True)
        self._lock = asyncio.Lock()
        self._snapshot = MappingProxyType({})
        self._remove_expiry = None
        self._expiry_task = None
        self._closed = False
        self._seasonal_profile = config.seasonal_policy.initial_profile

    @callback
    def set_seasonal_profile(self, profile):
        """Reflect the planner's qualified seasonal profile on room cards."""
        if profile not in ("cool", "warm") or profile == self._seasonal_profile:
            return
        self._seasonal_profile = profile
        self.notify()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @property
    def _timezone(self):
        return self.hass.config.time_zone

    def _room(self, room_id):
        try:
            return self.config.zones[room_id]
        except (KeyError, TypeError):
            raise ValueError("Unknown configured room") from None

    def bounds(self, room_id):
        """Room comfort limits are independent of the auxiliary thermostat."""
        self._room(room_id)
        return self.config.low_f, self.config.high_f

    def _fingerprint(self, room_id, config=None):
        config = config or self.config
        room = config.zones[room_id]
        data = {
            "room": asdict(room), "head": config.heads[room.head],
            "timezone": self._timezone, "limits": [config.low_f, config.high_f],
            "auxiliary_cutoff": config.heater_cutoff_f if room_id == config.auxiliary_room else None,
        }
        # Contact wiring does not change the room's thermostat hold or identity.
        # Preserve fingerprints written before optional windows existed, too.
        data["room"].pop("windows", None)
        if not data["room"].get("seasonal_profiles"):
            data["room"].pop("seasonal_profiles", None)
        for period in data["room"]["schedules"]:
            if tuple(period.get("weekdays", ())) == tuple(range(7)):
                period.pop("weekdays", None)
            if not period.get("all_day"):
                period.pop("all_day", None)
            if not period.get("seasonal_profiles"):
                period.pop("seasonal_profiles", None)
        # Preserve fingerprints stored before report-on-change probes existed.
        for probe in data["room"]["probes"]:
            if not probe.get("report_on_change", False):
                probe.pop("report_on_change", None)
            for name in ("observation_time_entity", "require_active_entity"):
                if probe.get(name) is None:
                    probe.pop(name, None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _states(self):
        # Only legacy profile helpers affect comfort settings. StateMachine.get
        # is the same cached accessor used by the native controller bridge.
        if not self.config.legacy:
            return {}
        from .controller.legacy import NUMBERS, TIMES, TOGGLES, helper
        result = {}
        for domain, names in (("input_number", NUMBERS), ("input_datetime", TIMES),
                              ("input_boolean", TOGGLES)):
            for name in names:
                entity = helper(name, domain)
                state = self.hass.states.get(entity)
                if state is not None:
                    result[entity] = {"state": state.state, "attributes": dict(state.attributes)}
        return result

    def _base(self, room_id, now):
        room = self._room(room_id)
        settings = Settings.read(self._states(), self.config)
        settings.seasonal_profile = self._seasonal_profile
        return settings, settings.profile(room, now, self._timezone)

    def _active(self, room_id, now, config=None):
        hold = self._snapshot.get(room_id)
        config = config or self.config
        if (hold is None or room_id not in config.zones
                or hold.fingerprint != self._fingerprint(room_id, config)
                or (hold.expires_at is not None and now >= hold.expires_at)):
            return None
        return hold

    def effective(self, room_id, now=None):
        """Resolve the dashboard state without changing schedule or ownership."""
        now = now or self._now()
        settings, profile = self._base(room_id, now)
        hold = self._active(room_id, now)
        if hold:
            low, high, target = hold.low_f, hold.high_f, hold.target_f
        else:
            target = settings.value(profile.target_f) + profile.target_offset_f
            if profile.comfort_enabled:
                half = settings.value(profile.half_band_f)
                low = max(self.config.low_f, settings.value(profile.low_f)
                          if profile.low_f is not None else target - half)
                high = min(self.config.high_f, settings.value(profile.high_f)
                           if profile.high_f is not None else target + half)
            else:
                # The card presents the saved/proposed comfort request while
                # inactive. Policy still enforces its full system safety range.
                half = settings.value(profile.half_band_f)
                low = settings.value(profile.low_f) if profile.low_f is not None else target - half
                high = settings.value(profile.high_f) if profile.high_f is not None else target + half
                minimum, maximum = self.bounds(room_id)
                width = min(high - low, maximum - minimum)
                low = min(max(low, minimum), maximum - width)
                high = low + width
                target = min(high, max(low, target))
        return {
            "low_f": low, "high_f": high, "target_f": target,
            "comfort_enabled": (hold.mode != "protection_only" if hold else profile.comfort_enabled),
            "hold": hold is not None,
            "mode": hold.mode if hold else "schedule",
            "expires_at": hold.expires_at.isoformat() if hold and hold.expires_at else None,
        }

    def settings(self, states, config, now, strict=False):
        """Worker-side provider; expired holds are ignored even if HA is busy."""
        settings = Settings.read(states, config, strict=strict)
        settings.seasonal_profile = self._seasonal_profile
        profiles = {}
        protection_only = set()
        for room_id, room in config.zones.items():
            hold = self._active(room_id, now, config)
            if hold is None:
                continue
            base = settings.profile(room, now, self._timezone)
            if hold.mode == "protection_only":
                protection_only.add(room_id)
            profiles[room_id] = replace(
                base, low_f=hold.low_f, high_f=hold.high_f, target_f=hold.target_f,
                target_offset_f=0, comfort_enabled=hold.mode != "protection_only",
                half_band_f=settings.value(base.half_band_f),
                fan_percentage=settings.value(base.fan_percentage),
            )
        return replace(settings, room_profiles=profiles, protection_only_rooms=frozenset(protection_only))

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("Temperature must be a finite number")
        return float(value)

    def _validate(self, room_id, low, high, target):
        low, high, target = (self._number(value) for value in (low, high, target))
        minimum, maximum = self.bounds(room_id)
        if not minimum <= low < high <= maximum:
            raise ValueError(f"Comfort range must be within {minimum:g}–{maximum:g}°F, with low below high")
        if not low <= target <= high:
            raise ValueError("Desired temperature must be within the comfort range")
        return low, high, target

    @staticmethod
    def _stored_mode(value):
        # Legacy rooms without schedules already have expiry=None. Never infer
        # a new persistent Hold from that old Temporary representation.
        mode = value.get("mode", "temporary")
        if mode not in ("temporary", "hold", "protection_only"):
            raise ValueError("Unknown room override mode")
        if mode in ("hold", "protection_only") and value["expires_at"] is not None:
            raise ValueError("Persistent room intent cannot have an expiry")
        return mode

    @staticmethod
    def _hold_metadata(value, mode, now):
        """Legacy/invalid ages start now; never invent an already-overdue Hold."""
        if mode != "hold":
            return None, False
        try:
            started = datetime.fromisoformat(value.get("started_at"))
            if started.tzinfo is None or started > now:
                raise ValueError("Unreliable Hold start")
            started = started.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            started = now
        return started, value.get("notification_claimed") is True

    def overdue_holds(self, now=None):
        """Immutable episode metadata; no actuator operations or state changes."""
        now = now or self._now()
        return {room: {"started_at": hold.started_at,
                       "notification_claimed": hold.notification_claimed}
                for room, hold in self._snapshot.items()
                if self._active(room, now) is hold and hold.mode == "hold"
                and hold.started_at is not None
                and now - hold.started_at > timedelta(hours=24)}

    async def async_claim_hold_notification(self, room_id, episode_started_at):
        """Save an at-most-once dispatch claim before any external service call."""
        async with self._lock:
            if self._closed:
                return False
            metadata = self.overdue_holds().get(room_id)
            if (metadata is None or metadata["started_at"] != episode_started_at
                    or metadata["notification_claimed"]):
                return False
            hold = self._snapshot[room_id]
            # A claim changes no thermal intent and must not wake the controller.
            holds = {**self._snapshot, room_id: replace(hold, notification_claimed=True)}
            await self.store.async_save(self._data(holds))
            self._snapshot = MappingProxyType(holds)
            return True

    def _new_hold(self, room_id, low, high, target, now, *, mode=None):
        low, high, target = self._validate(room_id, low, high, target)
        if mode is None:
            active = self._active(room_id, now)
            mode = active.mode if active else "temporary"
        if mode not in ("temporary", "hold", "protection_only"):
            raise ValueError("Choose Temporary, Hold, or Protection Only")
        settings, _ = self._base(room_id, now)
        expiry = (next_schedule_change(self._room(room_id), settings, now, self._timezone)
                  if mode == "temporary" else None)
        active = self._active(room_id, now)
        continuing = mode == "hold" and active is not None and active.mode == "hold"
        started = (active.started_at if continuing else now) if mode == "hold" else None
        claimed = active.notification_claimed if continuing else False
        return _Hold(low, high, target, expiry, self._fingerprint(room_id), mode, started, claimed)

    @staticmethod
    def _data(holds):
        rooms = {}
        for room, hold in holds.items():
            value = asdict(hold)
            value["expires_at"] = hold.expires_at.isoformat() if hold.expires_at else None
            if hold.mode == "hold":
                value["started_at"] = hold.started_at.isoformat() if hold.started_at else None
            else:
                # Leave historical Temporary/Protection Only records unchanged.
                value.pop("started_at")
                value.pop("notification_claimed")
            rooms[room] = value
        return {"rooms": rooms}

    def _notify(self):
        if self.wake_event is not None:
            self.wake_event.set()
        self.notify()

    async def _commit(self, holds):
        # A failed save must not publish an edit the next startup will forget.
        await self.store.async_save(self._data(holds))
        self._snapshot = MappingProxyType(dict(holds))
        self._schedule_expiry()
        self._notify()

    def _check_open(self):
        if self._closed:
            raise ValueError("Room comfort controls are stopping")

    async def async_load(self):
        async with self._lock:
            self._check_open()
            data = await self.store.async_load()
            now, holds = self._now(), {}
            rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
            if isinstance(rooms, dict):
                for room_id, value in rooms.items():
                    try:
                        if not isinstance(value, dict) or room_id not in self.config.zones:
                            continue
                        low, high, target = self._validate(
                            room_id, value["low_f"], value["high_f"], value["target_f"])
                        mode = self._stored_mode(value)
                        expiry = value["expires_at"]
                        if expiry is not None:
                            expiry = datetime.fromisoformat(expiry)
                            if expiry.tzinfo is None or expiry <= now:
                                continue
                            expiry = expiry.astimezone(timezone.utc)
                        settings, _ = self._base(room_id, now)
                        next_change = next_schedule_change(
                            self._room(room_id), settings, now, self._timezone)
                        # A restored hold must not survive a boundary introduced
                        # by changed legacy time helpers.
                        if mode == "temporary" and (next_change is None) != (expiry is None):
                            continue
                        if mode == "temporary" and next_change is not None and expiry > next_change:
                            continue
                        fingerprint = self._fingerprint(room_id)
                        if value["fingerprint"] != fingerprint:
                            continue
                        started, claimed = self._hold_metadata(value, mode, now)
                        holds[room_id] = _Hold(low, high, target, expiry, fingerprint, mode, started, claimed)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
            if data is not None and data != self._data(holds):
                await self.store.async_save(self._data(holds))
            self._snapshot = MappingProxyType(holds)
            self._schedule_expiry()
            self._notify()

    def _check_target_edit(self, room_id, now):
        active = self._active(room_id, now)
        if active and active.mode == "protection_only":
            raise ValueError("Resume Schedule or choose Hold before changing a Protection Only room target")

    async def async_set_protection_only(self, room_id):
        """Suspend ordinary room conditioning, retaining native thermal protection."""
        async with self._lock:
            self._check_open()
            now = self._now()
            current = self.effective(room_id, now)
            hold = self._new_hold(room_id, current["low_f"], current["high_f"],
                                  current["target_f"], now, mode="protection_only")
            await self._commit({**self._snapshot, room_id: hold})

    async def async_set_range(self, room_id, low_f, high_f):
        async with self._lock:
            self._check_open()
            now = self._now()
            self._check_target_edit(room_id, now)
            low, high = self._number(low_f), self._number(high_f)
            current = self.effective(room_id, now)
            target = min(high, max(low, current["target_f"]))
            hold = self._new_hold(room_id, low, high, target, now)
            await self._commit({**self._snapshot, room_id: hold})

    def _shifted_band(self, room_id, target, now):
        current = self.effective(room_id, now)
        if current["comfort_enabled"]:
            delta = target - current["target_f"]
            low, high = current["low_f"] + delta, current["high_f"] + delta
        else:
            settings, profile = self._base(room_id, now)
            half = settings.value(profile.half_band_f)
            low, high = target - half, target + half
        minimum, maximum = self.bounds(room_id)
        width = min(high - low, maximum - minimum)
        low = min(max(low, minimum), maximum - width)
        return low, low + width

    async def async_set_target(self, room_id, target_f, *, persistent=False):
        async with self._lock:
            self._check_open()
            target, now = self._number(target_f), self._now()
            self._check_target_edit(room_id, now)
            minimum, maximum = self.bounds(room_id)
            if not minimum <= target <= maximum:
                raise ValueError(f"Desired temperature must be within {minimum:g}–{maximum:g}°F")
            low, high = self._shifted_band(room_id, target, now)
            hold = self._new_hold(room_id, low, high, target, now,
                                  mode="hold" if persistent else None)
            await self._commit({**self._snapshot, room_id: hold})

    async def async_hold(self, room_id, *, persistent=False):
        async with self._lock:
            self._check_open()
            now = self._now()
            current = self.effective(room_id, now)
            target = current["target_f"]
            minimum, maximum = self.bounds(room_id)
            target = min(maximum, max(minimum, target))
            low, high = ((current["low_f"], current["high_f"]) if persistent
                         else self._shifted_band(room_id, target, now))
            hold = self._new_hold(room_id, low, high, target, now,
                                  mode="hold" if persistent else "temporary")
            await self._commit({**self._snapshot, room_id: hold})

    async def async_resume(self, room_id):
        async with self._lock:
            self._check_open()
            self._room(room_id)
            if room_id not in self._snapshot:
                return
            await self._commit({key: value for key, value in self._snapshot.items() if key != room_id})

    def _schedule_expiry(self):
        if self._remove_expiry:
            self._remove_expiry()
            self._remove_expiry = None
        if self._closed:
            return
        now = self._now()
        times = [hold.expires_at for hold in self._snapshot.values() if hold.expires_at is not None]
        if times:
            # A previous persistence failure may leave an already-expired entry.
            when = min(times)
            if when <= now:
                when = now + timedelta(seconds=30)
            self._remove_expiry = async_track_point_in_utc_time(self.hass, self._expiry_due, when)

    @callback
    def _expiry_due(self, _now):
        self._remove_expiry = None
        if not self._closed:
            self._expiry_task = self.hass.async_create_task(self._async_expire(), "Home Climate comfort expiry")

    async def _async_expire(self):
        async with self._lock:
            if self._closed:
                return
            now = self._now()
            holds = {room: hold for room, hold in self._snapshot.items()
                     if self._active(room, now) is not None}
            if len(holds) != len(self._snapshot):
                try:
                    await self._commit(holds)
                    return
                except Exception:
                    # Read paths already exclude expired intent. Retry durable
                    # cleanup without bringing expired controls back into effect.
                    _LOGGER.exception("Could not save expired room comfort holds")
                    self._notify()
            self._schedule_expiry()

    async def async_close(self):
        async with self._lock:
            self._closed = True
            if self._remove_expiry:
                self._remove_expiry()
                self._remove_expiry = None
        if self._expiry_task:
            await self._expiry_task
