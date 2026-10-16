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


def entry_configuration(config_dir, values):
    """UI configuration takes precedence; old file-based entries remain supported."""
    live = values.get("live", False)
    if type(live) is not bool:
        raise ValueError("Live control must be a boolean")
    if "configuration" not in values:
        return load_configuration(config_dir, values.get("config_file", "house_climate.toml"), live)
    config = Config.from_dict(values["configuration"])
    config.validate(live=live)
    return config
