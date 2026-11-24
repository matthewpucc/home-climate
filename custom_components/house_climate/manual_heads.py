"""Durable native manual-head disposition. No release or resume action yet."""
from dataclasses import dataclass
import math
import re

from .controller.ha import HAError

ENTITY = re.compile(r"climate\.[a-z0-9_]{1,243}")
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_RECORDS = 32


@dataclass(frozen=True)
class ManualHead:
    role: str
    entity: str
    registry_id: str
    reported_off_at: float

    def serialize(self):
        return {"entity": self.entity, "registry_id": self.registry_id,
                "reported_off_at": self.reported_off_at}


def read_manual_heads(raw):
    """Reject ambiguous or partially trustworthy disposition atomically."""
    if not isinstance(raw, dict) or len(raw) > MAX_RECORDS:
        raise HAError("Invalid saved manual heat-pump ownership")
    records = {}
    entities, registry_ids = set(), set()
    for role, row in raw.items():
        if (not isinstance(role, str) or not ROLE.fullmatch(role)
                or not isinstance(row, dict)
                or set(row) != {"entity", "registry_id", "reported_off_at"}):
            raise HAError("Invalid saved manual heat-pump ownership")
        entity, identity, stamp = row["entity"], row["registry_id"], row["reported_off_at"]
        if (not isinstance(entity, str) or not ENTITY.fullmatch(entity) or entity in entities
                or not isinstance(identity, str) or not 1 <= len(identity) <= 128
                or not identity.isascii() or not identity.isalnum() or identity in registry_ids
                or type(stamp) not in (int, float) or not 0 < stamp < 253402300800
                or not math.isfinite(stamp)):
            raise HAError("Invalid saved manual heat-pump ownership")
        records[role] = ManualHead(role, entity, identity, stamp)
        entities.add(entity)
        registry_ids.add(identity)
    return records


def held_entities(records, heads, lookup):
    """Protect the saved endpoint and the current role; never follow a remap blindly."""
    return frozenset(entity for role, row in records.items()
                     for entity in (row.entity, heads.get(role), lookup(row.registry_id)) if entity)


def check_no_overlap(held, devices, auxiliary, uncertain, plan):
    plan = plan or {}
    requested = set(plan.get("requests", {})) | set(plan.get("blower_recovery", {}))
    if held.intersection((*devices, *auxiliary, *uncertain, *plan.get("heads", ()), *requested)):
        raise HAError("Saved manual heat-pump ownership overlaps pending Home Climate work")
