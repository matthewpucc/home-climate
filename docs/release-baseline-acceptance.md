# Release baseline acceptance

This is the acceptance protocol for the configurable deterministic control
baseline. It is a checklist of required outcomes, not evidence that a candidate
is released, installed, or ready to operate equipment. Record results against an
exact commit and installed artifact. Use the current configuration when a value
is configurable; do not infer installed settings from defaults in this document.
The candidate version is 0.9.0; publication and live acceptance must be recorded
separately.

The planned supervised session is November 2, 2026, 12:00–15:00
`America/Los_Angeles` (20:00–23:00 UTC). The delivery target is November 3 at
12:00 local time (20:00 UTC), with the agreed version deployed and running.
These dates do not authorize unattended testing. The previously proposed
overnight trial remains canceled. Future experiments follow the separate
[thermal experiment protocol](thermal-experiment-series.md).

## Candidate record

Keep the completed record with the deployment notes, outside committed examples
if it contains private entity mappings or live snapshots.

| Field | Evidence to retain |
| --- | --- |
| Code | Commit SHA, PR, reviewed diff, unresolved issues |
| Checks | Exact-commit CI run and all required job outcomes; local test commands and counts |
| Distribution | Version, release/tag commit, asset filename and SHA-256, packaged-source test result |
| Installation | HACS/installed version, HA version, integration load result, configuration revision |
| Ownership | Controller/live state, auxiliary manual ownership, pending commands, retained cut/fault obligations |
| Sources | Physical-source map, units, corrections, quality restrictions and timestamp semantics |
| Notifications | Normal and critical routes, capability evidence, test outcome and remaining delivery uncertainty |
| Recovery | Configured recovery targets, tested handoff outcome, compatible rollback artifact and procedure |

No successful service call, optimistic state echo, or previously passed
commissioning test substitutes for the applicable current evidence.

## Readiness before the session

- [ ] The intended candidate is reviewed, its offline checks pass, and its
  installation and rollback steps are prepared. Any approval still required for
  publication or installation has been obtained.
- [ ] The operator is present and has an independent way to observe and stop the
  equipment. The session's room limits, off-limits rooms, maximum durations and
  end state are explicit.
- [ ] Compare the installed version and configuration with the candidate record.
  Preview plans must identify themselves as plans, not actual heater operation.
- [ ] If generated HA YAML is installed, regenerate it for the new independent
  auxiliary-authorization helper and configured lease limits. Confirm the intended
  house/generic dynamic-stop contract; an old watchdog is not a compatible substitute.
- [ ] Read fresh controller and auxiliary-guard diagnostics. Account for pending
  and uncertain commands, retained cut ownership, binding changes, storage errors
  and shutdown evidence. Do not enable Live, edit persistence, cycle a relay or
  repeat acknowledgements merely to bypass a recovery gate.
- [ ] If manual auxiliary heating is still in use, agree when it can stop before
  attempting recovery that requires native Off and low dedicated load. A Heat
  mode reporting low watts is not native Off. Resolve retained ownership through
  the supported acknowledgement/physical-feedback path without inventing a new
  switch report.
- [ ] Verify the room-reference probes, physical locations and source identities.
  The shared head's external probe belongs only to its actual room. Do not count
  aliases as independent corroboration. The Living internal head probe remains
  excluded from normal fusion pending comparison.
- [ ] Confirm that recently recovered sensors continue reporting; one new sample
  after a battery replacement is not proof of sustained reporting. Resolve large
  probe disagreements before using a room as a test boundary. Account for a
  purifier probe's operating-state restriction if configured.
- [ ] Establish the Primary room ceiling and a preemptive stop margin for the
  session. A prior planning value was 71°F; it is a ceiling, not a heating target
  or a guarantee of room temperature. If already above the approved ceiling,
  do not begin a heating phase that could worsen it.
- [ ] Verify current outdoor inputs, window states, battery/supply indications,
  dedicated meter and actual supported head/fan modes. KSEA fallback must be a
  qualified current observation, with physical observation age distinguished from
  HA update time; do not use forecast data as an observation.
