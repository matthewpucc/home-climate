"""Conservative source-air direction for configured transfer fans.

An ambient probe does not measure a head's discharge. Keep possible conditioning
directions through pending starts, delayed reports and a confirmed idle interval.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class _Direction:
    last_possible: datetime
    idle_since: datetime | None = None


class TransferAirflow:
    def __init__(self, config):
        self.config = config
        self._directions: dict[str, dict[str, _Direction]] = {}

    @staticmethod
    def _reported_at(state, now):
        for key in ("last_reported", "last_updated", "last_changed"):
            try:
                stamp = datetime.fromisoformat(state[key].replace("Z", "+00:00"))
                if stamp.tzinfo is not None and stamp <= now:
                    return stamp
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
        return None

    @staticmethod
    def _observed_directions(state):
        mode = state.get("state")
        action = state.get("attributes", {}).get("hvac_action")
        directions = set()
        if mode in ("heat", "cool"):
            directions.add(mode)
        elif mode == "dry":
            directions.add("cool")
        elif mode in ("auto", "heat_cool"):
            directions.update(("heat", "cool"))
        if action in ("heating", "cooling"):
            directions.add("heat" if action == "heating" else "cool")
        return directions

    def observe(self, states, now, plan):
        for head, entity in self.config.heads.items():
            state = states.get(entity, {})
            observed = self._observed_directions(state)
            active = set(observed)
            if head in plan.head_targets_f and plan.mode in ("heat", "cool"):
                active.add(plan.mode)
            attrs = state.get("attributes", {})
            idle = (state.get("state") in ("off", "fan_only")
                    and bool(attrs.get("hvac_modes")) and not observed)
            if not idle and not observed:
                # Unknown source output is not evidence for useful ambient air.
                active.update(("heat", "cool"))
            directions = self._directions.setdefault(head, {})
            for mode in active:
                directions[mode] = _Direction(now)
            reported = self._reported_at(state, now)
            for mode, previous in list(directions.items()):
                if mode in active:
                    continue
                # A pending opposite-mode call must not erase residual air.
                # Its direction remains possible, while fresh idle reports can
                # independently settle the previous conditioning direction.
                if not idle or reported is None or reported <= previous.last_possible:
                    previous.idle_since = None
                    continue
                if previous.idle_since is None:
                    previous.idle_since = now
                if (now - previous.idle_since).total_seconds() >= self.config.off_settle_seconds:
                    directions.pop(mode)
            if not directions:
                self._directions.pop(head, None)

    def opposing_direction(self, head, temperature, target):
        directions = self._directions.get(head, ())
        if "cool" in directions and temperature <= target:
            return "cooling"
        if "heat" in directions and temperature >= target:
            return "heating"
        return None
