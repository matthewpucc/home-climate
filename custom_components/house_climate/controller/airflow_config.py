"""Explicit per-device settings; outlet direction is not room-facing aim."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re


@dataclass(frozen=True)
class AirflowProfile:
    destination: str
    source: str
    lower_angle: int
    upper_angle: int
    calibration: str
    purpose: str = "transfer"
    direction: str = "forward"
    oscillating: bool = True

    def validate(self, rooms):
        if self.destination not in rooms or self.source not in rooms:
            raise ValueError("Airflow profiles must reference configured rooms")
        if self.purpose not in ("transfer", "circulation"):
            raise ValueError("Airflow purpose must be transfer or circulation")
        if (self.purpose == "circulation") != (self.source == self.destination):
            raise ValueError("Circulation is local; transfer requires distinct source and destination")
        if any(type(v) is not int or not 0 <= v <= 350
               for v in (self.lower_angle, self.upper_angle)) or self.lower_angle >= self.upper_angle:
            raise ValueError("Airflow sweeps require integer endpoints 0 <= lower < upper <= 350")
        if self.direction != "forward" or self.oscillating is not True:
            raise ValueError("This airflow adapter supports explicit forward, oscillating sweeps only")
        if self.calibration not in ("verified", "provisional"):
            raise ValueError("Airflow calibration must be verified or explicitly accepted provisional")


@dataclass(frozen=True)
class AirflowConfig:
    profiles: dict[str, AirflowProfile] = field(default_factory=dict)
    minimum_dwell_seconds: float = 300
    confirmation_seconds: float = 120
    backend: str = "hass_dyson"
    limitation: str = ""

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        profiles = value.pop("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("Airflow profiles must be named objects")
        return cls(profiles={name: AirflowProfile(**profile) for name, profile in profiles.items()}, **value)

    def validate(self, rooms):
        if self.backend != "hass_dyson" or not self.profiles:
            raise ValueError("Airflow requires supported backend hass_dyson and named profiles")
        for name, profile in self.profiles.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError("Airflow profile names must be stable lowercase identifiers")
            profile.validate(rooms)
        for value in (self.minimum_dwell_seconds, self.confirmation_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 3600):
                raise ValueError("Airflow dwell and confirmation must be finite, positive and at most 3600s")
        if not isinstance(self.limitation, str) or len(self.limitation) > 300:
            raise ValueError("Airflow limitation must be a short explanation")
