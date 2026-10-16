"""Source-checkout compatibility for the standalone controller package."""
from pathlib import Path

# Installed wheels map house_climate directly to the same canonical controller.
__path__ = [str(Path(__file__).resolve().parent.parent /
                "custom_components" / "house_climate" / "controller")]
