"""Time boundaries for temporary room comfort overrides.

A hold follows the existing room schedule. It ends when the selected period
changes, including a transition caused by a daylight-saving clock change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from .config import Settings, Zone


def _period_index(room: Zone, settings: Settings, instant: datetime, zone: ZoneInfo):
    local = instant.astimezone(zone).time()
    return next((index for index, period in enumerate(room.schedules)
                 if settings.active(period, local)), None)


def next_schedule_change(room: Zone, settings: Settings, now: datetime,
                         timezone: str) -> datetime | None:
    """Return the next change of scheduled period as a UTC instant.

    Local start/end times can occur twice in autumn or not at all in spring.
    Include both real occurrences and actual UTC offset changes, then compare
    the selected period immediately before and after each candidate. This also
    catches a clock jump into or out of a period without crossing its nominal
    wall-clock boundary. Equal-valued adjacent periods remain distinct periods.
    A room without schedules has no automatic hold expiry.
    """
    if now.utcoffset() is None:
        raise ValueError("Schedule calculations require a timezone-aware instant")
    if not room.schedules:
        return None

    zone = ZoneInfo(timezone)
    utc_now = now.astimezone(dt_timezone.utc)
    horizon = utc_now + timedelta(days=3)
    candidates: set[datetime] = set()
    local_date = utc_now.astimezone(zone).date()
    for day in range(-1, 5):
        date = local_date + timedelta(days=day)
        for period in room.schedules:
            for boundary in (settings.clock(period.start), settings.clock(period.end)):
                naive = datetime.combine(date, boundary)
                for fold in (0, 1):
                    instant = naive.replace(tzinfo=zone, fold=fold).astimezone(dt_timezone.utc)
                    # Reject imaginary spring times; the offset-jump candidate
                    # below represents their actual effect on the active profile.
                    if instant.astimezone(zone).replace(tzinfo=None) == naive:
                        candidates.add(instant)

    # IANA offset changes occur at whole seconds. Locate any transition in the
    # short horizon without assuming a one-hour DST change or a fixed local hour.
    cursor = utc_now.replace(microsecond=0)
    offset = cursor.astimezone(zone).utcoffset()
    while cursor < horizon:
        following = min(cursor + timedelta(hours=1), horizon)
        next_offset = following.astimezone(zone).utcoffset()
        if next_offset != offset:
            low, high = int(cursor.timestamp()), int(following.timestamp())
            while high - low > 1:
                middle = (low + high) // 2
                instant = datetime.fromtimestamp(middle, dt_timezone.utc)
                if instant.astimezone(zone).utcoffset() == offset:
                    low = middle
                else:
                    high = middle
            candidates.add(datetime.fromtimestamp(high, dt_timezone.utc))
        cursor, offset = following, next_offset

    for candidate in sorted(candidates):
        if not utc_now < candidate <= horizon:
            continue
        before = candidate - timedelta(microseconds=1)
        if _period_index(room, settings, before, zone) != _period_index(room, settings, candidate, zone):
            return candidate
    return None
