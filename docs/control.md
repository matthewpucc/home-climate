# Control behavior

## Sensor fusion and priorities

The controller polls every 15 seconds by default. It rejects stale, unavailable, nonfinite, or implausible probe readings, converts units, applies configured offsets, and takes a median across physical sources. Freshness uses `last_reported` where available, falling back to `last_updated`.

Single raw extremes immediately inhibit conditioning that could worsen the opposite limit. Extremes persisting for the confirmation interval also contribute to demand. A bounded recent temperature trend provides up to ten minutes of anticipation by default.

Heat/cool arbitration compares actual limit violations, forecast limit pressure, configured comfort priority, and severity, in that order. Safety-tier urgency uses the worst room rather than summed votes. Every room attached to a head must have usable readings before that head may run.

Heat/cool direction uses hysteresis, dwell, and sustained competing demand. Default limits are 66–75°F, with proactive demand near 67°F/74°F. These are configurable control objectives; they cannot guarantee physical bounds.

## Device execution

Startup, direction change, mixed reported modes, and recovery request all heads off. Every head must report off before the settling timer begins. Selected heads then receive one shared direction. Unacknowledged climate commands enter device recovery and retry off; startup or changeover continues waiting until the required acknowledgements arrive.

Changing active rooms within the same direction does not restart all heads. Device temperature ranges, steps, and fan percentage steps are respected. Fan speed commands use HA-compatible integer percentages and allow one point of reporting rounding on stepped fans. HA-reported state is the available acknowledgement, not proof of the compressor's physical state.

The provisional settling interval is 180 seconds. Verify it for the equipment. Software timing supplements manufacturer protections.

## Auxiliary heating

The heater's internal thermostat is bypassed operationally by requesting a verified high setpoint and explicitly turning the heater off from external room readings. Hysteresis uses the active room profile. A hot individual probe stops heating even when the median is cold.

Heating and fan-only are mutually exclusive. A heat-to-fan transition first commands off and confirms it on a subsequent poll. Fan-only never sends the high heating setpoint.

The HA watchdog acts while the controller owns the equipment. Every 15 seconds and on relevant state/startup events, it commands off when disabled or when the heartbeat is older than 120 seconds. When heating is possible, no valid external probe or any valid probe at the independent cutoff also triggers off. Confirmed fan-only operation is allowed above the heating cutoff unless the device reports heating.

The native integration supplies its watchdog automatically, using the saved configuration. Standalone/add-on and legacy file installations use the generated HA package, whose mapping signature prevents live control with stale watchdog settings. Both watchdogs run independently of the control worker and depend on HA and the device integration continuing to work.

## Ownership and failure behavior

Shadow mode rejects service writes from the control worker. Entities still expose proposed decisions. Live operation requires explicit configuration, the device's Controller enabled switch, and successful preflight. Standalone and legacy file installations use the HA enable helper instead.

Native ownership is persisted before actuator commands. After an HA restart, the independent watchdog stops previously owned devices even if the new configuration is in shadow mode. It never takes over devices without a recorded ownership claim. The enable switch resets off on restart and configuration reload.

While enabled, House Climate owns its mapped devices. Disable it before manual operation or using competing automations. Normal disable/shutdown attempts off for all owned devices. It does not restore an earlier device schedule.

Invalid configuration, invalid temperature ranges, or a controller-wide failure trigger shutdown attempts. Unsupported requested modes block only the affected device. Temporary climate connection loss is handled per device as described below. Shared direction changes still require the all-off barrier. Native climate ownership remains armed until every owned climate device acknowledges off and in-flight activation calls finish. A failed climate shutdown blocks unload and re-enabling while watchdog retries continue. Unfinished circulation-fan stops are persisted separately and retried without blocking climate recovery. Standalone ownership remains armed if auxiliary shutdown is unconfirmed.

The native integration serializes its worker lifecycle and refuses a second loaded instance. Reload waits for shutdown; a stuck worker blocks successful reload. The standalone CLI additionally has a host-local process lock. Neither provides a distributed lock across separate hosts.

