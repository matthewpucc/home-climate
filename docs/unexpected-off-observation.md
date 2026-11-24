# Unexplained heat-pump Off observations

Home Climate logs a warning when a head it currently owns reports an
unexplained transition to Off. This is on by default for live, native control.
It never pauses a head, releases ownership, writes a thermostat command,
changes shared direction, or sends a push notification. There is no live
handoff option in this change.

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
   attempt. Neither should log when recognized by the observer. This release
   does not make the remote take control; Home Climate can still turn the head
   back on as part of normal policy.

A separate opt-in live pilot needs a persisted per-head ownership boundary,
a write-drain/fence that includes queued trial and cleanup work, conservative
shared-direction arbitration against manual heads, and explicit scoped resume.
