# Install in Home Assistant

The native integration runs inside HA Core, using its state and service APIs. It needs no token, add-on, or container. Requires **Home Assistant 2026.10+**.

## HACS

1. Open HACS → Custom repositories.
2. Add `https://github.com/matthewpucc/home-climate`, category **Integration**.
3. Download **House Climate** and restart HA.

For manual installation, copy `custom_components/house_climate` into HA's config directory and restart.

## Set up in Home Assistant

1. Stop any existing add-on or standalone controller for the same equipment, including its automatic restart.
2. Open **Settings → Devices & services → Add integration → House Climate**.
3. Choose **Add room**. Enter a display name, optional HA area, heat-pump head, and temperature sensors. Select multiple independent probes where possible. Two areas supplied by one head select the same climate entity.
4. Set the room's target, comfort band, and priority. Add schedules and fans from its room menu. Repeat for other rooms.
5. Configure an auxiliary heater if present. Its room must use external temperature probes. Do not separately assign the same device's fan entity.
6. Choose **Review and save**. New configurations start in shadow mode. The integration's device shows controller decisions and fused room temperatures.

Use **Configure** to edit rooms, names, targets, schedules, fans, and system settings later. Changes are collected in a draft and apply together when saved. Cancelling leaves the running configuration untouched. Saving reloads the integration, stopping owned devices and restarting through the all-off barrier.

Room IDs stay stable when renamed, preserving entity identity/history. Removing a room or fan also removes its integration-owned temperature and plan entities. Shared heads and airflow references are validated before saving.

Temperatures in HA forms follow HA's configured unit system; stored configuration and diagnostic target attributes use Fahrenheit. Temperature entities are converted by HA for display.

## Updates and version numbers

HACS installs stable GitHub Releases such as `v0.4.3`. Release tags match the integration's manifest version. Each release is published only after controller, native HA, packaging, and container checks pass; installation bundles and changelog notes are attached.

If you installed before the first release, open **HACS → House Climate → ⋮ → Redownload**, choose the latest numbered release instead of `main`, and restart HA. Refresh repository information first if the new release has not appeared yet. Selecting `main` intentionally tracks development commits and may display a commit hash.

For manual upgrades, download `house-climate-integration.zip` from the release and copy its `custom_components/house_climate` directory into HA's config directory.

## See what the controller would do

Open **Settings → Devices & services → House Climate → House climate controller** (the device). The **Sensors** section automatically includes:

- **Plan summary**: for example, “Preview only · Would cool North alcove, South nook.”
- **A plan for each room**: the proposed head action and setpoint alongside the currently scheduled comfort range. Areas sharing a head show that head's same proposed action; “limits only” means ordinary comfort control is disabled for that period.
- **Room temperatures**: fused readings from the configured probes.
- **Planned fan speeds**: one percentage per configured fan, with 0% meaning off.
- **Auxiliary heater plan**, when configured: heat using room probes, circulate without heat, or stay off.
- **Current issues**: sensor warnings, conflicting demands, or a current controller fault. The latest fault also remains in **Diagnostics → Last controller fault** after recovery, until reload.

These entities appear automatically after updating and restarting HA. Shadow plans say **Would** and send no commands. Live plans still describe requests, not confirmation that a compressor or heater is operating. The summary shows when live control is waiting for heads to stop or settle. Plans become unavailable during startup, faults, and shutdown instead of displaying stale actions.

Temperature text follows HA's unit system. Ranges come from the same active schedule and settings as the controller decision, including HA's timezone. No Developer Tools, template sensors, or dashboard YAML are needed to view plans. Use the device page's **Add to dashboard** button if you want them on your dashboard.

## Enable and watchdog package

The HA editor stores comfort settings. A small generated package supplies a separate enable switch, controller lease/status, and, when configured, an auxiliary-heater watchdog.

1. Run **House Climate: Generate control and watchdog configuration** under **Developer tools → Actions**. It writes `house_climate_generated/house_climate.yaml` and `dashboard.json`. Existing generated copies are replaced; active HA configuration is not modified.
2. Enable packages under your existing `homeassistant:` section:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. Copy the generated YAML to `packages/house_climate.yaml`, check HA configuration, and restart. JSON syntax in the generated file is valid YAML. Paste the generated dashboard JSON into a manual card if desired.
4. Review shadow decisions and verify equipment timing and auxiliary high-setpoint behavior. In **Configure → System settings**, confirm timing and enable live commands. For an auxiliary heater, also confirm its setpoint under **Auxiliary heater**.
5. Save, then enable `input_boolean.house_climate_enabled`.

Both live commands and the enable switch are required. The enable switch resets off on HA restart. The auxiliary watchdog must be enabled and match the controller's mapping before live control starts.

Regenerate and reinstall the package after changing the auxiliary heater, its probes/calibration, cutoff, sensor freshness setting, or helper prefix. Live preflight rejects a stale watchdog signature. Ordinary room labels, targets, schedules, and fan edits do not require package regeneration.

Imported version-1 configurations retain the `hp_` helper prefix; use `input_boolean.hp_enabled` for those. See [migration](configuration.md#upgrading-from-version-1).

## Operation

The loop starts after HA startup and runs in a dedicated worker. It reads only configured entities through HA's synchronous cached-state API, preserving their original report timestamps. Service calls execute on HA's event loop with a deadline that includes queue time; expired requests are rejected before dispatch. Shutdown and option reload stop the worker before another can start.

If a fault clears before you can inspect it, the controller status retains `last_error`, `last_fault_at`, and `fault_count` until the integration reloads. The current state still shows recovery to shadow or live operation. A native timeout message identifies the operation. Recurring service timeouts can indicate slow device integrations or a busy HA event loop; cached state reads do not make those services responsive.

A recent existing controller lease blocks native live setup. This is a guard against accidental overlap, not a distributed lock. Stop competing controllers and automations before enabling control.

Turning the enable switch off commands owned devices off; it does not restore their previous schedules. If auxiliary shutdown cannot be confirmed, ownership remains armed so the watchdog can retry. The watchdog also depends on HA and the device integration remaining operational.
