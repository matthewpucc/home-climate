"""Numeric projections of captured evidence. No safety getters or control calls.

Codes are a public, bounded vocabulary. Unknown never becomes zero. Configured
fan/source identities select entities; reasons and timestamps never select labels.
"""
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite


GUARD_CODES = {"ready": 0, "preview": 1, "manual": 2, "persistence": 3,
               "identity": 4, "manual_recovery": 5, "emergency_ac": 6,
               "shutdown_fault": 7, "restoring": 8, "latched": 9, "blocked": 10,
               "battery_reserve": 11, "supply_disabled": 12}
AIRFLOW_CODES = {"minimum": 0, "reduce": 1, "outlet": 2, "angles": 3,
                 "verified": 4, "retry": 5, "unsupported": 6, "deferred": 7}
SOURCE_CODES = {"qualified": 0, "change_only_unverified": 1, "not_accepted": 2,
                "snapshot_expired": 3, "report_expired": 4, "future": 5}
UNKNOWN_CODE = 99
GUARD_FIELDS = ("ready", "blocked", "reason_code", "reserve_percent", "sampled_at")
FAN_FIELDS = ("requested_percent", "reported_percent", "reported_at", "speed_acknowledged",
              "speed_pending", "speed_failed", "requested_lower_angle", "requested_upper_angle",
              "reported_lower_angle", "reported_upper_angle", "airflow_acknowledged",
              "airflow_pending", "airflow_phase_code")
SOURCE_FIELDS = ("reported_at", "measurement_at", "qualified", "reason_code")


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def timestamp(value):
    if number(value) is not None:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def fresh(stamp, now, age):
    stamp = timestamp(stamp)
    return stamp is not None and 0 <= now - stamp <= age


def flag(value):
    return int(value) if isinstance(value, bool) else None


def bounded(value, low, high):
    value = number(value)
    return value if value is not None and low <= value <= high else None


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    scope: str
    identity: str
    field: str
    unit: str | None = None


def source_id(room, source):
    # Unambiguous, stable identity even when configured names contain separators.
    return sha256(json.dumps([room, source]).encode()).hexdigest()[:16]


def sources(config):
    result = {}
    for room, zone in config.zones.items():
        for probe in zone.probes:
            source = probe.source.strip()
            result[source_id(room, source)] = (room, source)
    for probe in config.outdoor_probes:
        result[source_id(None, probe.source)] = (None, probe.source)
    return result


def fan_reports(config, states):
    """Copy only bounded settings evidence, excluding registry/provider identity."""
    result = {}
    for entity in config.fans.values():
        state = states.get(entity, {})
        attrs = state.get("attributes") or {}
        result[entity] = {"state": state.get("state"),
                          "last_reported": state.get("last_reported") or state.get("last_updated"),
                          "attributes": {key: attrs[key] for key in (
                              "percentage", "angle_low", "angle_high", "restored", "assumed_state") if key in attrs}}
    return result


def metrics(config, *, controls=False, guard=False):
    result = []

    def add(scope, identity, label, fields):
        for field in fields:
            unit = ("s" if field.endswith("_at") else "%" if field.endswith("_percent")
                    else "°" if field.endswith("_angle") else None)
            result.append(Metric(f"{scope}_{identity + '_' if identity else ''}{field}",
                                 f"{label} {field.replace('_', ' ')}", scope, identity, field, unit))

    if guard:
        add("guard", "", "Guard", GUARD_FIELDS)
    if controls and config.heater_entity:
        add("auxiliary", "", "Auxiliary", ("manual_owned",))
    for fan in config.fan_controls.values():
        add("fan", fan.entity_id, fan.entity_id.split(".", 1)[-1].replace("_", " "), FAN_FIELDS)
    for identity, (room, source) in sources(config).items():
        add("source", identity, f"{room or 'Outdoor'} source {source}", SOURCE_FIELDS)
    return result


def guard_values(observations, now):
    current = observations.guard_current
    values = {"sampled_at": observations.sampled_at,
              "reserve_percent": number(current.get("battery_reserve_percent"))}
    if (not fresh(observations.sampled_at, now, 30)
            or not fresh(observations.guard_evaluated_at, now, 30)):
        return values
    blocked = flag(current.get("blocked"))
    live, manual = flag(current.get("live")), flag(current.get("manual_override"))
    values.update(blocked=blocked, ready=(int(live == 1 and manual == 0 and blocked == 0)
                                         if None not in (blocked, live, manual) else None))
    if None in (blocked, live, manual):
        return values
    battery, reserve = number(current.get("battery_percent")), values["reserve_percent"]
    # Priority is diagnostic categorization, never a replacement admission gate.
    reason = next((name for name, active in (
        ("persistence", current.get("persistence_error")),
        ("identity", current.get("binding_identity_mismatch")),
        ("emergency_ac", current.get("emergency_ac_latched")),
        ("shutdown_fault", current.get("shutdown_fault") or current.get("shutdown_contradicted")),
        ("manual_recovery", current.get("manual_recovery_required")),
        ("manual", manual), ("preview", not live), ("restoring", current.get("restoring")),
        ("battery_reserve", blocked and battery is not None and reserve is not None and battery <= reserve),
        ("supply_disabled", blocked and current.get("ac_output_enabled") is False),
        ("latched", current.get("latched")), ("blocked", blocked)) if active), "ready")
    values["reason_code"] = GUARD_CODES[reason]
    return values


