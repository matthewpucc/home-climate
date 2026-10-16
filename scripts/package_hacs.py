"""Build install and public-source ZIPs from explicit tracked-source allowlists."""
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist"
OUTPUT.mkdir(exist_ok=True)
INTEGRATION = ROOT / "custom_components/house_climate"
PUBLIC_URL = "https://github.com/matthewpucc/home-climate"
DEVELOPMENT_URL = "https://github.com/matthewpucc/home-climate"


def files_under(directory):
    return sorted(path for path in (ROOT / directory).rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")


paths = files_under("custom_components/house_climate")
manifest = json.loads((INTEGRATION / "manifest.json").read_text())
assert manifest["domain"] == "house_climate" and manifest["config_flow"]
assert (INTEGRATION / "brand/icon.png").is_file()
assert json.loads((ROOT / "hacs.json").read_text())["homeassistant"] == "2026.10.0"

with ZipFile(OUTPUT / "house-climate-integration.zip", "w", ZIP_DEFLATED) as archive:
    for path in paths:
        archive.write(path, path.relative_to(ROOT))
    for name in ("config.example.toml", "LICENSE"):
        archive.write(ROOT / name, name)
    archive.write(ROOT / "docs/hacs.md", "INSTALL.md")

# The source candidate includes the tests/CI needed to maintain the project.
# Generated mappings, secrets, live snapshots, Git metadata, and build outputs
# are never copied. Example and regression inputs are synthetic at source.
public = list(paths)
for directory in ("house_climate", "tests", "tests_homeassistant", "tests_release", "docs", "house_climate_addon"):
    public += files_under(directory)
for name in ("README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
             "hacs.json", "pyproject.toml", "config.example.toml", ".gitignore",
             "Dockerfile", ".dockerignore", "repository.yaml", "scripts/package_addon.py",
             "scripts/package_hacs.py", "scripts/validate_distribution.py", "scripts/publish_release.cjs"):
    public.append(ROOT / name)

workflow = (ROOT / ".github/workflows/container.yml").read_text()
# The public repository owns its CI and GHCR package.
workflow = workflow.replace("branches: [work]", "branches: [main]")
with ZipFile(OUTPUT / "house-climate-public-repository.zip", "w", ZIP_DEFLATED) as archive:
    for path in public:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix == ".png":
            archive.write(path, relative)
            continue
        text = path.read_text()
        if relative == "custom_components/house_climate/manifest.json":
            metadata = json.loads(text)
            metadata.update(documentation=PUBLIC_URL + "#readme", issue_tracker=PUBLIC_URL + "/issues")
            text = json.dumps(metadata, indent=2) + "\n"
        elif relative == "README.md":
            a, b = text.index("Use the [installation guide]"), text.index("\n1. Install the integration")
            text = text[:a] + "Use the [installation guide](docs/hacs.md) to add this repository to HACS as an **Integration**, or install the manual ZIP.\n" + text[b:]
        elif relative == "docs/hacs.md":
            a, b = text.index("## HACS"), text.index("## Set up in Home Assistant")
            text = text[:a] + """## HACS

1. Open HACS → Custom repositories.
2. Add `https://github.com/matthewpucc/home-climate`, category **Integration**.
3. Download **House Climate** and restart HA.

For manual installation, copy `custom_components/house_climate` into HA's config directory and restart.

""" + text[b:]
        text = text.replace(DEVELOPMENT_URL, PUBLIC_URL).replace("ghcr.io/matthewpucc/home-climate", "ghcr.io/matthewpucc/home-climate")
        text = text.replace("/blob/main/", "/blob/main/").replace("/tree/main/", "/tree/main/")
        archive.writestr(relative, text)
    archive.writestr(".github/workflows/container.yml", workflow)
print("Prepared manual installation and public-source candidate. Nothing was published.")
