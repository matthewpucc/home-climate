"""Read-only evidence for an unexplained native head Off transition."""
from dataclasses import dataclass
import logging

_LOGGER = logging.getLogger(__name__)
ACTIVE_MODES = frozenset({"heat", "cool", "fan_only", "auto", "heat_cool", "dry"})
VALID_MODES = ACTIVE_MODES | {"off"}
# A current HA report is not proof of a physical remote action.
REPORT_MAX_AGE = 15
ACTIVE_MAX_AGE = 180
CONFIRM_MAX_GAP = 90
CONFIRM_MIN_GAP = 1
OWN_COMMAND_WINDOW = 45


@dataclass
class HeadEvidence:
    last_report: float = 0
    active_mode: str | None = None
    active_at: float = 0
    off_at: float = 0
    generation: int | None = None


class UnexpectedOff:
    """Bounded by configured heads; never sends a command or changes ownership."""

    def __init__(self, heads, eligible, now, confirmed=None):
        self._rows = {entity: HeadEvidence() for entity in heads}
        self._eligible = eligible
        self._now = now
        self._confirmed = confirmed
        self._commands = {}

    def command(self, entity, when, context_id):
        if entity in self._rows:
            self._commands[entity] = when, context_id
            self._rows[entity] = HeadEvidence()

    def reset(self, entity):
        if entity in self._rows:
            self._rows[entity] = HeadEvidence()

    def report(self, state, reported_at, context):
        entity = state.entity_id
        row = self._rows.get(entity)
        if row is None:
            return
        now, stamp = self._now(), reported_at.timestamp()
        generation = self._eligible(entity)
        if (generation is None
                or state.attributes.get("restored") or state.attributes.get("assumed_state")
                or state.state not in VALID_MODES
                or not 0 <= now - stamp <= REPORT_MAX_AGE):
            self.reset(entity)
            return
        if row.generation != generation:
            self._rows[entity] = row = HeadEvidence(generation=generation)
        if stamp <= row.last_report:
            return
        row.last_report = stamp
        if state.state in ACTIVE_MODES:
            row.active_mode, row.active_at, row.off_at = state.state, stamp, 0
            return
        recent_command = self._commands.get(entity)
        if (any(marker.user_id is not None or marker.parent_id is not None
                for marker in (context, state.context))
                or state.attributes.get("hvac_action") not in (None, "off", "idle")
                or (recent_command is not None
                    and (stamp - recent_command[0] <= OWN_COMMAND_WINDOW
                         or recent_command[1] in (context.id, state.context.id)))):
            self.reset(entity)
            return
        if row.active_mode is None:
            return
        if not row.off_at:
            if stamp - row.active_at <= ACTIVE_MAX_AGE:
                row.off_at = stamp
            else:
                self.reset(entity)
            return
        elapsed = stamp - row.off_at
        if elapsed > CONFIRM_MAX_GAP:
            self.reset(entity)
        elif elapsed >= CONFIRM_MIN_GAP:
            _LOGGER.warning(
                "Home Climate head %s reported unexplained Off after %s; "
                "this observation alone does not change ownership",
                entity, row.active_mode,
            )
            self.reset(entity)
            if self._confirmed is not None:
                self._confirmed(entity, stamp, generation)