The native watchdog pauses control and stops all owned devices when the worker heartbeat expires after 120 seconds; probe cutoffs independently stop auxiliary heating. The standalone package watches only the auxiliary heater, so shared heads can retain setpoints if its process stops. While HA or the host is down, neither watchdog can run. Native ownership recovery resumes after HA starts.

## Climate connectivity recovery

An unavailable or unknown climate state is not an off acknowledgement. Missing capability metadata is treated as initialization in progress. Known capabilities are respected per device: an Off/Cool head can cool or stay off while other heads heat. Demand for an unavailable mode is excluded from arbitration and reported as a warning. Capability changes are used automatically.

A disconnected climate device, failed service call, or unacknowledged activation enters local recovery. The controller retains ownership and heartbeat, requests off, and waits for a fresh HA report confirming off before resuming that device. Cached pre-command off state cannot acknowledge a potentially late activation. Off retries are bounded to avoid flooding an offline device. Retries retain the original uncertain-command report baseline, so a genuine subsequent off report is not discarded merely because an already-off command produces no further report. Recovery also requires a successfully dispatched superseding off request.

Reachable shared heads continue in the established direction. A request to change direction stops available heads and waits for every head, including any disconnected head, to acknowledge off before starting the full settling interval. Startup follows the same all-head barrier. An unreachable head cannot permit opposite-mode activation.

Auxiliary recovery is independent of the shared heads. The heater receives off retries and no new heat or fan-only activation until off is freshly confirmed. External probe cutoffs and the independent watchdog remain active. Explicit disable, restart recovery, and critical shutdown still retain ownership until climate shutdown is confirmed.

Current issues identifies waiting devices; affected room and heater plans show off requests rather than implying that activation is proceeding. Other room plans remain visible. Recovery status names the affected rooms and the remaining configured settling time. Native control wakes on availability, capability, and relevant off-confirmation changes rather than waiting for its next polling interval.

In the native HA integration, a heat-pump connection or acknowledgement problem lasting at least two minutes can trigger a reload of the integration that owns the affected entity. All heads belonging to that integration may briefly become unavailable. House Climate retains ownership and its existing off-confirmation barriers; reloading never substitutes for an off acknowledgement. A missing mode alone does not trigger a reload.

Reload attempts are serialized and grouped by owning HA config entry. The cooldown is 15 minutes after the first attempt, then 30 minutes, then 60 minutes; after three attempts during one continuous incident, automatic reloads pause until the incident clears. Cooldowns survive healthy reports for the current House Climate load. Disabled and shadow controllers do not initiate reloads. Recovery status includes reload progress, cooldown, and failure details. This native recovery is unavailable to standalone/add-on processes.

## Circulation fan recovery

Separate circulation fans do not block climate preflight or heat-pump operation. An unavailable fan, failed service call, or unacknowledged fan command adds a warning to Current issues while climate control and the other fans continue. Climate commands run first.

Each failed fan retries independently. The first retry delay is the greater of 15 seconds, the poll interval, and the command timeout; subsequent failures double that delay up to five minutes. A warning stays visible until HA reports the requested on/speed or off state. A newly requested off bypasses a failed speed command's backoff. Recovery does not restart the heat pumps' all-off settling interval.

The auxiliary heater's climate entity, including its fan-only mode, still uses strict acknowledgement and heat-to-off-to-fan checks. Disabling or shutting down still attempts off for all owned devices. Native HA retains unfinished circulation-fan stops separately, including fans removed from the current configuration. They resume retrying after a restart and appear in Current issues. Reclaiming a configured fan for live operation cancels its old shutdown retry before new activation. If the integration is unloaded with only circulation-fan stops remaining, their retries resume on the next load; climate shutdown must already be acknowledged.

## Current scope

One shared heat/cool system, one optional auxiliary heater, daily schedules, and percentage-capable fans are supported. Multiple outdoor systems, occupancy/calendar schedules, preconditioning, automatic calibration, and restoration of prior device settings are not implemented.
