"""Install HA's own pinned optional imports for the hardware-free Bluetooth tests."""
import json
from pathlib import Path
import subprocess
import sys

import homeassistant

components = Path(homeassistant.__file__).parent / "components"
requirements = [requirement for component in ("bluetooth", "usb", "hassio")
                for requirement in json.loads((components / component / "manifest.json").read_text())["requirements"]]
subprocess.run([sys.executable, "-m", "pip", "install", *requirements], check=True)
