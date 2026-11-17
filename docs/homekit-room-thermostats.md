# Apple Home room thermostats

The optional single-setpoint room thermostat is a separate Home Climate entity. It represents a **room's comfort request**, not a Mitsubishi head or an auxiliary heater. Keep the existing Home Climate range thermostat in Home Assistant for adjusting the low and high limits independently. Only expose the single-setpoint entity to Apple Home.

## Controls

| Apple Home control | Home Climate behavior |
| --- | --- |
| Temperature | Moves the desired point and shifts its existing comfort window using the same rules as the room's Desired Temperature control. For example, 68°F with a 67–71°F window becomes 70°F with a 69–73°F window, unless the room's configured bounds require limiting the shift. A change durably selects Hold until that room is explicitly resumed or its mode is changed; ordinary HA range/number edits retain their existing Temporary behavior. |
| Off | Selects **Protection Only** for this room. Ordinary comfort demand from this room stops; configured building temperature protection, equipment safety checks and independent purifier behavior remain in effect. The setting survives a normal restart. It does not shut down shared equipment or prevent another room from calling for it. |
| Auto | Returning from Protection Only resumes this room's schedule. A repeated Auto write while already in Auto does not clear an existing Temporary or Hold override. Home Climate, not the Apple Home tile, decides whether the shared equipment heats, cools or stays idle. |

An isolated temperature write while Off does not switch the room on. Use Auto first. Actual equipment activity and requested comfort are separate: a head serving several rooms can be running without a call from this room. The thermostat is not an emergency stop, a manual-ownership control, or proof of delivered heat or cooling.

Protection Only is also a preset on the existing Home Assistant range thermostat. Like other saved room overrides, it is tied to the room's validated configuration. Changes to its room policy, schedule, probes, head mapping, time zone, or limits can invalidate the saved override and return the room to Schedule. Verify the mode after a material configuration edit; a normal restart with unchanged configuration preserves it.

## Pilot and rollback

1. Leave the existing Kumo accessory and bridge setup as-is. Enable the optional single-setpoint entity for one room. Use a distinct name for the pilot to avoid ambiguous Siri commands, and select **Thermostat** on the HomeKit Bridge for that entity.
2. First check the accessory against inactive/shadow control, or in a supervised idle window. Note the room's current preset, desired point, and window. In Apple Home and with Siri, change the setpoint while in Auto. Verify the exact Home Climate readback, shifted bounds, preservation of Hold if present, and that the change did not directly command a head or heater.
3. Verify Off enters Protection Only and an Off-state temperature edit alone does not clear it. Verify Auto restores Schedule. Confirm the other rooms' settings and any independent auxiliary manual ownership were not changed. Software tests should cover protection calls and shared heads; the pilot should not create extreme temperatures to test them.
4. Once the Apple Home UI and Siri paths are verified, remove only that room's Kumo accessory from the bridge. Keep the Kumo integration and the underlying device/entity in Home Assistant, since Home Climate still uses it. To roll back the Apple Home surface, restore bridge exposure for the original Kumo entity and remove the pilot entity. Verify the pilot room has returned to the intended Home Climate schedule or preset before the pilot is left unattended.

The bridge can publish a Thermostat accessory without the separate heating and cooling thresholds that it adds for range-capable entities. This does not by itself verify how the current Apple Home app or Siri presents or writes the target when Auto is the only active mode. Treat that as an acceptance check before replacing an existing accessory or rebuilding Apple Home automations.

## Long-running Holds

Any room that remains in Hold for more than 24 hours receives a room-specific persistent HA reminder and one dispatch through the explicitly configured normal (non-critical) notification route. Setpoint changes do not restart the clock. The reminder remains while Hold remains and is recreated if dismissed manually. Resume Schedule, Temporary, or Protection Only clears it; a later new Hold starts a new episode.

The episode start and notification dispatch claim survive reloads and restarts. Legacy Holds without a reliable start time begin their reminder clock when first migrated, not retroactively. Reminder dispatch accepts only the configured normal route with no provider payload and no critical-capable flag, so high/critical provider options cannot leak into this reminder. If the normal route is absent, unsupported, or unavailable, the HA reminder still appears and external dispatch waits for a usable route. The claim is saved before the service call: an interrupted or failed call is not retried, because HA cannot prove whether a phone received it. Service acceptance is not confirmation of phone delivery. Checks run at one-minute intervals, so the reminder may appear up to about a minute after the strict 24-hour threshold.
