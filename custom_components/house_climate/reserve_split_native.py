"""Native pre-dispatch fence for cooling that depends on independent heat."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class ReserveSplitAuthorization:
    heads: frozenset[str]
    expires_at_monotonic: float
    generation: int
    auxiliary_generation: int
    stop_revision: int
    stop_epoch: int
    reserve_room: str
    floor_f: float
    changeover_seconds: float
    repair_epochs: tuple[tuple[str, int], ...]
    probe_sources: frozenset[str]
    attempt_id: str | int | float | None = None

    @classmethod
    def parse(cls, ticket, config):
        if isinstance(ticket, cls):
            ticket = vars(ticket)
        if not isinstance(ticket, Mapping):
            raise ValueError("Reserve split authorization is missing")
        try:
            raw_heads = ticket["heads"]
            heads = frozenset(raw_heads)
            expiry, floor, changeover = (ticket[key] for key in
                                         ("expires_at_monotonic", "floor_f", "changeover_seconds"))
            generation, auxiliary_generation, stop_revision, stop_epoch = (ticket[key] for key in
                               ("generation", "auxiliary_generation", "stop_revision", "stop_epoch"))
            room = ticket["reserve_room"]
            raw_sources = ticket["probe_sources"]
            sources = frozenset(raw_sources)
            configured_sources = {probe.source.strip() for probe in config.zones[room].probes}
            attempt_id = ticket.get("attempt_id")
            epochs = tuple(sorted(tuple(pair) for pair in ticket["repair_epochs"]))
            reserve_head = config.heads[config.zones[room].head]
            if (isinstance(raw_heads, (str, bytes)) or not heads <= set(config.heads.values())
                    or reserve_head in heads or room != config.auxiliary_room
                    or isinstance(raw_sources, (str, bytes)) or not sources or not sources <= configured_sources
                    or len(epochs) != len(config.heads)
                    or {entity for entity, epoch in epochs} != set(config.heads.values())
                    or not all(type(epoch) is int and epoch >= 0 for entity, epoch in epochs)
                    or not all(_finite(v) for v in (expiry, floor, changeover))
                    or expiry <= 0 or not 40 <= floor <= 100 or not 0 < changeover <= 3600
                    or not all(type(v) is int and v >= 0 for v in
                               (generation, auxiliary_generation, stop_revision, stop_epoch))
                    or (attempt_id is not None and not isinstance(attempt_id, str) and not _finite(attempt_id))):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("Invalid reserve split authorization") from None
        return cls(heads, expiry, generation, auxiliary_generation, stop_revision, stop_epoch, room,
                   floor, changeover, epochs, sources, attempt_id)


def proof(api):
    """Read fresh evidence on HA's loop, without refreshing or issuing commands."""
    controls, guard, config = api.controls, api.power_guard, api.config
    heater = config.heater_entity
    if (not api.live or config.legacy or controls is None or guard is None
            or api.stop_event.is_set() or not heater
            or api.service_jobs({heater}) or api.uncertain_service_jobs({heater})):
        return None
    try:
        # Reuse the strict contract for already-owned, acknowledged native
        # Heat (lease, generation, stop epoch, probe/target and manual fences).
        heat = controls.auxiliary_soc_grace_token()
        if heat is None:
            return None
        state = api.hass.states.get(heater)
        instant = datetime.now(timezone.utc).timestamp()
        report = state.last_reported.timestamp()
        if (state.state != "heat" or state.attributes.get("hvac_action") != "heating"
                or state.attributes.get("restored") or state.attributes.get("assumed_state")
                or not 0 <= instant - report <= config.auxiliary_heartbeat_timeout_seconds):
            return None
        meter = guard.reserve_split_heating_evidence(
            maximum_age=config.auxiliary_heartbeat_timeout_seconds,
            not_before=state.last_changed.timestamp())
        if meter is None:
            return None
        repairs = tuple(sorted((entity, api.repair_gate.capture(entity, activation=True))
                               for entity in config.heads.values()))
        return {
            "ready": True, "repair_epochs": repairs, "heat_reported_at": report, "meter_reported_at": meter,
            "expires_at_monotonic": heat[0], "generation": controls.ownership_generation,
            "auxiliary_generation": controls.auxiliary_generation,
            "stop_revision": controls.auxiliary_stop_revision, "stop_epoch": heat[2],
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RuntimeError):
        return None


def floor_valid(api, ticket):
    """Fence new physical cold evidence and repair changes at actual dispatch."""
    from .controller.sensors import probe_evidence

    now = datetime.now(timezone.utc)
    states = {state["entity_id"]: state for state in api._read_states_without_controls()}
    # Never authorize opposite shared-system modes, or use a cooling token
    # queued before a repair or when Living isn't confirmed Off.
    maximum_age = min(api.config.sensor_stale_seconds, api.config.auxiliary_heartbeat_timeout_seconds)
    for entity in api.config.heads.values():
        state = states.get(entity, {})
        attrs = state.get("attributes", {})
        mode = state.get("state")
        try:
            stamp = datetime.fromisoformat(state["last_reported"])
            if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= maximum_age:
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if (attrs.get("restored") or attrs.get("assumed_state")
                or api.repair_gate.held(entity)
                or mode not in (("off", "cool") if entity in ticket.heads else ("off",))
                or attrs.get("hvac_action") == "heating"
                or (mode == "off" and attrs.get("hvac_action") == "cooling")):
            return False
    reports = []
    for probe in api.config.zones[ticket.reserve_room].probes:
        try:
            reports.append(probe_evidence(probe, states, now, api.config.sensor_stale_seconds,
                                          api.hass.config.units.temperature_unit))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    fresh_sources = {row["source"] for row in reports
                     if _finite(row["observed_at"]) and 0 <= now.timestamp() - row["observed_at"]
                        <= api.config.sensor_stale_seconds}
    return (ticket.probe_sources <= fresh_sources and bool(reports)
            and min(row["temperature_f"] for row in reports) > ticket.floor_f)
