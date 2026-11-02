"""Explicit device feedback, independent of HA cache-write timestamps.

Only a source adapter that actually reads/subscribes to the physical attribute
may supply ``device_report``. An optimistic command, HA update_entity, template
timestamp, or an unrelated sensor update must never create/renew this record.
Unsupported sources remain unknown; this module never polls or operates devices.
"""
from datetime import datetime
import math


def device_feedback(state, now, *, maximum_age=180, after=None):
    """Return (value, physical_report_time), or (None, None).

    The snapshot stores its own value as well as its acquisition time. This
    prevents a later optimistic entity update from borrowing an older report.
    Repeated real On reports renew evidence without requiring an On transition.
    """
    if state is None:
        return None, None
    value = state.get("state") if isinstance(state, dict) else state.state
    attrs = state.get("attributes", {}) if isinstance(state, dict) else state.attributes
    report = attrs.get("device_report")
    if (value not in ("on", "off") or attrs.get("restored") or attrs.get("assumed_state")
            or not isinstance(report, dict) or report.get("state") != value):
        return None, None
    try:
        stamp = datetime.fromisoformat(report["reported_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timezone required")
        seconds = stamp.timestamp()
        if (not math.isfinite(seconds) or not -5 <= now - seconds <= maximum_age
                or (after is not None and seconds <= after)):
            raise ValueError("Device report expired or predates the operation")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None, None
    return value, seconds
