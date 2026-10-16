# Changelog

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
