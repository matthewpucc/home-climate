"""Validate the install ZIP and run tests from the actual public-source ZIP."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
version = json.loads((ROOT / "custom_components/house_climate/manifest.json").read_text())["version"]

with ZipFile(ROOT / "dist/home-climate-integration.zip") as archive:
    names = set(archive.namelist())
    assert "custom_components/house_climate/config_flow.py" in names
    assert "custom_components/house_climate/controller/legacy.py" in names
    assert "LICENSE" in names
    assert "bindings.example.json" in names
    assert not any("__pycache__" in name or name.startswith(".git/") for name in names)
    assert not any(Path(name).name in ("home_climate_bindings.json", "house_climate_bindings.json", "bindings.local.json")
                   for name in names)
    manifest = json.loads(archive.read("custom_components/house_climate/manifest.json"))
    assert manifest["version"] == version
    native_files = {name for name in names if name.startswith("custom_components/house_climate/")}
    expected_files = {path.relative_to(ROOT).as_posix()
                      for path in (ROOT / "custom_components/house_climate").rglob("*")
                      if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
                      and path.name not in ("home_climate_bindings.json", "house_climate_bindings.json", "bindings.local.json")}
    assert native_files == expected_files, "Native integration archive file set differs from checkout"
    for name in sorted(native_files):
        assert archive.read(name) == (ROOT / name).read_bytes(), name
    print(f"Verified native integration version {version}: {len(native_files)} files match checkout", flush=True)
    digest = hashlib.sha256((ROOT / "dist/home-climate-integration.zip").read_bytes()).hexdigest()
    print(f"Native integration ZIP SHA256: {digest}", flush=True)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    with ZipFile(ROOT / "dist/home-climate-public-repository.zip") as archive:
        for name in archive.namelist():
            target = (root / name).resolve()
            assert target.is_relative_to(root), name
            assert not any(part in (".git", "__pycache__") for part in Path(name).parts)
            assert not name.endswith(("config.local.toml", ".jsonl"))
            assert Path(name).name not in ("home_climate_bindings.json", "house_climate_bindings.json", "bindings.local.json")
        archive.extractall(root)
    assert not (root / "home_assistant/house_climate.yaml").exists()
    for folder in ("tests", "tests_homeassistant"):
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", folder, "-q"],
                       cwd=root, check=True)
    subprocess.run(["node", "--test", *[str(p) for p in sorted((root / "tests_release").glob("*.test.cjs"))]],
                   cwd=root, check=True)
    subprocess.run([sys.executable, "-m", "house_climate", "--config", "config.example.toml",
                    "check-config"], cwd=root, check=True)
print("Install layout and public-source tests passed.")
