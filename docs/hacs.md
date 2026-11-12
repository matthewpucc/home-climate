# Install Home Climate

## Home Assistant Marketplace (2026.11+)

1. Open **Settings → Marketplace → Add from link** (upper right).
2. Enter `https://github.com/matthewpucc/home-climate` under **GitHub link**, then select **Add**. If prompted for **Type**, select **Integration** and add again.
3. Open **Home Climate**, select **Install**, then restart Home Assistant. Connect GitHub when prompted if it is not already connected.

Home Assistant 2026.11 integrates the former HACS workflow into Marketplace.
Existing HACS installations migrate to Marketplace; do not install HACS again
on that release. On older supported HA versions, use the legacy HACS custom
repository workflow instead.

Marketplace still consumes `hacs.json` and GitHub releases. That metadata name
and the repository's `scripts/package_hacs.py` tooling are retained deliberately.
The minimum HA version in `hacs.json` is a compatibility floor, not the current
CI target. The full suite is tested against current stable HA as documented in
[Contributing](../CONTRIBUTING.md).

See the [official Marketplace custom-repository guide](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).

For manual installation, copy `custom_components/house_climate` from the release ZIP into HA's config directory and restart.

## Set up in Home Assistant

First place your private device map at `/config/home_climate_bindings.json`. Do this before upgrading an existing entry, too. A prepared map avoids retyping device IDs; the public `bindings.example.json` contains synthetic examples only.

1. Open Settings → Devices & services → Add integration → **Home Climate**.
2. Select whether device commands are allowed and review the separate protection temperatures. Leaving live commands off gives a shadow preview with no writes.
3. Inspect room climate cards and plan/status entities on the device page.
4. When ready, turn on **Controller enabled**.

Update the private bindings file when equipment entities change. Room profiles, schedules and priorities are editable in Configure; see the [configuration guide](configuration.md).

Configure contains the live-command opt-in and separate [protection settings](protection-temperatures.md). Room temperature/range changes belong on the dashboard cards. Temporary lasts until that room's next schedule change; explicit Hold survives boundaries and normal restarts until Schedule is selected. Upgrades preserve existing entity identities and compatible holds. See [room holds](room-holds.md) for the scoped MCP release button and readback sensor.

The living-room heater power guard is independent of the comfort switch once live commands are permitted. It can cut a manually operated heater for overload, low battery, or missing safety telemetry. A separately bound shared AC emergency cutoff requires explicit opt-in and manual recovery; it is not enabled by binding a dedicated heater meter. See [meter selection and recovery](heater-emergency-cutoff.md#dedicated-load-meter).

## Updates

The rename to Home Climate preserves the existing integration, entities, stored settings, and add-on install. Existing `house_climate_bindings.json` files continue to work. If both private files exist, `home_climate_bindings.json` takes precedence; an invalid new file fails closed rather than selecting the older wiring.

Install a published release through Marketplace and restart Home Assistant. On the first upgrade from a version without saved Controller On intent, comfort control starts disabled. After explicitly enabling this version, a graceful restart resumes control only when saved Controller On and Allow Live are both enabled, after startup reconciliation. Explicit Off and operational faults inhibit resumption. The persisted power-cut latch survives restart; the dedicated outlet is not blindly re-enabled. Review the power-guard and recovery statuses before starting another trial.

Version 0.8.8 combines isolated dedicated-relay recovery and advisory whole-circuit
telemetry. Existing meter/binding selections remain valid; no JSON migration is
required. Keep comfort and live commands Off while updating, then verify guard
status and fresh source reports before a supervised commissioning check. Shared
AC fallback and manual-recovery latches are not enabled or cleared by upgrading.

See [fixed wiring](configuration.md) and [operating rules](control.md).
