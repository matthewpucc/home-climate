"""Bounded, process-local observations; never consulted by control decisions.

All writes run on HA's loop. Counters survive missed scrapes, not integration
reloads. Detailed history is download-only; entity attributes stay small.
"""
from collections import deque
from datetime import datetime, timezone


EVENTS = (
    "authorization_renewed", "authorization_expired", "off_requested",
    "off_dispatched", "off_completed", "off_failed", "off_cancelled",
    "off_not_dispatched", "off_delegated", "off_delegate_completed",
    "off_stale_callback", "off_acknowledged", "off_isolated",
)
HISTORY_LIMIT = 16


class AuxiliaryDiagnostics:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc).timestamp()
        self.sequence = 0
        self.errors = 0
        self.counts = dict.fromkeys(EVENTS, 0)
        self.times = dict.fromkeys(EVENTS)
        self.history = deque(maxlen=HISTORY_LIMIT)
        self._last_ack = dict.fromkeys(("off_acknowledged", "off_isolated"))
        self.ack_reported_at = None

    def record(self, controls, event, *, command=None, source=None, receipt=None,
               at=None, proof=None, request_generation=None, report_at=None):
        """Best-effort instrumentation must never withhold a safety action."""
        try:
            return self._record(controls, event, command=command, source=source,
                                receipt=receipt, at=at, proof=proof,
                                request_generation=request_generation, report_at=report_at)
        except Exception:
            self.errors += 1
            return None

    def _record(self, controls, event, *, command, source, receipt, at, proof, request_generation, report_at):
        if event in ("off_acknowledged", "off_isolated"):
            identity = (event, receipt if event == "off_acknowledged" else
                        (controls.command_generation(controls.config.heater_entity),
                         controls._auxiliary_stop_epoch))
            if identity == self._last_ack[event]:
                return None
            self._last_ack[event] = identity
        if event == "off_acknowledged":
            self.ack_reported_at = report_at
        self.sequence += 1
        self.counts[event] += 1
        stamp = datetime.now(timezone.utc).timestamp() if at is None else at
        self.times[event] = stamp
        row = {"sequence": self.sequence, "event": event, "at": stamp,
               "generation": controls.command_generation(controls.config.heater_entity),
               "stop_epoch": controls._auxiliary_stop_epoch}
        if command is not None:
            row.update(request=command.get("request", command["sequence"]), source=command["source"],
                       request_generation=command.get("request_generation", command["generation"]))
            if "dispatch_generation" in command:
                row["dispatch_generation"] = command["dispatch_generation"]
        elif source is not None:
            row["source"] = source
        if request_generation is not None:
            row["request_generation"] = request_generation
        if event in ("off_dispatched", "off_delegated"):
            row["dispatch_generation"] = row["generation"]
        if receipt is not None:
            row.update(receipt_generation=receipt[0], receipt_epoch=receipt[1],
                       dispatched_at=receipt[2].timestamp())
        if proof is not None:
            row["proof"] = proof
        if report_at is not None:
            row["report_at"] = report_at
        self.history.append(row)
        return row

    def values(self, controls, instant):
        """Read raw state only: no acknowledgement, expiry or policy helpers."""
        heater = controls.config.heater_entity
        state = controls.hass.states.get(heater) if heater else None
        now = datetime.now(timezone.utc).timestamp()
        report_at = state.last_reported.timestamp() if state else None
        if state is None or state.state in ("unknown", "unavailable"):
            quality = "unavailable"
        elif state.attributes.get("restored"):
            quality = "restored"
        elif state.attributes.get("assumed_state"):
            quality = "assumed"
        elif not 0 <= now - report_at <= 180:
            quality = "stale" if now >= report_at else "future"
        else:
            quality = "reported"
        renewed = self.times["authorization_renewed"]
        valid = (None if renewed is None else int(
            controls.active and controls.live and controls.enabled
            and not controls.recovering and not controls._closing
            and not controls.auxiliary_manual and heater in controls.owned
            and not controls.auxiliary_heartbeat_blocked
            and bool(controls.auxiliary_authorized_at)
            and 0 <= instant - controls.auxiliary_authorized_at
                <= controls.config.auxiliary_heartbeat_timeout_seconds))
        native_off = (None if quality != "reported" else int(
            state.state == "off" and state.attributes.get("hvac_action") not in ("heating", "cooling")))
        values = {f"{key}_count": value for key, value in self.counts.items()}
        values.update({f"{key}_at": value for key, value in self.times.items()})
        values.update(
            session_started_at=self.started_at, sequence=self.sequence,
            off_ack_reported_at=self.ack_reported_at,
            observation_errors=self.errors, authorization_valid=valid,
            authorization_deadline_at=(renewed + controls.config.auxiliary_heartbeat_timeout_seconds
                                       if renewed is not None else None),
            ownership_retained=int(heater in (*controls.owned, *controls.pending_auxiliaries)),
            stop_pending=int(controls._auxiliary_stop_pending and not controls._auxiliary_heartbeat_off_ack),
            pending_jobs=len({job for job in (*controls.stop_jobs({heater}),
                                             *controls.pending_device_jobs({heater}))
                              if not isinstance(job, str)}),
            command_uncertain=int(heater in controls._uncertain_device_jobs),
            ownership_generation=controls.ownership_generation,
            auxiliary_generation=controls.auxiliary_generation,
            stop_epoch=controls._auxiliary_stop_epoch,
            reported_off=native_off,
        )
        attributes = {"schema_version": 1, "report_quality": quality,
                      "report_at": report_at, "generation": controls.command_generation(heater),
                      "stop_epoch": controls._auxiliary_stop_epoch,
                      "manual": controls.auxiliary_manual,
                      "recovering": controls.recovering,
                      "last_event": dict(self.history[-1]) if self.history else None}
        return values, attributes

    def download(self):
        return {"schema_version": 1, "session_started_at": self.started_at,
                "history_limit": HISTORY_LIMIT, "sequence": self.sequence,
                "counts": dict(self.counts), "last_occurrence": dict(self.times),
                "observation_errors": self.errors,
                "off_ack_reported_at": self.ack_reported_at,
                "recent_events": [dict(row) for row in self.history]}
