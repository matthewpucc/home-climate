"""Owner-scoped activation epochs for coordinated integration repairs."""
from __future__ import annotations

from dataclasses import dataclass
import threading

from .controller.ha import HAActivationDeferred


@dataclass(frozen=True)
class RepairLease:
    owner: str
    entities: tuple[str, ...]
    sequence: int


class RepairGate:
    """Keep queued pre-repair starts invalid after the repair lease is released."""

    def __init__(self, heads):
        self._lock = threading.Lock()
        self._epochs = {head: 0 for head in heads}
        self._blocked = {}
        self._off_since = {}
        self._leases = {}
        self._sequence = 0

    def capture(self, entity, *, activation=True):
        with self._lock:
            if not isinstance(entity, str) or entity not in self._epochs:
                return None
            if activation and entity in self._blocked:
                raise HAActivationDeferred("Owning integration repair is holding new head activations")
            return self._epochs[entity]

    def check(self, entity, epoch, *, activation=True):
        with self._lock:
            if not isinstance(entity, str) or entity not in self._epochs:
                return
            if ((activation and entity in self._blocked)
                    or (0 if epoch is None else epoch) != self._epochs[entity]):
                raise HAActivationDeferred("Head command belongs to an earlier integration repair epoch")

    def begin(self, owner, entities):
        with self._lock:
            if owner in self._leases:
                return self._leases[owner]
            entities = tuple(sorted(set(entities).intersection(self._epochs)))
            if any(entity in self._blocked for entity in entities):
                raise RuntimeError("A heat-pump head is already held by another repair")
            self._sequence += 1
            lease = RepairLease(owner, entities, self._sequence)
            for entity in entities:
                self._epochs[entity] += 1
                self._blocked[entity] = owner
                self._off_since.pop(entity, None)
            self._leases[owner] = lease
            return lease

    def release(self, lease, *, off_since=None):
        with self._lock:
            if self._leases.get(lease.owner) != lease:
                return False
            for entity in lease.entities:
                if self._blocked.get(entity) == lease.owner:
                    self._blocked.pop(entity)
                    self._epochs[entity] += 1
                    if off_since is not None:
                        self._off_since[entity] = off_since
            self._leases.pop(lease.owner)
            # Epochs remain advanced: delayed pre-repair callbacks stay invalid.
            return True

    def held(self, entity):
        with self._lock:
            return entity in self._blocked

    def manages(self, entity):
        with self._lock:
            return isinstance(entity, str) and entity in self._epochs

    def overlay(self, entity):
        with self._lock:
            if entity not in self._epochs:
                return None
            return {"epoch": self._epochs[entity], "held": entity in self._blocked,
                    "off_since": self._off_since.get(entity)}