def fan_values(status, entity, now, snapshot_age):
    values = {}
    snapshot_fresh = fresh(status.get("timestamp"), now, snapshot_age)
    report = status.get("fan_reports", {}).get(entity, {})
    attrs = report.get("attributes") or {}
    stamp = timestamp(report.get("last_reported") or report.get("last_updated"))
    values["reported_at"] = stamp
    trusted = (snapshot_fresh and fresh(stamp, now, 180) and report.get("state") in ("on", "off")
               and not attrs.get("restored") and not attrs.get("assumed_state"))
    if trusted:
        # Off without percentage/angles does not fabricate numeric telemetry.
        values.update(reported_percent=bounded(attrs.get("percentage"), 0, 100),
                      reported_lower_angle=bounded(attrs.get("angle_low"), 0, 360),
                      reported_upper_angle=bounded(attrs.get("angle_high"), 0, 360))
    if not snapshot_fresh or status.get("event") != "live":
        return values
    speed = status.get("fan_observations", {}).get(entity, {})
    if fresh(speed.get("sampled_at"), now, snapshot_age):
        values.update(requested_percent=bounded(speed.get("requested_percentage"), 0, 100),
                      speed_pending=flag(speed.get("pending")), speed_failed=flag(speed.get("failed")))
        if trusted:
            values["speed_acknowledged"] = flag(speed.get("acknowledged"))
    intent = status.get("fan_airflow", {}).get(entity, {})
    requested = intent.get("requested") or {}
    execution = intent.get("execution") or {}
    phase = execution.get("phase")
    if phase is not None:
        values.update(airflow_phase_code=AIRFLOW_CODES.get(phase, UNKNOWN_CODE),
                      airflow_pending=(int(phase in ("reduce", "outlet", "angles", "retry", "deferred"))
                                       if phase in AIRFLOW_CODES else None))
    values.update(requested_lower_angle=bounded(requested.get("lower_angle"), 0, 360),
                  requested_upper_angle=bounded(requested.get("upper_angle"), 0, 360))
    # Minimum has no requested profile; unsupported/deferred is not a negative
    # acknowledgement. A verified phase is only an accepted HA settings match.
    if trusted and phase in ("reduce", "outlet", "angles", "verified", "retry"):
        values["airflow_acknowledged"] = int(phase == "verified")
    return values


def source_values(status, room, source, config, now, snapshot_age):
    if room is None:
        observation = status.get("outdoor_source_evidence") or {}
        sampled_at = observation.get("sampled_at")
        evidence = observation.get("sources", ())
    else:
        sampled_at = status.get("timestamp")
        evidence = status.get("rooms", {}).get(room, {}).get("source_evidence", ())
    sample = next((row for row in evidence if row.get("source") == source), {})
    reported, measurement = timestamp(sample.get("reported_at")), timestamp(sample.get("measurement_at"))
    values = {"reported_at": reported, "measurement_at": measurement}
    if not fresh(sampled_at, now, snapshot_age):
        if sampled_at is not None:
            values["reason_code"] = SOURCE_CODES["snapshot_expired"]
        return values
    reason = "not_accepted"
    if sample:
        if any(stamp is not None and stamp > now for stamp in (reported, measurement)):
            reason = "future"
        elif room is None:
            expiry = number(sample.get("valid_until"))
            reason = "qualified" if expiry is not None and now < expiry else "report_expired"
        elif (not fresh(reported, now, config.sensor_stale_seconds)
              or measurement is not None and not fresh(measurement, now, config.sensor_stale_seconds)):
            reason = "report_expired"
        else:
            reason = sample.get("freshness")
    values["reason_code"] = SOURCE_CODES.get(reason, UNKNOWN_CODE)
    values["qualified"] = (1 if reason == "qualified" else 0 if reason == "change_only_unverified" else None)
    return values


def values(runtime, definitions, now):
    """Only immutable publication data and the explicit manual ownership flag."""
    status, config = runtime.status, runtime.config
    age = max(30, 2 * config.poll_seconds)
    groups = {}
    source_map = sources(config)
    for metric in definitions:
        key = metric.scope, metric.identity
        if key not in groups:
            if metric.scope == "guard":
                groups[key] = guard_values(runtime.power_guard.shutdown_observations, now)
            elif metric.scope == "auxiliary":
                groups[key] = {"manual_owned": flag(runtime.controls.auxiliary_manual)}
            elif metric.scope == "fan":
                groups[key] = fan_values(status, metric.identity, now, age)
            else:
                identity = source_map.get(metric.identity)
                groups[key] = (source_values(status, *identity, config, now, age)
                               if identity is not None else {})
    return {metric.key: groups[metric.scope, metric.identity].get(metric.field) for metric in definitions}
