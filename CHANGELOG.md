# Changelog

## 0.6.2

- Separate desired room temperatures from the setpoints sent to heat-pump heads. External room probes continue deciding demand and shutdown.
- Add independent heating and cooling call offsets, defaulting to 5°F, to keep a head's onboard thermostat from ending a room call prematurely. Active calls account for the head's reported temperature and respect device limits and steps; zero restores direct room targets.
- Keep call setpoints stable between ordinary 90-second decisions and retain the last acknowledged setpoint during minimum-run holds.
- Stop biased calls promptly at the opposite comfort boundary, missing readings, or system limits. Safety stops remain latched until the next comfort decision.
- Show room goals and head call setpoints separately in room plans. Configure call offsets in System settings using Home Assistant's temperature units.

## 0.6.1

- Make routine comfort decisions every 90 seconds by default, including head setpoints and circulation fan speeds. Ordinary state updates and recovery wakeups no longer trigger extra comfort decisions.
- Keep safety, command acknowledgements, heartbeat renewal, and disconnect recovery responsive between decisions. Dashboard edits, schedule changes, and newly confirmed temperature-limit violations can still trigger an immediate decision.
- Add per-head minimum run and off intervals, defaulting to five and three minutes. Holds use acknowledged HA states and yield to room comfort boundaries, missing probes, unsafe conditions, profile changes, and shutdown.
- Expose decision timing and head cycle holds in controller diagnostics. Existing configurations receive these defaults automatically; timing can be adjusted in System settings.

## 0.6.0

- Add a native room thermostat for Home Assistant's standard Thermostat card, exposing the comfort low/high range and fused room temperature.
- Add an ordinary Desired temperature control per room. Moving it shifts the current comfort band without editing integration configuration.
- Apply dashboard edits immediately through the running policy, preserving controller enablement, ownership, shared-mode arbitration, fan schedules, and safety limits.
- Hold edits until the next room schedule transition; rooms without schedules hold until Schedule is selected. Temporary enables an explicit comfort hold during limits-only periods.
- Persist active holds across HA restarts until their original expiry. Resume the saved schedule from the thermostat's Schedule preset.
- Convert temperatures through HA's standard climate/number APIs and reject invalid or out-of-bounds service inputs.

## 0.5.3

- Accept each head's advertised capabilities: an Off/Cool head can stay idle or cool without faulting the whole controller. Unsupported demand no longer wins shared-system arbitration.
- Preserve fresh off evidence across retries so a recovered head can resume even when a later, already-off command produces no new report.
- Wake native control promptly when availability, modes, off confirmation, or shutdown readiness changes.
- Add per-head Reload integration buttons for explicitly refreshing the owning HA integration, including stale capabilities.
- Add a readable Recovery status with affected rooms, reported modes, and the remaining settling time.
- In the native integration, automatically reload the owning Home Assistant integration after a heat-pump recovery issue persists for two minutes. Reloads share a per-integration cooldown, back off after failures, and stop after three attempts during one continuous incident. Shadow and disabled control never initiate reloads.
- Preserve ownership, genuine off confirmation, and the full configured all-head changeover delay throughout recovery.

## 0.5.2

- Tolerate temporary climate-device disconnections, API failures, and missing acknowledgements without restarting healthy rooms or dropping controller ownership.
- Keep reachable heads in the established shared direction; wait and retry at startup or changeover until every shared head confirms off before settling and activation.
- Reconcile an affected device through a fresh off report before resuming commands, including when an earlier activation may have completed late.
- Recover the auxiliary heater independently while retaining external-probe cutoffs, off retries, and strict heat-to-off-to-fan sequencing.
- Show waiting climate devices and off requests in room/heater plans and Current issues. Clear resolved auxiliary connectivity warnings.
- Update all GitHub Actions to stable Node.js 24 releases with immutable commit pins.

## 0.5.1

- Recover shutdown ownership once climate devices confirm off; persist unfinished circulation-fan stops separately so an offline fan cannot prevent climate control from resuming.
- Keep heat-pump and auxiliary climate control running when a separate circulation fan times out, becomes unavailable, or rejects a service call.
- Show persistent fan warnings in Current issues, retry each fan with capped backoff, and clear its warning after HA acknowledges the requested state.
- Prioritize climate commands over circulation fans and let a new fan-off request supersede an unsuccessful speed command immediately.
- Use integer fan percentages compatible with HA speed converters and accept one percentage point of reporting rounding for stepped fans.
- Preserve strict climate acknowledgement checks, shared-head all-off barriers, auxiliary heat/fan-only transitions, and owned-device shutdown recovery.

## 0.5.0

- Add a Controller enabled switch directly to the native HA device. Room-based installations no longer need a separate YAML enable/watchdog package.
- Show the reason live control is paused in the plan summary and current issues.
- Run a native HA-loop watchdog independently of the worker, checking external heater probes and a 120-second heartbeat.
- Persist ownership before issuing actuator commands, recover previously owned devices after restart, and retain shutdown retries until off is acknowledged.
- Reject queued activations after disabling; keep ownership while actuator calls are still in flight. Enabling control always starts through the all-off barrier.
- Preserve the generated package for standalone/add-on use and legacy version-1 file installations. Native enable controls reset off on restart or configuration reload.

## 0.4.3

- Show a readable plan summary, per-room head actions and active comfort ranges, planned fan percentages, and auxiliary-heater plans directly on the Home Assistant device page.
- Mark shadow decisions as previews and distinguish live plans from equipment feedback. Hide action plans when the controller faults, stops, or has not produced a decision.
- Expose current issues and the latest controller fault as readable entities. Preserve existing status and room-temperature identities.
- Display temperatures in HA's unit system and report the exact scheduled profile used by the decision engine. Room and fan configuration changes automatically update the available entities.

## 0.4.2

- Publish numbered GitHub Releases after CI passes, with changelog notes and installation bundles, so HACS can offer versioned updates.
- Read only configured entities through HA's direct cached-state API, avoiding per-poll event-loop waits and whole-house state copies.
- Preserve probe report timestamps and include all controller interlocks and legacy helpers in selective reads.
- Reject queued native operations after their deadline so a delayed command cannot activate equipment after timing out.
- Keep the latest fault reason, time, and count visible in controller status after recovery; include the operation in timeout messages.

## 0.4.1

- Fix missing room-name translation errors when opening room menus during setup or configuration. Room names now appear in the menu description, where Home Assistant supports placeholders.

## 0.4.0

- Replace fixed household roles with arbitrary rooms, editable names, shared-head assignments, profiles, schedules, and fan rules.
- Add native HA configuration and options editors with entity/area selectors and local temperature units.
- Add fused per-room temperature sensors with stable IDs across renames.
- Make auxiliary heating optional and assignable to any room.
- Preserve version-1 file/helper operation and provide import into the HA editor.
- Replace personal mappings with synthetic examples; add migration and room-configuration coverage.
- Include public distribution source, tests, CI, documentation, and MIT license.

## 0.3.0

- Add native Home Assistant lifecycle, status diagnostics, and HACS packaging.
- Share the controller engine between native integration, CLI, Docker, and add-on.
