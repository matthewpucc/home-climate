"""Conservative shared-compressor evidence for unowned native heads."""
from datetime import datetime

SCOPE_FIELD = "native_manual_head_scope"
DIRECTIONS = frozenset({"heat", "cool"})
MAX_REPORT_AGE = 90


def read_scope(config, states):
    return states.get(config.entity("status", "input_text"), {}).get("attributes", {}).get(SCOPE_FIELD, {})


def trusted_mode(state, now, boundary):
    """Never substitute last_changed/last_updated or infer Off from idle."""
    if not state or boundary is None:
        return None
    attrs = state.get("attributes") or {}
    mode = state.get("state")
    action = attrs.get("hvac_action")
    if (attrs.get("assumed_state") or attrs.get("restored")
            or (state.get("integration_repair") or {}).get("held")
            or mode not in {"off", "heat", "cool"}
            or action not in (None, "off", "idle", {"heat": "heating", "cool": "cooling"}.get(mode))):
        return None
    try:
        report = datetime.fromisoformat(state["last_reported"])
        if report.tzinfo is None:
            return None
        stamp = report.timestamp()
        return mode if (stamp > boundary and 0 <= now - stamp <= MAX_REPORT_AGE) else None
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def manual_heads_off(scope, states, now):
    return all(trusted_mode(states.get(entity), now, boundary) == "off"
               for entity, boundary in scope.items())


def permits_direction(scope, states, now, requested, *, continuing):
    """A managed system may continue only compatible output; starts require Off."""
    if not scope:
        return True
    if requested not in DIRECTIONS:
        return False
    allowed = {"off", requested} if continuing else {"off"}
    return all(trusted_mode(states.get(entity), now, boundary) in allowed
               for entity, boundary in scope.items())
