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

Use the room thermostats and **Desired temperature** controls for everyday temperature adjustments. Use **Configure** to edit rooms, names, saved schedule defaults, fans, and system settings later. Changes are collected in a draft and apply together when saved. Cancelling leaves the running configuration untouched. Saving reloads the integration, stopping owned devices and resetting Controller enabled to off. Enabling it again restarts through the all-off barrier.

Room IDs stay stable when renamed, preserving entity identity/history. Removing a room or fan also removes its integration-owned temperature and plan entities. Shared heads and airflow references are validated before saving.

**System settings** includes a **Comfort adjustment interval** (90 seconds by default), **Minimum head run time** (five minutes), and **Minimum head off time** (three minutes). Existing installations pick up these defaults automatically. **Safety and recovery check interval** remains 15 seconds so probe checks, acknowledgements, heartbeat renewal, and device recovery stay responsive. Ordinary state changes do not trigger extra comfort adjustments; dashboard edits and schedule transitions still apply promptly. Run holds yield to comfort boundaries and safety stops. Controller status attributes expose the decision countdown and any head cycle holds.

**Heating call offset** and **Cooling call offset** control how far the head setpoint is pushed beyond the room goal and its onboard reading during an active call. Each defaults to a 5°F difference (about 2.8°C); zero disables that direction's offset. Room plans show both the room goal and head call setpoint, while thermostat cards keep showing the desired room temperature. The external probes still decide when a call ends.

For sensors that send a temperature only when it changes enough, open **Edit a room → select the room → Room settings** and select them under **Sensors that report only on change**. Their unchanged values remain usable while available in HA. Leave periodic sources out of this selection to retain their age checks.

Temperatures in HA forms follow HA's configured unit system; stored configuration and diagnostic target attributes use Fahrenheit. Temperature entities are converted by HA for display.

## Adjust temperatures from your dashboard

Each configured room automatically gets two normal control entities on the **House climate controller** device:

- **Room thermostat**: the room's fused temperature and its comfort low/high range. Add it to a dashboard with HA's standard **Thermostat** card.
- **Room desired temperature**: the point the controller aims to reach inside that band. Add it to an **Entities** card for direct adjustment.

Use **Add to dashboard** on the device page, or add cards in the dashboard editor and select the House Climate entities for that room. No helpers, custom cards, YAML, or extra integration configuration are required. These controls remain editable in shadow mode, so you can preview changes before enabling live commands.

Moving Desired temperature shifts the existing band around the new target, clipped to the configured system limits. Editing the range keeps the desired temperature inside it. When scheduled comfort is paused, the card shows proposed comfort settings; the room plan continues showing its active safety limits. An explicit edit starts a **Temporary** comfort hold, including during a limits-only period. It keeps the active period's priority and fan schedule.

The hold ends at the next schedule transition for that room, including overnight transitions in HA's timezone. If the room has no schedule, the hold remains until you select **Schedule**. Choose the thermostat's **Schedule** preset in its More info dialog to resume early, or **Temporary** to hold the current values. A still-valid hold survives an HA restart and keeps its original expiry; it does not restart its duration.

Dashboard adjustments apply to the existing worker without reloading the integration or turning off Controller enabled. They do not change saved schedule defaults, enable live control, or directly command a head. The shared controller continues choosing one heat/cool direction for the house. System limits and the auxiliary heater's independent cutoff still constrain the controls. Use the room plan and Recovery status to see whether the requested comfort can currently be served.

## Updates and version numbers

HACS installs stable GitHub Releases such as `v0.6.0`. Release tags match the integration's manifest version. Each release is published only after controller, native HA, packaging, and container checks pass; installation bundles and changelog notes are attached.

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

## Enable live control

For configurations created in the HA room editor or imported as schema version 2:

1. Open **Configure → Auxiliary heater**, if used, and confirm its high setpoint after verifying that it reliably calls for heat.
2. Open **System settings**, verify the equipment's all-off timing, and turn on **Enable live commands**. Choose **Review and save**.
3. Open the **House climate controller** device and turn on **Controller enabled** under **Controls**.

