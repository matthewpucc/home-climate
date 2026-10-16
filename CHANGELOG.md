# Changelog

## 0.5.1

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
