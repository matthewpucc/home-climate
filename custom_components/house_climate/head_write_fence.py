"""Per-head dispatch revisions for a future scoped native ownership transfer.

No production caller rotates these revisions yet. All mutations must occur on
HA's loop; the worker only captures an immutable row before queueing a write.
"""
from .controller.ha import HAActivationDeferred


class HeadWriteFence:
    def __init__(self, entities):
        self._rows = dict.fromkeys(entities, (0, False))

    def capture(self, entity):
        row = self._rows.get(entity) if isinstance(entity, str) else None
        return row[0] if row is not None else None

    def rotate(self, entity, *, blocked):
        """Invalidate every queued ticket, whether closing or reopening writes."""
        revision, _ = self._rows[entity]
        self._rows = {**self._rows, entity: (revision + 1, blocked)}

    def check(self, entity, captured):
        row = self._rows.get(entity) if isinstance(entity, str) else None
        if row is not None and (row[1] or (row[0] != (0 if captured is None else captured))):
            raise HAActivationDeferred("Heat-pump write belongs to a fenced head ownership scope")


def capture_head_write(controls, entity):
    fence = getattr(controls, "head_write_fence", None)
    return fence.capture(entity) if fence is not None else None


def check_head_write(controls, entity, captured):
    fence = getattr(controls, "head_write_fence", None)
    if fence is not None:
        fence.check(entity, captured)
