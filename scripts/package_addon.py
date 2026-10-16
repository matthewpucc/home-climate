"""Create self-contained local and prebuilt-image HA installation bundles."""
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist"
OUTPUT.mkdir(exist_ok=True)
metadata = json.loads((ROOT / "house_climate_addon/config.json").read_text())
for kind in ("local", "registry"):
    config = dict(metadata)
    if kind == "local":
        config.pop("image")
    with tarfile.open(OUTPUT / f"house-climate-{kind}.tar.gz", "w:gz") as archive:
        payload = (json.dumps(config, indent=2) + "\n").encode()
        info = tarfile.TarInfo("house_climate/config.json")
        info.size, info.mode = len(payload), 0o644
        archive.addfile(info, io.BytesIO(payload))
        for name in ("DOCS.md", "README.md", "CHANGELOG.md", "translations"):
            archive.add(ROOT / "house_climate_addon" / name, arcname="house_climate/" + name)
        if kind == "local":
            paths = [ROOT / name for name in ("Dockerfile", ".dockerignore",
                                               "pyproject.toml", "README.md", "LICENSE", "config.example.toml")]
            paths += sorted((ROOT / "custom_components/house_climate/controller").glob("*.py"))
            for path in paths:
                archive.add(path, arcname="house_climate/" + str(path.relative_to(ROOT)))
print("Created local-build and registry installation bundles in dist/")