- [ ] Verify normal notification routing and the critical route's supported
  payload. A normal message with a critical-looking title is not proof of
  critical delivery. Do not send a test alert without authorization.

If these checks fail, use the session for diagnosis or the independently safe
subset. Do not record the untested remainder as accepted.

## Offline acceptance matrix

Use synthetic states and controlled clocks for failure, boundary and restart
cases. Do not reproduce unsafe conditions on equipment merely to test them.

| Requirement | Required evidence |
| --- | --- |
| Editable room policy | Schedule, band, priority, peak-window and recovery changes validate, save and reload without restoring obsolete fixed settings. |
| Weekly schedules | Start-day overnight ownership; approved weekend transitions; half-open endpoints; reject conflicting overlaps and verify the base profile in gaps; timezone and DST cases. Limits-only seeks the scheduled bounds, not a comfort target. |
| Seasonal Living profiles | Thirty-minute qualified average; warm above 61°F, cool below 59°F, retain at/between thresholds; startup, source switching, expiry and persistence; conservative freeze handling remains separate. |
| Room evidence | Explicit external head probes eligible; physical aliases deduplicated; shared probe assigned once; missing/stale/invalid sources and operating-state restrictions visible; no duplicate offset application. |
| Shared demand | Living head participates normally. Auxiliary supplements or supplies Living while the shared system cools, without taking Living permanently out of arbitration. Window comfort yielding works in both directions; actual protection remains. |
| Auxiliary policy | Automated reserve requires battery above 40% and qualified supply signals; zero pre-start output is inconclusive. No software input/output watt ceiling is reintroduced in admission, running, queued dispatch or recovery. Automated thermostat setting is 80°F. |
| Peak and off-peak | Default peak window 17:00–21:00 local: at least 1°F below the band for 15 minutes without meaningful recovery, or at least 2°F below without the full wait. Meaningful recovery is at least 0.5°F toward the band over 10 minutes of qualified observations; stop auxiliary heat 0.5°F inside the band. Missing trend is unknown. Off-peak help does not inherit the peak 1°F/15-minute prerequisite. Equipment timers still apply. |
| Manual auxiliary ownership | Explicit, durable takeover affects only the auxiliary; other comfort continues. Reserve cutoff and recovery preset do not reclaim the manually owned unit; low-reserve alerts and genuine fault obligations remain. Return is explicit and checks readiness. |
| Independent leases | Controller health renews separately from auxiliary authorization; defaults 300/120 seconds. Auxiliary supervision failure cannot be concealed by a healthy general heartbeat. Native/generated settings agree; stale owner generations cannot renew leases. |
| Command confirmation | Heat-pump activation retains 120 seconds. Shared-head changeover Off attempts at 0/15/30 seconds stop early on confirmation; failure is visible at 45 seconds and blocks changeover. Later safety cleanup is accounted for separately. |
| Auxiliary stop evidence | Native Off, electrical verification pending, corroborated stop and contradictory draw remain distinguishable. Existing five-second native deadline is preserved; late/unknown jobs and aggregate corroboration are handled without fabricating physical proof. |
| Fan-only during a heat hold | An ordinary hold permits circulation only after proved native Off, low dedicated load and restored reserve/supply. Faults, owned cuts, pending jobs and legacy cooldowns still block it; automated low reserve keeps the whole auxiliary Off. |
| Recovery handoff | Every exit from active ownership, including disable, graceful restart and fault, applies configured head/fan recovery where safe. Auxiliary manual ownership is respected. Test interruption at each persisted stage, partial failure, manual intervention and changed bindings. |
| Explicit recovery retry | Retry recovery settings requires Controller Off, saved authority, no pending head/fan work and fresh head Off after the failed-request/restart boundary. It durably retries the whole saved preset through normal timers, preserves auxiliary obligations and leaves Controller Off after completion. |
| Notifications | Severity selection, low battery during either ownership mode, handoff/shutdown failures, deduplication, recovery and delivery-error reporting; unsupported critical routing stays visibly unverified. |
| Migration | Existing bindings and storage load safely; ownership/faults are not discarded. Old ownership without saved preset authority retains Off cleanup only; a new successful live claim arms the preset. Invalid or ambiguous migration inhibits the affected operation with an actionable reason. |