The summary changes from **Preview only** to **Live plan**. Startup turns owned devices off and waits for the configured settling interval before activating heat-pump heads.

The switch and independent watchdog are supplied by the integration. No helper entities, generated YAML package, or Developer Tools action is needed. The switch starts **off after HA restarts or configuration reloads**. Both the live-command setting and this switch must be on. While control is paused, the summary tells you which setting is missing.

The watchdog follows room/probe/cutoff edits automatically. It checks external probes and the controller heartbeat on HA's event loop, independently of the control worker. Ownership is saved before actuator writes so a restart can turn previously owned devices off, including devices removed from the latest configuration. Failed shutdowns keep the watchdog active and prevent unloading/re-enabling until devices acknowledge off.

### Existing helper packages and legacy files

After upgrading a room-based installation, use the device's **Controller enabled** switch. Old enable/lease helpers are no longer its controls. Stop any standalone/add-on controller before enabling the native integration; a recent external lease blocks live setup. Once the old controller is stopped and its ownership is off, the obsolete package can be removed.

Unconverted version-1 TOML installations retain their helper-based behavior. Their device switch controls the existing helper. Open **Configure**, review the imported room settings, and save to move to native controls; this also snapshots existing comfort-helper values. See [migration](configuration.md#upgrading-from-version-1).

## Enable and watchdog package

This package is still required for **standalone, Docker, add-on, and unconverted version-1 file configurations**. It is not required for room-based native HA installations.

Use the CLI's `generate-ha` command, or the native **House Climate: Generate control and watchdog configuration** action, to generate the package. The action writes `house_climate_generated/house_climate.yaml` and `dashboard.json`. Install the YAML under HA's packages directory and restart. Enable `input_boolean.house_climate_enabled` (or `input_boolean.hp_enabled` for legacy configurations) after commissioning live control.

Regenerate this external package after changes to the auxiliary heater, its probes/calibration, cutoff, freshness setting, or helper prefix. Standalone preflight rejects a stale watchdog signature.

## Operation

The loop starts after HA startup and runs in a dedicated worker. It reads only configured entities through HA's synchronous cached-state API, preserving their original report timestamps. Service calls execute on HA's event loop with a deadline that includes queue time; expired requests are rejected before dispatch. Shutdown and option reload stop the worker before another can start.

If a fault clears before you can inspect it, the controller status retains `last_error`, `last_fault_at`, and `fault_count` until the integration reloads. The current state still shows recovery to shadow or live operation. Native watchdog faults also appear under Current issues. A native timeout message identifies the operation. Recurring service timeouts can indicate slow device integrations or a busy HA event loop; cached state reads do not make those services responsive.

A recent existing controller lease blocks native live setup. This is a guard against accidental overlap, not a distributed lock. Stop competing controllers and automations before enabling control.

Turning Controller enabled off commands owned devices off; it does not restore their previous schedules. If auxiliary shutdown cannot be confirmed, ownership remains armed so the watchdog can retry. The watchdog also depends on HA and the device integration remaining operational.

## Heat-pump recovery

A temporary disconnect is handled per device. Healthy heads can continue in the current direction; startup and direction changes still wait for every head to confirm off and complete the configured settling interval. The device's **Recovery status** names a waiting room or shows the settling countdown.

With live control enabled, a heat-pump recovery problem persisting for two minutes can make House Climate reload the HA integration that owns that head. If several heads share the entry, all of them can briefly disappear together. Attempts use a per-integration cooldown and stop after three attempts in one continuous incident. No extra automation is needed. Missing Heat on a valid Off/Cool head produces a capability warning when needed, rather than a reload loop.

Each configured head also has a **Reload integration** button on the House Climate device. It reloads the HA entry that owns that head, including any other heads on the same entry. This explicit action also works while control is disabled or previewing, and can refresh stale capabilities without waiting for automatic recovery. It does not enable House Climate or change its safety checks.
