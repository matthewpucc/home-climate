"""Home Assistant Supervisor launcher; keeps the controller as the main process."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def launch_spec(options: dict, environ: dict[str, str], config_root: Path):
    if not isinstance(options, dict):
        raise ValueError("Add-on options must be an object")
    live = options.get("live", False)
    if type(live) is not bool:
        raise ValueError("The live option must be a boolean")
    filename = options.get("config_file", "house_climate.toml")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("config_file must be a relative TOML path")
    root = config_root.resolve()
    relative = Path(filename)
    config_file = (root / relative).resolve()
    if relative.is_absolute() or not config_file.is_relative_to(root) or config_file == root:
        raise ValueError("config_file must stay inside the Home Assistant config directory")
    token = environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise ValueError("Supervisor token missing; install as an HA add-on or use the CLI")
    env = dict(environ)
    env.pop("SUPERVISOR_TOKEN", None)
    # Always use the internal Core proxy; never send this token to a user-supplied URL.
    env["HA_URL"] = "http://supervisor/core"
    env["HA_TOKEN"] = token
    argv = [sys.executable, "-m", "house_climate", "--config", str(config_file), "run"]
    if live:
        argv.append("--live")
    return argv, env


def main():
    try:
        with open("/data/options.json") as options_file:
            options = json.load(options_file)
        argv, env = launch_spec(options, os.environ, Path("/homeassistant"))
        # Replace this process so SIGTERM reaches the existing graceful shutdown handler.
        os.execve(sys.executable, argv, env)
    except (OSError, ValueError) as exc:
        # Do not print options or environment values (they may contain credentials).
        print(json.dumps({"event": "addon_start_error", "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
