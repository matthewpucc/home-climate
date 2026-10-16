# Contributing

Use synthetic entity IDs and snapshots in issues, tests, examples, and pull requests. Do not commit HA tokens, exported live states, personal mappings, or generated local packages.

## Architecture

The canonical controller is `custom_components/house_climate/controller/`. It is standard-library Python and is also packaged as `house_climate` for the CLI. The root `house_climate/` module is a source-checkout import shim.

- `config.py`: versioned rooms, profiles, schedules, and validation.
- `sensors.py`: freshness, physical-source fusion, extremes, and trends.
- `policy.py`: pure decisions; no I/O.
- `executor.py`: acknowledged commands and all-off transitions.
- `ha_package.py`: independent HA lease/watchdog generation.
- `legacy*.py`: version-1 compatibility only.
- Native `config_flow.py`, `runtime.py`, and `sensor.py`: HA editor, worker bridge, and diagnostics.

Keep behavior dependent on configuration, never on display names or particular manufacturers.

## Checks

Core (Python 3.11+):

```sh
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
python -m house_climate --config config.example.toml check-config
python -m house_climate --config config.example.toml demo
```

Native integration (Python 3.14):

```sh
python -m pip install -e '.[test]' 'homeassistant==2026.10.1'
python -m unittest discover -s tests_homeassistant -v
python scripts/package_hacs.py
```

Tests cover arbitration, shared heads, sensor failures, schedules/DST, room edits, migration, shadow isolation, shutdown, unit conversion, and rendered watchdog templates. Native tests use a real HA state/service registry. No tests connect to physical equipment.

## Packaging

Keep versions aligned in `pyproject.toml`, the integration manifest, and add-on metadata. Update the changelog when behavior or migration requirements change.

The development Container workflow runs tests before building native installation artifacts, source-build add-on bundles, and AMD64/ARM64 GHCR images. Pull requests do not publish images. Version and latest tags may be refreshed; commit tags/digests identify exact builds.

`scripts/package_hacs.py` creates a manual integration ZIP and a public-repository candidate from an explicit file allowlist. The candidate has generic examples, tests, and its own CI. It contains no Git history. Review it before publication; producing the artifact does not publish it.
