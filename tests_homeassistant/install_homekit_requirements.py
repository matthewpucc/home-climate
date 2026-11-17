"""Install pinned HA HomeKit imports for a hardware-free bridge contract test."""
import json
from pathlib import Path
import subprocess
import sys

import homeassistant

ha = Path(homeassistant.__file__).parent
requirements = [
    requirement
    for domain in ("homekit", "stream", "camera", "ffmpeg")
    for requirement in json.loads(
        (ha / "components" / domain / "manifest.json").read_text()
    )["requirements"]
]
subprocess.run([
    sys.executable, "-m", "pip", "install", "--constraint",
    str(ha / "package_constraints.txt"), *dict.fromkeys(requirements), "Pillow",
], check=True)
subprocess.run([
    sys.executable, "-c", "from homeassistant.components.homekit.type_thermostats import Thermostat",
], check=True)
