# Install Home Climate

## HACS

1. Open HACS → Custom repositories.
2. Add `https://github.com/matthewpucc/home-climate`, category **Integration**.
3. Download **Home Climate** and restart Home Assistant.

For manual installation, copy `custom_components/house_climate` from the release ZIP into HA's config directory and restart.

## Set up in Home Assistant

First place your private device map at `/config/home_climate_bindings.json`. Do this before upgrading an existing entry, too. A prepared map avoids retyping device IDs; the public `bindings.example.json` contains synthetic examples only.

1. Open Settings → Devices & services → Add integration → **Home Climate**.
2. Select whether device commands are allowed and review the separate protection temperatures. Leaving live commands off gives a shadow preview with no writes.
3. Inspect room climate cards and plan/status entities on the device page.
4. When ready, turn on **Controller enabled**.

The comfort policy is fixed. Update the private bindings file when equipment entities change; update the source when schedules or comfort priorities change. There is no room/schedule editor.

Configure contains the live-command opt-in and separate [protection settings](protection-temperatures.md). Room temperature/range changes belong on the dashboard cards and hold until the next schedule change. Upgrades preserve existing entity identities and compatible temporary holds.

The living-room heater power guard is independent of the comfort switch once live commands are permitted. It can cut a manually operated heater for overload, low battery, or missing safety telemetry. A separately bound shared AC emergency cutoff requires explicit opt-in and manual recovery; it is not enabled by binding a dedicated heater meter. See [meter selection and recovery](heater-emergency-cutoff.md#dedicated-load-meter).

## Updates

The rename to Home Climate preserves the existing integration, entities, stored settings, and add-on install. Existing `house_climate_bindings.json` files continue to work. If both private files exist, `home_climate_bindings.json` takes precedence; an invalid new file fails closed rather than selecting the older wiring.

Install a published release through HACS and restart Home Assistant. Comfort control starts disabled. The persisted power-cut latch survives restart; the dedicated outlet is not blindly re-enabled. Review the power-guard and recovery statuses before starting another trial.

Version 0.8.8 combines isolated dedicated-relay recovery and advisory whole-circuit
telemetry. Existing meter/binding selections remain valid; no JSON migration is
required. Keep comfort and live commands Off while updating, then verify guard
status and fresh source reports before a supervised commissioning check. Shared
AC fallback and manual-recovery latches are not enabled or cleared by upgrading.

See [fixed wiring](configuration.md) and [operating rules](control.md).
