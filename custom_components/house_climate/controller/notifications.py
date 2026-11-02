"""Explicit notification routes; a notify entity is not a service name.

This module has no Home Assistant dependency and is safe in generated/CLI
configuration. Provider options describe a configured route, never proof that
an end device received a message or made a sound.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re


@dataclass(frozen=True)
class NotificationRoute:
    action: str
    entity_ids: tuple[str, ...] = ()
    data: dict = field(default_factory=dict)
    critical_capable: bool = False

    def __post_init__(self):
        if not isinstance(self.action, str) or not re.fullmatch(r"notify\.[a-z0-9_]+", self.action):
            raise ValueError("Notification action must be an explicit notify service")
        if (not isinstance(self.entity_ids, tuple)
                or any(not isinstance(item, str) or not re.fullmatch(r"notify\.[a-z0-9_]+", item)
                       for item in self.entity_ids)
                or len(set(self.entity_ids)) != len(self.entity_ids)):
            raise ValueError("Notification targets must be distinct notify entity IDs")
        if self.action == "notify.send_message":
            if not self.entity_ids:
                raise ValueError("notify.send_message requires explicit target entities")
            if self.data:
                raise ValueError("notify.send_message does not support provider data or critical payloads")
            if self.critical_capable:
                raise ValueError("notify.send_message has no verified per-message critical delivery contract")
        elif self.entity_ids:
            raise ValueError("Notify entity targets require notify.send_message")
        if not isinstance(self.data, dict) or any(key in self.data for key in ("message", "title", "entity_id")):
            raise ValueError("Notification route data cannot override message, title or entity targets")
        try:
            json.dumps(self.data, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Notification route data must contain finite JSON values") from exc
        if type(self.critical_capable) is not bool:
            raise ValueError("critical_capable must explicitly be true or false")
        if self.critical_capable and not self.data:
            raise ValueError("A critical-capable provider route requires its explicit provider payload")
        object.__setattr__(self, "data", deepcopy(self.data))

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        if (not isinstance(value, dict)
                or set(value) - {"action", "target", "data", "critical_capable"}):
            raise ValueError("Notification route requires action and optional target, data, critical_capable")
        target = value.get("target", {})
        if not isinstance(target, dict) or set(target) - {"entity_id"}:
            raise ValueError("Notification target accepts only explicit entity_id")
        entities = target.get("entity_id", [])
        if isinstance(entities, str):
            entities = [entities]
        if not isinstance(entities, (tuple, list)):
            raise ValueError("Notification entity_id must be a string or list")
        return cls(value.get("action"), tuple(entities), deepcopy(value.get("data", {})),
                   value.get("critical_capable", False))

    def to_dict(self):
        result = {"action": self.action}
        if self.entity_ids:
            result["target"] = {"entity_id": list(self.entity_ids)}
        if self.data:
            result["data"] = deepcopy(self.data)
        if self.critical_capable:
            result["critical_capable"] = True
        return result

    def service_data(self, message, title):
        result = deepcopy(self.data)
        result.update(message=message, title=title)
        if self.entity_ids:
            result["entity_id"] = list(self.entity_ids)
        return result


@dataclass(frozen=True)
class NotificationSettings:
    normal: NotificationRoute | None = None
    critical: NotificationRoute | None = None

    def __post_init__(self):
        for key in ("normal", "critical"):
            value = getattr(self, key)
            if value is not None and not isinstance(value, NotificationRoute):
                raise ValueError(f"Notification {key} route must be a NotificationRoute")

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - {"normal", "critical"}:
            raise ValueError("Notifications accept normal and critical routes")
        return cls(**{key: NotificationRoute.from_dict(route) if route is not None else None
                      for key, route in value.items()})

    def to_dict(self):
        return {key: route.to_dict() for key in ("normal", "critical")
                if (route := getattr(self, key)) is not None}

    def route_for(self, severity):
        """A critical condition never silently claims a normal route is critical."""
        if severity == "critical" and self.critical is not None and self.critical.critical_capable:
            return self.critical, True
        return self.normal, False
