# Room configuration

For native HA installations, use **House Climate → Configure**. TOML is optional, intended for standalone/add-on operation or importing an existing setup.

## Model

One configuration describes one outdoor/shared heat-or-cool system. Any number of indoor heads can belong to it. Each room references one head; several rooms can reference the same head. An optional independent auxiliary heater belongs to one room.

A room has a stable ID, editable name, optional HA area, temperature probes, base profile, daily schedules, and fan rules. IDs and entity IDs are independent of display names.

| Setting | Meaning |
| --- | --- |
| Target | Desired temperature while comfort control is enabled |
| Half-width | Heat/cool demand begins outside target ± this amount |
| Priority | 0.1–10; higher wins between competing comfort demands |
| Comfort enabled | Off uses system temperature limits instead of a comfort target |
| Scheduled fan speed | Used by fans with the schedule strategy; 0 means off |
| Lower/upper override | Optional explicit comfort edges, for asymmetric bands |
| Schedule | Complete profile override for a daily start/end interval |

Schedules are local to HA's timezone, start-inclusive and end-exclusive. Midnight crossings work; overlapping intervals and equal start/end times are rejected. DST repeated hours use the same local profile. Outside schedules, the base profile applies. Comfort disabled still retains temperature-limit protection.

HA forms use the configured temperature units. TOML and saved config-entry fields ending in `_f` use Fahrenheit; half-band and calibration offsets are Fahrenheit differences.

## Heat-pump call offsets

**System settings** includes **Heating call offset** and **Cooling call offset**, each defaulting to a 5°F temperature difference (about 2.8°C). These push the device setpoint beyond the desired room goal and, when available, its onboard temperature while external probes call for conditioning. Room goals and comfort bands remain unchanged. Set an offset to zero to send direct room targets for that direction.

TOML stores these as `head_heat_call_offset_f` and `head_cool_call_offset_f` in Fahrenheit differences, each from 0 to 20. Existing configurations receive the defaults automatically. See [control behavior](control.md#heat-pump-call-setpoints) for bounds, rounding, and comfort stops.

## Heat-pump acknowledgement wait

**System settings → Heat-pump acknowledgement wait** defaults to 120 seconds. A head can briefly report its old setpoint after accepting a command. The controller waits for the exact requested mode and temperature before acknowledging activation; changes to an unconfirmed target do not restart the overall wait. The TOML setting is `head_activation_timeout_seconds` (1–300 seconds). Off commands, auxiliary-heater commands, fan commands, safety polling, and the watchdog retain their existing timing.

## Sensors

Use independent probes where possible. Ordinary comfort control uses their median; confirmed extremes still protect system limits. Prefer probes that measure occupied room air, and omit or calibrate consistently biased readings. The HA editor assigns a common source identifier to entities from the same registered device, so aliases are not counted twice. Existing imported probe source IDs, unit overrides, attributes, and offsets are preserved when reselecting them.

TOML probes can specify `source`, `attribute`, `unit`, and `offset_f`. Share a source ID between aliases of the same physical probe. Explicit units are particularly useful for attribute-based probes used by the auxiliary watchdog. Omit unreliable sensors, especially the auxiliary heater's internal temperature.

Periodic probes use the configured maximum report age. Some battery sensors report a temperature only when it changes enough; an old measurement from such a source can still be valid. In **Edit a room → select a room → Room settings**, select those sources under **Sensors that report only on change**. Other sources retain normal age checks. Removing a source from that selection restores its age check.

TOML uses `report_on_change = true` on the individual sensor entry. The default is false. This option accepts the last reported value while HA considers the entity available; it does not infer a new measurement or prove that a silent device is connected. Unknown/unavailable, invalid, implausible, missing-timestamp, and future-dated readings remain rejected. Native and generated auxiliary watchdogs follow the same policy. Standalone/legacy users must regenerate their HA package after changing this setting.

At least one probe must be configured per room. Operation can continue with one valid independent source and a diagnostic warning. If all sources fail, the room's shared head is inhibited.

## Fans

| Strategy | Behavior |
| --- | --- |
| cooling | Run during this room's cooling demand |
| conditioning | Run during this room's heating or cooling demand |
| schedule | Use the active profile's fan speed |
| transfer | Run for useful air transfer, or assist a shared head conditioning this room |
| always | Continuous circulation while the room has valid temperature data |

A fan belongs to the room it helps, regardless of its physical location. Transfer rules also select a source room. Two fans can help the same room. Each fan can appear only once in a configuration.

Transfer control uses temperature-difference hysteresis: start above 0.75°F, stop below 0.25°F, and stop when the source air would no longer help. When the source and destination share a head, transfer fans also run at their configured percentage while that head is selected in a direction that helps the destination. This moves conditioned air before the source room's average temperature changes. Fast safety checks remove head-dependent assistance when the head is inhibited; useful independent transfer can continue. Actual airflow direction is installation-specific.

## Optional auxiliary heater

Assign its climate entity and room, high heating setpoint, external cutoff, and optional fan-only behavior. The cutoff must exceed all configured comfort-band upper bounds and lie inside system limits. The high device setpoint must be supported and verified to call for heat.

A combined heater/fan uses only its climate entity. Its `fan_only` mode retains the existing device fan speed; the integration does not assume a separate fan-speed service. Unsupported fan-only mode is skipped. Do not select its internal temperature as an external probe.

## TOML

[config.example.toml](../config.example.toml) documents schema version 2 with synthetic devices. Change stable room IDs, display names, devices, profiles, and schedules as needed. Remove auxiliary fields and fan arrays when unused.

Advanced TOML settings include sensor freshness/confirmation, calibration, forecast horizon, switch confirmation, command timeout, and helper prefix. Native users can import an advanced TOML file; options edits preserve fields not exposed by the editor.

Never put API tokens in configuration files. Standalone REST credentials belong in `HA_URL` and `HA_TOKEN`.

## Upgrading from version 1

Upgrading the integration preserves the old TOML file and live HA helpers. Their values remain authoritative until you save a room-based configuration in **Configure**.

When opening the editor or importing the old file, the controller translates the former room roles into explicit profiles, schedules, priorities, and fan rules. It copies current numeric, schedule, and fan-enable helper values from HA; missing helpers use the prior defaults. The editor starts in shadow mode for this conversion.

Review the imported draft while HA helpers are loaded. Saving stores the configuration in HA; the TOML file and old comfort helpers then stop being authoritative. The helper prefix remains in the stored configuration for compatibility, but native control uses the device's Controller enabled switch.

After saving, verify live-command settings and turn on Controller enabled on the device. Native controls and the watchdog are created automatically. Stop the old controller before migration; once its ownership is off, the obsolete helper package can be removed. Keep a backup of the old file/package until migration is verified. Standalone/add-on installations continue to require their generated package.

An ordinary room rename preserves its stable ID. Existing legacy role words remain only in the compatibility reader and regression fixtures; new configurations do not depend on them.
