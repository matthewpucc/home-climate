# Room holds and scoped restoration

Each virtual room thermostat has three presets. These are requests to Home Climate, not physical equipment modes.

| Preset | Meaning | Expiry |
| --- | --- | --- |
| Schedule | Use the room's current scheduled/seasonal profile | None |
| Temporary | Use the edited desired temperature and range | That room's next scheduled profile change; none if the room has no transitions |
| Hold | Keep the desired temperature and range until explicitly released | None; survives ordinary reloads and restarts |

Select Hold before or after editing. While Hold is active, later desired-temperature and range edits remain in Hold. Changing the desired temperature shifts its range using the existing room-card behavior, so set the desired point first and then the final low/high range. Selecting Hold itself preserves all three current values, including a displayed limits-only range, and enables comfort for that room. The scheduled priority and fan settings can still change. Selecting Temporary explicitly returns to its ordinary expiry behavior. Selecting Schedule, or pressing that room's Resume schedule button, durably releases either kind of override.

Holds do not enable Controller or Allow Live, bypass protection or equipment timers, change a regular schedule, or return a manually owned auxiliary to automation. A resumed room's new demand can affect shared-head arbitration through the normal policy; no other room's saved settings are edited. Restoring the schedule is not proof that physical equipment has reached its new target.

## Persistence and errors

The mode and temperatures are saved together before new intent is published or the worker is woken. A storage error leaves the prior durable intent in effect and reports a service error; failed release cannot display Schedule. Retrying release after it succeeded is a no-op. The climate card exposes `comfort_mode` (`schedule`, `temporary`, `hold`) and `override_expires_at`; Hold always has a null expiry. A separate room comfort-mode sensor exposes `Schedule`, `Temporary` or `Hold` as its primary state for integrations that omit climate attributes.

Legacy records without a mode remain Temporary, including their existing behavior when no schedule transition exists. Normal restarts with unchanged valid configuration preserve Hold. Existing room-identity/configuration fingerprints and temperature bounds still reject mismatched or invalid saved requests; removing a room or deliberately changing incompatible wiring, schedules or bounds can invalidate a saved hold. Known supported house-policy migration retains an explicitly valid Hold. Downgrading to code that predates this preset is not a supported way to retain Hold. Inspect the actual mode after upgrades, deliberate configuration changes or storage errors.

## Narrow Assist/MCP release

Each configured room adds two entities under the Home Climate device:

- **`<room name> resume schedule`** — button, releasing only that room's override. It accepts no target, mode, duration or equipment parameters.
- **`<room name> comfort mode`** — enum sensor, providing the saved/effective mode as its primary state.

Their stable integration unique IDs are `<entry_id>_resume_schedule_<room_id>` and `<entry_id>_comfort_mode_<room_id>`. Home Assistant assigns the actual `button.*` and `sensor.*` entity IDs from its registry, including any prior renames/collisions. Copy those actual IDs from Settings → Devices & services → Home Climate → device/entities after installing a release containing this feature; do not construct them from these examples. The existing room climate and desired-number IDs remain unchanged.

Expose only the intended room's resume button and comfort-mode sensor to the Home Assistant Assist/MCP connection, using the entities' voice-assistant exposure settings. No JSON binding change, helper script, general service-call tool, or equipment exposure is required. Use unique, unambiguous entity names or aliases. The feature does not expose entities automatically, and merely exposing the virtual climate entity does not add a preset-setting capability to MCP.

The My Wanderings connector schema inspected during development provides:

```javascript
// Discovery/readback first. Use the actual uniquely resolved name/alias.
await codemode.hass_mcp_homeassistant__GetLiveContext({
  name: "<actual room comfort-mode entity name or alias>", domain: "sensor"
});

// Only after authorized release and successful unique entity discovery:
await codemode.hass_mcp_intent__HassTurnOn({
  name: "<actual room resume-schedule button name or alias>", domain: ["button"]
});

// Read again; require the intended room's sensor to report Schedule.
await codemode.hass_mcp_homeassistant__GetLiveContext({
  name: "<actual room comfort-mode entity name or alias>", domain: "sensor"
});
```

The connector calls this action “Turn on/opens/presses”; Home Assistant's `HassTurnOn` handler maps a button specifically to `button.press`. It does not energize equipment directly. Synthetic tests exercise that installed HA intent handler, durable release, repeated release, one-room scope, failure readback and independent auxiliary manual storage. This verifies the implementation path, not live exposure or end-to-end access in a particular home. After installation and explicit exposure, rediscover both entities and the current connector schema, then test an authorized release with readback. Missing/ambiguous entities, service errors or a readback other than Schedule mean restoration has not been verified; never substitute a broad area command or controller toggle.

For a future supervised experiment, restoration may release the room Hold at the agreed time. Manual auxiliary ownership is a separate decision and remains untouched by this action. Do not assume a scheduled remote task replaces local temperature protection or that an unconfigured MCP exposure is available.
