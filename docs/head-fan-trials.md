# Bounded heat-pump blower trials

This is **opt-in test plumbing, not an adaptive fan policy**. Installation enrolls
nothing and changes no blower speed. It does not control room purifiers, vane
angles, setpoints, demand, changeover delays, or auxiliary equipment. No Kumo
fork or integration changes are required.

## Scope and enrollment

`house_climate.start_head_fan_trial` requires explicit `confirm: true`, one
configured climate `entity_id`, every served `room_ids`, one currently advertised
`mode`, and a duration of 10–720 minutes. The current reported fan mode is the
restoration baseline. All served rooms must have at least two qualified sources
before dispatch. A shared head requires joint room acknowledgment. Unknown
capabilities, disabled/shadow control, unhealthy thermal recording, or an
unresolved prior trial block enrollment.

`sleep_sensitive` defaults to true and `overnight_authorized` to false. By default
the entire duration plus restoration margin must fit between 09:00 and 22:00 in
HA's timezone. Overnight enrollment requires explicitly declaring the served
spaces non-sleep-sensitive and authorizing overnight operation. Do not use that
exception for occupied sleeping rooms. The initial operational trial should be
reviewed for a non-Primary room, not automatically selected from room names.

Enrollment waits for a **natural heat/cool call**. It neither starts nor prolongs
that call. After ordinary HVAC processing, it makes at most one experimental
fan request while the head has no pending HVAC/recovery/service work. The
native bridge rechecks ownership, availability, capabilities, deadline, current
HVAC direction, interlocks, and the trial reservation immediately at dispatch.
A command/persistence error never triggers an experimental retry.

The deadline is an upper bound, not a promise to run that long. Natural call
completion, cancellation, or lost recording health requests baseline restoration.
`house_climate.cancel_head_fan_trial` cancels unused enrollment or requests that
restoration. Ordinary HVAC/Off and safety remain higher priority.

## Evidence and restoration

The native service returning is **not physical acknowledgment**. Kumo may update
its cache optimistically. The request state machine can report
`reported_consistent` only for distinct cached HA updates; physical confirmation
always remains `unknown`. No RPM or capacity measurement is implied.

Restoration is one journaled service attempt. A later baseline report can mark
`complete` with `baseline_reported_physical_unverified`; lack of that report in
60 seconds is `needs_attention`, not successful cleanup. An uncertain request or
restore is never blindly replayed. A user/other automation fan service supersedes
the trial, even if it requests the same value; a return from observed trial mode
to baseline also ends it without overwriting the external choice.

Before the first experimental write, a separate blower-Auto cleanup obligation
is saved in native ownership storage. On shutdown, lost ownership, or restart,
the experiment is never resumed. The native watchdog uses its existing shared-
head Off and pending-job barriers, restores the affected blower to `fan_mode:
auto`, and applies the normal saved HVAC fallback (`heat_cool` / Auto with the
saved low/high temperature range). These are **two different settings**.
Neither setpoints from the experiment nor a stale experimental fan command are
replayed. Ordinary end/cancel still restores the pre-test fan baseline.

An interrupted trial inhibits saved Controller-On takeover until recovery has
finished; the controller remains stopped in native HVAC Auto rather than
resuming the experiment. Re-enable ordinary Home Climate control explicitly
when ready. Automatic recovery is not an absolute guarantee: offline equipment,
missing Auto capability, changed bindings, unresolved service jobs, or an
interrupted recovery command without a completion receipt retain an actionable
obligation. Fresh Auto reports can reconcile an interrupted write; ambiguous
writes are not blindly replayed. Blower failure does not withhold an otherwise
safe HVAC Auto handoff, but final cleanup ownership remains until resolved.

Later observed fan services take precedence, including same-value services.
Explicit user HVAC changes inhibit the shared-system fallback rather than
being overwritten; independent auxiliary safety Off remains active. Changes
made while HA is down, particularly same-value changes, cannot reliably be
distinguished from the experimental setting. Fresh reported state is checked,
but this is not a claim that every offline manual action is observable.

If recovery cannot finish, inspect the reported blocker. The existing
`house_climate.acknowledge_head_fan_restoration` remains a human-review tool,
not an equipment command or physical acknowledgment. Never use it to bypass an
unresolved native handoff obligation. After inspecting the equipment and
restoring fresh shared-head Off, the existing explicit recovery-retry action
can renew the normal Auto handoff; it does not restart the experiment.

## Persistence and lookback

The bounded HA Store journal `.storage/house_climate.<entry_id>.head_fan_trial`
contains enrollment, baseline, exact expiry, configuration fingerprint, ownership
generation, and the durable pre-dispatch stage. It is included with the HA config
in system backups. An interrupted reserved write is not replayed after restore.

Compact lifecycle events and current `head_fan_trial` evidence are also recorded
in Home Climate's SQLite decision history. Trial-only events do not manufacture
room samples or split demand episodes. Normal decision context includes trial
identity so comparisons do not silently mix baseline and intervention. Normal
retention and truncation limitations still apply. The status sensor and downloaded
diagnostics expose current intent, uncertainty, and restoration blockers.

Optional fan work has a bounded worker wait. Journal failure or abandonment
blocks experimental dispatch without shutting down normal climate control.

## Next stage

A separately approved room trial and comparison protocol still need to specify
baseline duration, candidate mode, comfort/noise stop conditions, and analysis.
Learning, repeated experiments, near-target fan modulation, automatic room
selection, and temperature-setpoint changes are not implemented by this PR.
