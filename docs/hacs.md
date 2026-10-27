# Install House Climate

## HACS

1. Open HACS → Custom repositories.
2. Add `https://github.com/matthewpucc/home-climate`, category **Integration**.
3. Download **House Climate** and restart Home Assistant.

For manual installation, copy `custom_components/house_climate` from the release ZIP into HA's config directory and restart.

## Set up in Home Assistant

First place your private device map at `/config/house_climate_bindings.json`. Do this before upgrading an existing entry, too. A prepared map avoids retyping device IDs; the public `bindings.example.json` contains synthetic examples only.

1. Open Settings → Devices & services → Add integration → **House Climate**.
2. Select whether device commands are allowed. Leaving this off gives a shadow preview with no writes.
3. Inspect room climate cards and plan/status entities on the device page.
4. When ready, turn on **Controller enabled**.

The operating policy is fixed. Update the private bindings file when equipment entities change; update the source when operating rules change. There is no room/schedule editor.

Configure contains only the live-command opt-in. Room temperature/range changes belong on the dashboard cards and hold until the next schedule change. Upgrades preserve existing entity identities and compatible temporary holds.

The living-room heater power guard is independent of the comfort switch once live commands are permitted. It can cut a manually operated heater for overload, low battery, or missing safety telemetry. It never switches network equipment or the Elite's overall AC output.

## Updates

Install a published release through HACS and restart Home Assistant. Comfort control starts disabled. The persisted power-cut latch survives restart; the dedicated outlet is not blindly re-enabled. Review the power-guard and recovery statuses before starting another trial.

See [fixed wiring](configuration.md) and [operating rules](control.md).