Run the repository checks from [CONTRIBUTING](../CONTRIBUTING.md), including core,
native HA, release-publisher, configuration/CLI and packaged-distribution checks.
Record any timing-sensitive failure and its investigation; a passing rerun alone
does not explain a failure. Do not weaken relevant safety tests to fit the patch.

## One supervised hardware session

The following allocation is a planning budget, not a command schedule. Start only
after readiness is confirmed. Existing minimum-run, minimum-Off, direction
confirmation, dwell and settling rules determine what fits. Do not shorten them
to fit more cases into three hours.

| Session portion | Purpose |
| --- | --- |
| Opening, approximately 20 minutes | Confirm installed identity, fresh sources, actual room conditions, ownership and recording. Reconcile any outstanding supported recovery before live admission. |
| Middle, approximately 115 minutes | Complete a small number of useful operating cycles: heat-pump participation, auxiliary eligibility/assistance where naturally needed, ordinary Off observations, and a shared-direction transition when room conditions permit. Capture thermal data alongside acceptance. |
| Closing, at least 45 minutes reserved | Verify explicit auxiliary takeover/return where appropriate, normal recovery handoff, end-state feedback and notification outcome if authorized. Leave time to stop and diagnose rather than beginning a final unfinishable phase. |

Prioritize observable outcomes:

1. Correlate requested mode/setpoint with reported state and actual operation
   evidence. Do not demand active heating from an already-satisfied thermostat.
2. Observe several ordinary auxiliary shutdowns only when ordinary demand and
   equipment timers permit. Record command, native Off and meter-report timing;
   do not force repeated relay cycles or deliberate failed stops.
3. Confirm that an auxiliary-only hold does not suppress eligible head/fan
   comfort. Test reserve/stale-sensor/failure branches offline, not by draining the
   battery, disconnecting probes or obstructing equipment.
4. Verify the configured recovery state: normally heads in supported dual-setpoint
   operation at 68–73°F, auxiliary Off, other fans 40%. A manually owned auxiliary
   is excluded. Unsupported or unsafe targets must report incomplete handoff,
   not success. Graceful restart may be checked only if time and readiness permit;
   do not simulate a power failure. Confirm actual `heat_cool` capability and fresh
   feedback, and allow normal Off/settling timing. Do not deliberately create a
   failed handoff to exercise its retry button; cover that branch offline.
5. Finish with the explicit agreed ownership/end state. A late service result,
   incomplete stop or missing observation remains an open item even after the
   session clock ends.

## Release and deployment decision

After the session, replay captured cases through synthetic/offline tests, keeping
private data outside committed fixtures. Record the transformation to synthetic
fixtures and preserve the behavior that matters.

- A changed actuator behavior after the only hardware session creates a new live
  validation gap. Do not inherit the earlier session's result automatically.
- Verify the reviewed release commit, tag, workflow, downloaded assets and HACS
  source. Verify the actually installed version and active runtime independently.
- Confirm that ordinary operation is continuing with expected ownership, valid
  sensors, no unexplained pending jobs and the intended room policy. A successful
  install alone does not mean “deployed and running.”
- Keep a compatible rollback artifact and configuration backup. Before rollback,
  use a verified handoff or the applicable fault procedure; retain outstanding
  ownership and shutdown obligations. Restoring an old file or backup is not a
  way to clear a fault. Verify the resulting installed version and device state.
- Deliver the configuration/migration notes, acceptance results, known limitations
  and future experiment plan. If a required outcome remains unverified, identify
  it explicitly rather than claiming the delivery target was fully met.
