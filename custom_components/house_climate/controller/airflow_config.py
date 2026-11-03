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
    direction: str | None = "forward"
    oscillating: bool = True

    def validate(self, rooms, outlet="switchable"):
        if self.destination not in rooms or self.source not in rooms:
            raise ValueError("Airflow profiles must reference configured rooms")
        if self.purpose not in ("transfer", "circulation"):
            raise ValueError("Airflow purpose must be transfer or circulation")
        if (self.purpose == "circulation") != (self.source == self.destination):
            raise ValueError("Circulation is local; transfer requires distinct source and destination")
        if any(type(v) is not int or not 0 <= v <= 350
               for v in (self.lower_angle, self.upper_angle)) or self.lower_angle >= self.upper_angle:
            raise ValueError("Airflow sweeps require integer endpoints 0 <= lower < upper <= 350")
        if self.oscillating is not True:
            raise ValueError("This airflow adapter requires explicitly oscillating sweeps")
        if self.calibration not in ("verified", "provisional"):
            raise ValueError("Airflow calibration must be verified or explicitly accepted provisional")
        if outlet == "calibrated_fixed":
            if self.direction is not None or self.calibration != "verified" or self.purpose != "circulation":
                raise ValueError("A calibrated fixed outlet requires verified local circulation and direction=null")
        elif self.direction != "forward":
            raise ValueError("Switchable airflow profiles require an explicit forward outlet")


@dataclass(frozen=True)
class AirflowConfig:
    profiles: dict[str, AirflowProfile] = field(default_factory=dict)
    minimum_dwell_seconds: float = 300
    confirmation_seconds: float = 120
    backend: str = "hass_dyson"
    outlet: str = "switchable"
    calibration_key: str | None = None
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
        if self.outlet not in ("switchable", "calibrated_fixed"):
            raise ValueError("Airflow outlet must be switchable or explicitly calibrated_fixed")
        if self.outlet == "calibrated_fixed":
            if not isinstance(self.calibration_key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.calibration_key):
                raise ValueError("A fixed outlet requires an explicit per-device calibration_key")
        elif self.calibration_key is not None:
            raise ValueError("Fixed-outlet calibration keys cannot describe switchable outlets")
        for name, profile in self.profiles.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError("Airflow profile names must be stable lowercase identifiers")
            profile.validate(rooms, self.outlet)
        for value in (self.minimum_dwell_seconds, self.confirmation_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 3600):
                raise ValueError("Airflow dwell and confirmation must be finite, positive and at most 3600s")
        if not isinstance(self.limitation, str) or len(self.limitation) > 300:
            raise ValueError("Airflow limitation must be a short explanation")
