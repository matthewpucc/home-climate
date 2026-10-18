# Control behavior

## Sensor fusion and priorities

Routine comfort decisions run every 90 seconds by default (`decision_interval_seconds`). Head targets and ordinary circulation fan speeds remain unchanged between those decisions. Availability events and acknowledgement wakeups do not advance that timer. Dashboard edits, resolved schedule changes, and newly confirmed hard-limit violations can trigger an earlier decision.

Safety and recovery checks run every 15 seconds by default (`poll_seconds`). In the native integration these read HA's cached states rather than polling devices over the network. Heartbeat renewal, command acknowledgements, disconnect recovery, and fresh temperature/probe safety checks continue between comfort decisions. A safety stop remains latched until the next comfort decision; a subsequent ordinary probe update cannot immediately restart it. Live control keeps this check interval at 30 seconds or less to support the independent watchdog.

The controller rejects stale, unavailable, nonfinite, or implausible probe readings, converts units, applies configured offsets, and takes a median across physical sources. Freshness uses `last_reported` where available, falling back to `last_updated`.

Probes explicitly marked `report_on_change` keep their last value usable beyond the periodic report-age limit. Their HA availability, value validity, and timestamp checks still apply; no new measurement is inferred. This policy is shared with auxiliary watchdog checks. Periodic sources continue to expire normally.

Single raw extremes immediately inhibit conditioning that could worsen the opposite limit. Extremes persisting for the confirmation interval contribute to system-limit demand. Ordinary comfort demand, comfort error, and comfort-edge stops use the fused median, so one persistently biased probe cannot create competing comfort calls inside the system limits. A bounded recent temperature trend provides up to ten minutes of anticipation by default.

Heat/cool arbitration compares actual limit violations, forecast limit pressure, configured comfort priority, and severity, in that order. Safety-tier urgency uses the worst room rather than summed votes. Every room attached to a head must have usable readings before that head may run.

Heat/cool direction uses hysteresis, dwell, and sustained competing demand. Default limits are 66–75°F, with proactive demand near 67°F/74°F. These are configurable control objectives; they cannot guarantee physical bounds.

## Dashboard comfort holds

The native HA integration exposes a range thermostat and a Desired temperature number for each room. Card changes are settings requests to the shared policy, not direct head commands. They wake the existing worker and preserve controller enablement and ownership.

An explicit edit enables a temporary comfort profile, preserving the active schedule's priority and fan settings. The hold expires at the next room schedule transition in HA's timezone; without scheduled transitions it remains until Schedule is selected. Holds persist independently of saved integration configuration and retain their original expiry across restarts. Limits, sensor checks, and auxiliary cutoffs continue to apply. The standalone/add-on engine retains its configured profiles.

## Device execution

Startup, direction change, mixed reported modes, and recovery request all heads off. Every head must report off before the settling timer begins. Selected heads then receive one shared direction. Unacknowledged climate commands enter device recovery and retry off; startup or changeover continues waiting until the required acknowledgements arrive.

Changing active rooms within the same direction does not restart all heads. Device temperature ranges, steps, and fan percentage steps are respected. Fan speed commands use HA-compatible integer percentages and allow one point of reporting rounding on stepped fans. HA-reported state is the available acknowledgement, not proof of the compressor's physical state.

Each heat-pump head has a five-minute minimum run hold (`head_minimum_run_seconds`) and a three-minute minimum off hold (`head_minimum_off_seconds`) by default. A run hold starts only when HA acknowledges the requested mode and setpoint; an off hold starts only when off is confirmed without pending or uncertain activation. Unknown or disconnected time does not count as confirmed off. Changing an active head's target does not restart its run timer.

A run hold can retain the last acknowledged target after ordinary demand subsides, while every attached room remains safely inside its comfort band. Missing readings, opposite temperature limits, unsupported modes, changed room profiles, explicit disable/shutdown, and shared direction changes bypass retention. All rooms sharing a head are checked, including rooms configured for limits only. The auxiliary heater has no minimum run hold: its external cutoff remains immediate.

A head returning within the established direction waits its own off interval without stopping healthy heads. The shared all-off settling barrier still applies at startup and direction changes. With off protection enabled, its effective minimum is the greater of the per-head interval and the all-off settling interval. Setting a per-head run or off interval to zero disables that guard; it does not disable the shared all-off barrier. These are HA head-mode holds; HA feedback does not reveal the compressor's physical cycle.

The provisional settling interval is 180 seconds. Verify it for the equipment. Software timing supplements manufacturer protections.

## Heat-pump call setpoints

