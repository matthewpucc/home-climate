"""Load user-owned configuration without blocking Home Assistant's event loop."""
from pathlib import Path

from .controller.config import Config


def load_configuration(config_dir, filename, live=False):
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Specify a relative TOML path")
    root = Path(config_dir).resolve()
    relative = Path(filename)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root) or path == root:
        raise ValueError("Configuration must be inside Home Assistant's config directory")
    config = Config.load(path)
    config.validate(live=live)
    return config


def generate_configuration(config, config_dir):
    from .controller.ha_package import write_dashboard, write_package

    output = Path(config_dir) / "house_climate_generated"
    output.mkdir(exist_ok=True)
    write_package(config, output / "house_climate.yaml")
    write_dashboard(output / "dashboard.json", config)


def entry_configuration(config_dir, values, bindings=None):
    """Apply explicit policy edits while private bindings retain device identity."""
    live = values.get("live", False)
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    from .house_policy import HOUSE_POLICY, house_configuration, load_bindings

    if values.get("policy") == HOUSE_POLICY:
        from .controller.protection import ProtectionTemperatures
        protection = ProtectionTemperatures.from_dict(values["protection"]) if "protection" in values else ProtectionTemperatures()
        config = house_configuration(values.get("configuration"),
                                     bindings if bindings is not None else load_bindings(config_dir),
                                     protection, values.get("policy_settings"))
        config.validate(live=live)
        return config
    if "configuration" not in values:
        return load_configuration(config_dir, values.get("config_file", "house_climate.toml"), live)
    config = Config.from_dict(values["configuration"])
    config.validate(live=live)
    return config


def validate_actuators(hass, config):
    """Reject controller room thermostats as physical equipment targets."""
    from homeassistant.helpers import entity_registry as er

    from .const import DOMAIN

    registry = er.async_get(hass)
    if any((entry := registry.async_get(entity)) and entry.platform == DOMAIN
           for entity in config.climate_entities):
        raise ValueError("Select physical heat-pump and auxiliary heater entities")
    for entity in config.fans.values():
        if issue := purifier_identity_issue(hass, config, entity, activation=False):
            raise ValueError(issue)


def purifier_identity_issue(hass, config, entity, *, activation=True, require_identity=False, controls=None):
    """Never treat an auxiliary heater's fan alias as an independent purifier.

    Registry device association is configuration evidence, not physical device
    telemetry. New always-running floors require explicit distinct identities;
    legacy fan rules retain their existing behavior when identity is unknown.
    A known alias also blocks fan Off, which could interrupt manual heating.
    """
    heaters = {value for value in (config.heater_entity,
        getattr(controls, "auxiliary_manual_entity", None),
        *getattr(controls, "pending_auxiliaries", ())) if value}
    if not heaters or not isinstance(entity, str) or not entity.startswith("fan."):
        return None
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    heater_devices = {entry.device_id if (entry := registry.async_get(heater)) else None
                      for heater in heaters}
    fan_entry = registry.async_get(entity)
    fan_device = fan_entry.device_id if fan_entry else None
    if fan_device and fan_device in heater_devices:
        return "Purifier binding is an auxiliary heater fan alias; use a separate physical purifier"
    explicit_floor = any(fan.entity_id == entity and fan.purification_minimum > 0
                         for fan in config.fan_controls.values())
    if activation and (explicit_floor or require_identity) and (None in heater_devices or not fan_device):
        return "Purifier device identity is unverified; a separate device from the auxiliary heater is required"
    return None


def entry_policy_settings(config_dir, values):
    """Resolve form defaults without exposing device mappings or sending commands."""
    from .house_policy import load_bindings, HOUSE_POLICY
    from .controller.house_policy import editable_policy_settings
    bindings = load_bindings(config_dir)
    config = entry_configuration(config_dir, {**values, "policy": HOUSE_POLICY}, bindings)
    return editable_policy_settings(config, bindings)
