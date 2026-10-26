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
    """Native house policy is fixed; legacy files remain a standalone path."""
    live = values.get("live", False)
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    from .house_policy import HOUSE_POLICY, house_configuration, load_bindings

    if values.get("policy") == HOUSE_POLICY:
        config = house_configuration(values.get("configuration"),
                                     bindings if bindings is not None else load_bindings(config_dir))
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
