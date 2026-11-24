# Unexplained heat-pump Off observations

Home Climate logs a warning when a head it currently owns reports an
unexplained transition to Off. This is on by default for live, native control.
The warning alone never changes ownership, writes a thermostat command,
or sends a push notification. For a configured native head only, a separate
explicit opt-in can release that head to manual ownership when the evidence
below qualifies and earlier commands are resolved.

The observer must see an active mode reported during the current ownership
lease, within three minutes before Off. The first and second Off reports
must be at least one second and no more than 90 seconds apart. Each report
must be no more than 15 seconds old when observed. Re-reading one cached
state is not a second report. An unavailable, assumed, restored, or stale
report breaks the evidence chain. Home Climate ignores Off feedback within
45 seconds of one of its own mode dispatches to that head or matching the
latest dispatch’s HA context, while a head
operation is pending or uncertain, and when Home Assistant supplies a
user or parent context for either Off report.

**Unexplained does not mean RF remote.** Home Assistant generally reports
state, not which physical remote or controller originated it. A bridge
can also republish cached state and some adapters do not publish a second
unchanged Off report. No log is not proof that a remote was ignored or that
the head remained on.

## Supervised observation

1. Use one known head with reliable feedback while you are present. Keep
   normal Home Climate protections in place. If it's unsafe for Home Climate
   to reassert its requested mode, skip this test.
2. In **Settings → System → Logs**, look for
   `Home Climate head … reported unexplained Off after …`. Let the chosen head
   publish its active mode under Home Climate ownership and avoid tests during
   startup, recovery, or shortly after a Home Climate mode command.
3. Press Off on the physical remote once, and note the head, time, HA state,
   and whether the warning appeared. If the integration reports unchanged
   states, wait for its next Off report; don't simulate confirmation by polling
   Developer Tools or forcing an HA state.
4. Compare a normal Home Climate Off and an HA-initiated Off with the remote
   attempt. Neither should log when recognized by the observer. In the default
   diagnostic-only mode Home Climate can still turn the head back on as part
   of normal policy.

## Optional live pilot

In the HA configuration directory, add `home_climate_manual_head_handoff.json`
containing an array with **one** exact role from your Home Climate `heads`
mapping. For example:

```json
["guest"]
```

No file or an empty array leaves automatic handoff off. An invalid file opts
in no head. Read it on integration load or reload, not on each event. Use a head
with a stable HA entity registry binding and unchanged Off reports. The warning
is still on for all owned native heads. There is no automatic re-enable.

1. Supervise one opt-in head. Verify the Controller status entity attribute
   `manual_head_handoff` shows only that opted-in role and no pending/held head.
2. Wait until the pilot head reports an active mode with no trial, recovery,
   or recent controller dispatch. Press Off once on the original remote.
   Look for both the diagnostic warning and the separate ownership-release
   message. Confirm that Controller status names exactly the pilot head as
   manual. Verify Home Climate sends no Off, Auto, target, or fan command to
   the released head. Unverified storage or native work in flight does not
   count as a release.
3. If safe, turn the pilot head on using the same mode as an already-running
   managed sibling, then observe that continuation stays compatible. A
   contrary or untrustworthy manual report should block new shared activation
   and enter ordinary Off for **managed heads only**. Never force an
   incompatible mode on the hardware just to test. A new shared start needs
   fresh manual Off through the normal settle. Terminal Auto and guarded
   reserve-split cooling are unavailable while any head is manual.
4. While a manual head exists, switching Controller Off stops its remaining
   owned heads and auxiliary according to their normal negative safety
   checks, but skips positive terminal presets. It does not command the
   manual head. Wait for shutdown to finish, send Off from the pilot head’s
   own remote, and wait for fresh native Off. An HA administrator can then
   call `house_climate.resume_manual_head_control` with the head’s
   `entity_id` and `confirm: true`. This removes only that disposition and
   sends no equipment command. Use ordinary Controller On afterward. The
   **Controller Off** requirement deliberately prevents reacquiring the head
   amid queued or running shared writes.
5. Remove the role from the allowlist (or delete the file) and reload if you
   want to turn off future automatic handoffs. This does not release an
   already-persisted manual hold. A renamed or replaced native entity is
   deliberately not reclaimed automatically. Resolve it while supervised.

Unchanged, repeated HA feedback can still be a bridge’s cached report. A
physical remote is independent of HA, so an external action may reach the
hardware before HA can observe it and stop managed heads. This is why only
opted-in heads move to manual ownership and initial testing is supervised.
