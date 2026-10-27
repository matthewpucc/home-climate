# Add-on and Docker installation

The native integration uses fixed operating rules and private device bindings. This alternative runs the same controller as a continuous Supervisor-managed add-on, reading a TOML file. Do not enable both controllers for the same equipment.

## Install the add-on

Download **home-climate-ha-install** from a successful [Container workflow](https://github.com/matthewpucc/home-climate/actions/workflows/container.yml).

- `home-climate-local.tar.gz` contains source for an HA local build. It needs Docker Hub/PyPI access, without GHCR credentials.
- `home-climate-registry.tar.gz` uses `ghcr.io/matthewpucc/home-climate:<version>`. Private pulls require a GitHub token with package access and `read:packages`, configured privately in Supervisor's registry settings.

Extract one archive's `house_climate` directory into HA's local app/add-on directory: `/local_apps` on current HA, or `/addons` on older installations. Refresh the app store and install **Home Climate**. Local metadata updates must be copied into that folder before checking for new versions.

Copy `config.example.toml` to `house_climate.toml` alongside HA's `configuration.yaml`. Replace all synthetic entities and configure the rooms. Leave the add-on options as:

```yaml
config_file: house_climate.toml
live: false
```

The configuration mount is read-only. Supervisor authentication is supplied internally; do not add API tokens to the TOML or add-on options.

Start in shadow mode and inspect logs. Generate the HA enable/watchdog package from the same TOML as described below, install the generated YAML under your HA `homeassistant.packages` configuration, and verify equipment timing and any auxiliary high setpoint.

For live control, set the applicable confirmation flags in TOML, set the add-on's `live` option to true, restart, and enable `input_boolean.house_climate_enabled` (or `hp_enabled` for legacy files). Comfort edits in schema-2 TOML require an add-on restart. The native integration's private bindings do not configure this process.

Enable Start on boot and Supervisor Watchdog if desired. Supervisor's watchdog restarts the process; the generated HA auxiliary watchdog independently monitors temperature and the controller heartbeat.

## Docker

Actions builds AMD64 and ARM64 images after tests pass. It publishes the configured version, `latest`, and `sha-<commit>` tags. Use a digest for an immutable image reference.

```sh
docker build -t home-climate .
docker run --rm --network none --entrypoint python home-climate \
  -m house_climate --config /opt/home-climate/config.example.toml demo
```

The default image entrypoint is the Supervisor launcher. For standalone operation, override it and provide HA credentials through the environment:

```sh
docker run --rm --entrypoint python \
  --env HA_URL --env HA_TOKEN \
  -v "$PWD/config.local.toml:/config/config.toml:ro" home-climate \
  -m house_climate --config /config/config.toml run
```

To generate the package without contacting HA:

```sh
mkdir -p home_assistant
docker run --rm --user "$(id -u):$(id -g)" --entrypoint python \
  -v "$PWD/config.local.toml:/config/config.toml:ro" \
  -v "$PWD/home_assistant:/output" home-climate \
  -m house_climate --config /config/config.toml generate-ha \
  --output /output/house_climate.yaml --dashboard /output/dashboard.json
```

Normal shutdown attempts to turn owned equipment off. The independent HA auxiliary watchdog remains necessary and requires HA to be operational. The add-on has no host networking, device access, Docker socket access, or Supervisor management API permission.
