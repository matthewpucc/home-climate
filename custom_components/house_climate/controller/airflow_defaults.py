"""Accepted house sweeps, kept separate from private physical device bindings."""


CALIBRATIONS = {
    "office": {
        "toward_entertainment": {"lower_angle": 43, "upper_angle": 75, "calibration": "verified"},
        "toward_office": {"lower_angle": 100, "upper_angle": 190, "calibration": "provisional"},
    },
    "entertainment": {
        "entertainment_circulation": {"lower_angle": 100, "upper_angle": 190, "calibration": "verified"},
    },
}


def validate_overrides(overrides):
    if not isinstance(overrides, dict) or set(overrides) - set(CALIBRATIONS):
        raise ValueError("Airflow calibration edits must name a configured house purifier")
    for device, profiles in overrides.items():
        if not isinstance(profiles, dict) or set(profiles) - set(CALIBRATIONS[device]):
            raise ValueError("Only known per-device airflow profiles may be adjusted")
        for name, values in profiles.items():
            if not isinstance(values, dict) or set(values) - {"lower_angle", "upper_angle", "calibration"}:
                raise ValueError("Calibration edits may change sweep endpoints and verification status only")
            value = {**CALIBRATIONS[device][name], **values}
            if (any(type(value[k]) is not int or not 0 <= value[k] <= 350
                    for k in ("lower_angle", "upper_angle"))
                    or value["lower_angle"] >= value["upper_angle"]
                    or value["calibration"] not in ("verified", "provisional")):
                raise ValueError("Calibration requires an ordered integer sweep and verified/provisional status")


def house_airflow(device, identities, overrides):
    validate_overrides(overrides)
    profiles = {}
    for name, angles in CALIBRATIONS[device].items():
        destination = "office" if name == "toward_office" else "entertainment"
        source = ("entertainment" if destination == "office" else "office") if device == "office" else destination
        profiles[name] = {**angles, **overrides.get(device, {}).get(name, {}),
                          "destination": identities[destination], "source": identities[source],
                          "purpose": "transfer" if device == "office" else "circulation"}
    limitation = "No Office-facing profile is known for this device; only Entertainment circulation is supported"
    if device == "office":
        limitation = ("Office-facing sweep is provisionally accepted; physical calibration pending"
                      if profiles["toward_office"]["calibration"] == "provisional" else "")
    return {"profiles": profiles, "limitation": limitation}