Room comfort goals and head call setpoints are separate. The external room probes determine whether a room needs conditioning. During a call, the head receives a more demanding setpoint so its onboard thermostat is less likely to stop conditioning while the occupied area is still outside the desired range.

The default heating and cooling offsets are each 5°F. Heating requests the greater of the room goal and the head's valid onboard temperature, plus the heating offset. Cooling requests the smaller of those temperatures, minus the cooling offset. Missing, invalid, or implausible onboard temperatures fall back to the room goal. Onboard temperature is used to choose the actuator setpoint; it does not replace fused external room feedback.

For example, a 72°F cooling goal normally sends a 67°F head setpoint. If the head already reports 69°F while the external room probes still need cooling, the offset asks for 64°F. A configured 65°F minimum cooling setpoint raises that call to 65°F, subject to the head's advertised range and step. The room goal remains 72°F. A device limit can prevent the full offset from being achieved.

Optional per-head cooling floors (`head_cool_min_f`, keyed by physical climate entity) supplement incomplete HA metadata. Both planning and execution apply them. Rounding stays on the original device temperature grid and never goes below the floor. An incompatible floor blocks only the affected head, reports a configuration issue, and does not request an integration reload. Heating and the independent auxiliary heater are unchanged. See [head cooling limits](configuration.md#head-cooling-limits) for setup.

System settings exposes separate Heating call offset and Cooling call offset controls as temperature differences. TOML uses `head_heat_call_offset_f` and `head_cool_call_offset_f`, each 0–20°F. Zero disables the offset for that direction and sends the original room goal, ignoring onboard temperature for this calculation.

Biased setpoints change only at normal comfort decisions, explicit profile changes, or safety-triggered decisions. Minimum-run retention uses the last acknowledged room goal and head call setpoint. The fresh fused room temperature at a room's opposite comfort boundary stop a biased call immediately: cooling stops at or below the lower edge, heating at or above the upper edge. Every room sharing a head is checked. Raw opposite system-limit guards and missing-probe checks remain immediate. These stops override run holds and remain latched until the next comfort decision.

Room plans show both the desired room goal and the head setpoint. Dashboard thermostat and Desired temperature controls always refer to room comfort. HA acknowledgement confirms a mode and setpoint, not physical compressor operation or guaranteed conditioning. The independent auxiliary heater retains its separately configured high setpoint and cutoff.

## Auxiliary heating

The heater's internal thermostat is bypassed operationally by requesting a verified high setpoint and explicitly turning the heater off from external room readings. Hysteresis uses the active room profile. A hot individual probe stops heating even when the median is cold.

Heating and fan-only are mutually exclusive. A heat-to-fan transition first commands off and confirms it on a subsequent poll. Fan-only never sends the high heating setpoint.

The HA watchdog acts while the controller owns the equipment. Every 15 seconds and on relevant state/startup events, it commands off when disabled or when the applicable heartbeat lease expires. When heating is possible, no valid external probe or any valid probe at the independent cutoff also triggers off. Confirmed fan-only operation is allowed above the heating cutoff unless the device reports heating.

The native integration supplies its watchdog automatically, using the saved configuration. Standalone/add-on and legacy file installations use the generated HA package, whose mapping signature prevents live control with stale watchdog settings. Both watchdogs run independently of the control worker and depend on HA and the device integration continuing to work.

## Ownership and failure behavior

Shadow mode rejects service writes from the control worker. Entities still expose proposed decisions. Live operation requires explicit configuration, the device's Controller enabled switch, and successful preflight. Standalone and legacy file installations use the HA enable helper instead.

Native ownership is persisted before actuator commands. After an HA restart, the independent watchdog stops previously owned devices even if the new configuration is in shadow mode. It never takes over devices without a recorded ownership claim. The enable switch resets off on restart and configuration reload.

While enabled, House Climate owns its mapped devices. Disable it before manual operation or using competing automations. Normal disable/shutdown attempts off for all owned devices. It does not restore an earlier device schedule.

Invalid configuration, invalid temperature ranges, or a controller-wide failure trigger shutdown attempts. Unsupported requested modes block only the affected device. Temporary climate connection loss is handled per device as described below. Shared direction changes still require the all-off barrier. Native climate ownership remains armed until every owned climate device acknowledges off and in-flight activation calls finish. A failed climate shutdown blocks unload and re-enabling while watchdog retries continue. Unfinished circulation-fan stops are persisted separately and retried without blocking climate recovery. Standalone ownership remains armed if auxiliary shutdown is unconfirmed.

The native integration serializes its worker lifecycle and refuses a second loaded instance. Reload waits for shutdown; a stuck worker blocks successful reload. The standalone CLI additionally has a host-local process lock. Neither provides a distributed lock across separate hosts.

The native watchdog pauses control and requests all owned devices off when the last confirmed heartbeat exceeds the configured main lease. Auxiliary heating has its own shorter heartbeat lease and independent probe cutoffs. The standalone package retains its 120-second auxiliary-heater watchdog and does not watch shared heads, so those heads can retain setpoints if its process stops. While HA or the host is down, neither watchdog can run. Native ownership recovery resumes after HA starts.

## Native heartbeat delivery

The native integration queues one coalesced heartbeat callback on Home Assistant's event loop without making the worker wait for a synthetic helper-service roundtrip. Only a callback received within five seconds can confirm that pulse. Queuing a pulse does not renew ownership; an expired request or a callback from a previous enable session cannot revive an expired lease.

A brief pending pulse is normal. When confirmation becomes overdue, **Plan summary**, **Recovery status**, and **Current issues** explain that new activations are deferred. Already acknowledged calls are not stopped solely because one heartbeat callback was delayed. Fresh room-safety checks, Off requests, command acknowledgements, and existing timeout deadlines continue. Heartbeat confirmation clears this temporary block without restarting shared-head settling or minimum-run timing.

Configure the native **Controller heartbeat timeout** and **Auxiliary heater heartbeat timeout** in System settings. Their defaults are 300 seconds for the controller and 120 seconds for auxiliary heating. The main timeout accepts 60–900 seconds; the auxiliary timeout accepts 30 seconds up to the main timeout. These settings apply to the native integration; standalone and legacy generated packages keep their existing auxiliary watchdog behavior.

The main HA-loop watchdog expires the last confirmed ownership lease at the configured timeout. The shorter auxiliary lease stops heating independently while healthy heat-pump heads retain the main lease. A late pulse cannot cancel an auxiliary Off obligation: the heater must confirm Off after any in-flight activation, followed by a new confirmed heartbeat, before heating can resume. Confirmed fan-only operation without heating does not trigger this auxiliary heating cutoff.

Heartbeat delivery does not extend either deadline from the worker or bypass disable, unload, persisted ownership recovery, auxiliary probe cutoffs, or failed-command recovery. If HA's event loop itself is stalled, queued actuator calls and the watchdog cannot run until that loop resumes. Native status-helper writes are handled locally because normal integration sensors already publish status; standalone and legacy helper-service behavior is unchanged.

Controller and recovery attributes expose heartbeat state, whether activations are blocked, and the reason. Frequently changing heartbeat ages are omitted from these recorder-facing attributes.

## Saving configuration and reloading

Saving options first persists the new configuration, then stops the existing controller while the HA entry remains loaded. **Plan summary** and **Recovery status** show that the configuration is saved and waiting for devices to stop. The controller blocks new activations and keeps its watchdog cleanup active while critical climate devices are unavailable or have not reported Off.

Only after the worker exits, in-flight activations finish, and critical shutdown ownership is durably cleared does House Climate ask HA to reload. A delayed Off report or a head reconnecting can therefore finish the saved reload without restarting HA Core. Repeated saves while cleanup is pending use the latest saved configuration. The replacement controller starts disabled and still follows the ordinary all-off, settling, and per-head timing rules when enabled.

An explicit unload or reload requested outside this options-save path allows 45 seconds for worker exit and an additional 120-second acknowledgement wait after initial cleanup. Individual cleanup operations retain their own bounds. Unconfirmed critical ownership still blocks successful unload; HA treats a failed unload as nonrecoverable until Core restarts. Pending circulation-fan cleanup alone remains persisted and does not block reload.

## Stop logs

Home Assistant's **Settings → System → Logs** records controller-wide failures separately from individual device recovery. A worker fault logs an ERROR explaining why Off was requested for every configured climate device and circulation fan. Main heartbeat expiry logs a WARNING with the heartbeat age, the configured lease limit, and every owned device included in the stop request. The shorter auxiliary lease reports its heater-only stop separately. Worker pauses and shutdown-storage failures also identify the cause and owned-device scope.

Routine startup reconciliation, explicit disable, saved-ownership cleanup, and normal shutdown use INFO. Enable INFO logging for `custom_components.house_climate` to include these lifecycle messages. Identical stop causes are logged once per incident rather than on every retry; a later control session can log the same cause again. Local climate/fan failures and auxiliary probe cutoffs do not imply a controller-wide shutdown.

When an unload/reload cannot finish, a deduplicated WARNING lists unconfirmed owned climate devices, in-flight activations, retained ownership, and any worker or integration-reload blocker. Pending circulation-fan cleanup alone does not block unload.

These messages describe Off requests, not physical stop confirmation. A failed request names the affected device, while ownership and retries continue under the existing safety rules. The standalone/add-on engine emits structured `stopping` and `stopped` events with the reason, cause, configured-device scope, and any dispatch failures.

## Climate connectivity recovery

An unavailable or unknown climate state is not an off acknowledgement. Missing capability metadata is treated as initialization in progress. Known capabilities are respected per device: an Off/Cool head can cool or stay off while other heads heat. Demand for an unavailable mode is excluded from arbitration and reported as a warning. Capability changes are used automatically.

Heat-pump activation waits up to `head_activation_timeout_seconds` (120 seconds by default) for the exact mode and setpoint report. The first unconfirmed command establishes the deadline, including across same-direction target changes. A stale or bouncing target remains pending without starting a minimum-run timer. Recovery status and room plans show the requested/reported values and countdown. Off, auxiliary, fan, and transport timeouts remain unchanged; safety stops and lost availability supersede pending activation immediately.

A disconnected climate device, failed service call, or unacknowledged activation enters local recovery. The controller retains ownership and heartbeat, requests off, and waits for a fresh HA report confirming off before resuming that device. Cached pre-command off state cannot acknowledge a potentially late activation. Off retries are bounded to avoid flooding an offline device. Retries retain the original uncertain-command report baseline, so a genuine subsequent off report is not discarded merely because an already-off command produces no further report. Recovery also requires a successfully dispatched superseding off request.

Reachable shared heads continue in the established direction. A request to change direction stops available heads and waits for every head, including any disconnected head, to acknowledge off before starting the full settling interval. Startup follows the same all-head barrier. An unreachable head cannot permit opposite-mode activation.

Auxiliary recovery is independent of the shared heads. The heater receives off retries and no new heat or fan-only activation until off is freshly confirmed. External probe cutoffs and the independent watchdog remain active. Explicit disable, restart recovery, and critical shutdown still retain ownership until climate shutdown is confirmed.

Current issues identifies waiting devices; affected room and heater plans show off requests rather than implying that activation is proceeding. Other room plans remain visible. Recovery status names affected rooms, the remaining configured settling time, and per-head run/off holds. Room plans distinguish pending targets from an off-time wait. Stop causes remain available in controller and room attributes after the wait clears; observed head-off reports are distinguished from controller off requests without attributing them to another automation. Climate timeout messages identify the command and observed feedback, while recovery details retain the original uncertain operation. Native control wakes on availability, capability, and relevant off-confirmation changes rather than waiting for its next polling interval.

In the native HA integration, a heat-pump connection or acknowledgement problem lasting at least two minutes can trigger a reload of the integration that owns the affected entity. All heads belonging to that integration may briefly become unavailable. House Climate retains ownership and its existing off-confirmation barriers; reloading never substitutes for an off acknowledgement. A missing mode alone does not trigger a reload.

Reload attempts are serialized and grouped by owning HA config entry. Automatic reloads defer while another healthy head on that entry is heating or cooling; local device retries continue, and deferral consumes no reload attempt or cooldown. Explicit manual reloads remain available. The cooldown is 15 minutes after the first attempt, then 30 minutes, then 60 minutes; after three attempts during one continuous incident, automatic reloads pause until the incident clears. Cooldowns survive healthy reports for the current House Climate load. Disabled and shadow controllers do not initiate reloads. Recovery status includes reload progress, cooldown, and failure details. This native recovery is unavailable to standalone/add-on processes.

## Circulation fan recovery

Separate circulation fans do not block climate preflight or heat-pump operation. An unavailable fan, failed service call, or unacknowledged fan command adds a warning to Current issues while climate control and the other fans continue. Climate commands run first.

Each failed fan retries independently. The first retry delay is the greater of 15 seconds, the poll interval, and the command timeout; subsequent failures double that delay up to five minutes. A warning stays visible until HA reports the requested on/speed or off state. A newly requested off bypasses a failed speed command's backoff. Recovery does not restart the heat pumps' all-off settling interval.

The auxiliary heater's climate entity, including its fan-only mode, still uses strict acknowledgement and heat-to-off-to-fan checks. Disabling or shutting down still attempts off for all owned devices. Native HA retains unfinished circulation-fan stops separately, including fans removed from the current configuration. They resume retrying after a restart and appear in Current issues. Reclaiming a configured fan for live operation cancels its old shutdown retry before new activation. If the integration is unloaded with only circulation-fan stops remaining, their retries resume on the next load; climate shutdown must already be acknowledged.

## Current scope

One shared heat/cool system, one optional auxiliary heater, daily schedules, and percentage-capable fans are supported. Multiple outdoor systems, occupancy/calendar schedules, preconditioning, automatic calibration, and restoration of prior device settings are not implemented.
