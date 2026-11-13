# Fan-first investigation and staged implementation

## Stage 1: deployed observation foundation

The plan contains `head_fan_investigation`, grouping currently advertised head
fan modes and cached reported settings by configured head and served rooms.
It carries source counts and the qualified low/high room spread, not a new
room average. The existing `rooms` source evidence remains authoritative;
there is no duplicate source stream or claim that HA writes are physical samples.

`physical_confirmation` is always `unknown`. A returned HA fan mode, including
one seen after a service call, can be an optimistic provider cache. Neither it
nor a Prometheus scrape proves a successful physical poll or blower RPM.

There are no fan service calls, candidate mode requests, learned calibration,
parameter updates, new native entities, or changes to temperature policy. The
executor ignores this evidence. This stage cannot start a live trial.

The pure experiment-window diagnostic rejects 22:00–09:00 in the configured
local timezone, as well as invalid/naive clocks and invalid duration estimates.
It requires room for the full proposed trial plus a restoration margin before
22:00, not merely permission to start. Its default five-minute margin is a
provisional investigation bound, not proof a future cleanup can complete.
Daytime eligibility is not live authorization or evidence sufficiency. Safety,
Off, manual commands and normal baseline comfort control are never time-gated.

## Investigation

1. Verify per-head capability lists, installed integration/library versions,
   physical poll semantics, all room probe identities and acquisition cadence.
2. Compare natural Auto heating, cooling, and idle episodes separately. Measure
   individual-probe response lag, rate, post-stop coast, spatial spread, band
   violation degree-minutes, recovery time, noise/draft reports and reversals.
3. Segment evidence on placement, bindings, firmware, schedule and control
   changes. Do not automatically offset probes to erase real spatial gradients.
4. Match episodes by outside conditions, other active heads and internal loads.
   Repeated cached samples are not independent observations. Mark missing
   physical-poll evidence and interruptions explicitly.

Room context must be configured, not encoded by display name or manufacturer.
Shared indoor heads require joint evaluation of every served room. Electronics,
refrigeration and server equipment can create time-varying internal loads.
Oversized equipment may hit minimum capacity even at reduced fan speed. Exposed
open-plan spaces need multiple spatial probes and wind/outdoor context before
concluding that a local reading represents the whole served floor.

## Stage 2: command/readback contract and tests

Add a distinct per-head fan intent to the policy and executor. Do not reuse
purifier percentages or overwrite existing pending HVAC/setpoint/Off jobs.
Validate current per-head advertised capabilities at dispatch; serialize writes,
cancel superseded requests, and preserve manual ownership and Off priority.

Inspect the installed provider: upstream Kumo/pykumo may update fan cache on send.
If no trusted successful post-command physical poll marker exists, retain an
explicitly unverified physical status in the Home Climate-side contract. Do not
modify or fork Kumo/pykumo; any upstream proposal requires a separate discussion.
Record requested and cached-reported settings separately; neither is measured RPM.

Add bounded retries, failure diagnostics, restoration intent and ownership across
restart. A future dispatcher must recheck the local time window immediately
before execution, cancel queued experimental work at cutoff, and restore the
owned baseline before the applicable trial cutoff. Cleanup must be bounded and verified; unresolved
cleanup prevents further trials and is surfaced. Manual takeover supersedes
restoration. Cleanup must not delay safety shutdown or restart equipment.

## Stage 3: shadow policy, then a separately approved pilot

Start with a dedicated-head room with qualified spatial probes. Compare Auto
against a moderate supported lower fan setting near the existing cooling
completion boundary; do not assume lower airflow improves mixing. Keep targets,
offsets, timers, schedules, vane direction and purifier settings unchanged.
Use natural demand only, at most one normal experimental transition per call,
with Auto for large recovery demand or insufficient evidence. Select the entry
threshold from observed noise, lag and available margin, not a fixed guess.
Never prolong a call for data. Insufficient comparable calls means inconclusive.

No experiments or automatic learned selections during sleep until explicitly
approved. This includes promotion of a daytime candidate into overnight control.

## Stage 4: persistent room learning

Persist versioned, room/direction-specific models, source/configuration identity,
independent episode counts, uncertainty and last-known-good parameters using HA
storage. Validate on later independent episodes. Reduce confidence after drift,
sensor relocation, unsupported weather or prediction error; return to baseline.
Choose only among an approved finite mode envelope and retain deterministic raw
extreme guards. Jointly optimize shared indoor heads without changing the shared
outdoor unit's direction arbitration in this phase. Heating is a separate model.
Parameter learning should not require reloads or repeated code edits.

## Acceptance and publication

Pre-register success thresholds before a trial. Seek repeatable improvement in
overshoot/oscillation without worse spatial gradients, band violations, recovery,
noise or safety. Controller calls are not compressor cycles; do not infer energy
savings without appropriate independent metering. Validate stop/restart, queued
commands, manual takeover, stale/cached telemetry, unsupported modes, DST and
boundary-crossing behavior. Publish an incremental draft for review; deployment
and live trials are separate decisions.

## Home Climate-only command evidence contract

The bounded `controller/head_fan_command.py` lifecycle is a pure component,
not an enabled actuator path. It has no Kumo imports, private Kumo attribute
access, service calls, automatic retry, or physical-poll claims. The runtime
must not treat its output as authorization or as evidence of physical RPM.

A candidate request requires explicit authorization and current ownership,
current per-head capabilities, a known baseline fan setting, active heating or
cooling, clear safety gates and no competing HVAC work. Its entire deadline
plus restoration margin must fit within the configured trial scope. It
reserves at most one request and separates service outcome from observation.
A rejected request terminates; an uncertain service outcome is observed without
blind resending. An expired deadline remains unconfirmed.

Two distinct HA state-event reports with the requested value, separated by the
settling interval, may yield `reported_consistent`. Repeated reads of the same
event do not count. This remains cached-HA corroboration, **not** independent
post-command device acknowledgment. An event timestamp or Kumo coordinator
success cannot upgrade the confidence label. Sensor and acquisition metadata
must keep those limitations visible in episode history.

Ownership or plan generation changes, safety/HVAC work, loss of capability,
Off/unavailability, a third fan setting, or the applicable trial cutoff cancel the
request. The lifecycle never restores a setting after cancellation: blindly
restoring could overwrite a manual command or race an Off operation. A future
runtime adapter must implement serialized, ownership-aware baseline restoration
before the applicable trial cutoff, with explicit uncertainty if that cannot be achieved.
Protection, Off, manual takeover and normal control must not wait for fan work.

Before enabling a pilot, the runtime adapter must recheck all gates immediately
at dispatch, bind event identities to the correct head, coordinate with the
existing pending-HVAC executor, store outcomes in thermal history, and survive
restart without replaying a previous request. This implementation does not
supply those live behaviors. A supervised pilot using HA corroboration rather
than physical-poll proof is a separate acceptance decision; no experiment is
authorized by a `reported_consistent` result.


### Revised overnight scope

Passive overnight observation is allowed. A future concrete trial can opt a
non-sleep-sensitive head into overnight use only with explicit authorization
and an aware, hard end timestamp. Every room served by a shared head must be
classified; the safe default is sleep-sensitive. Initial Primary bedroom
experiments remain daytime-only. This lifecycle does not add a UI setting or
authorize any live test. The existing observation snapshot retains its
conservative daytime diagnostic until a runtime trial adapter is implemented.

Any supplied trial end applies during daytime too. The request deadline and
five-minute cleanup reserve must fit before it. Service completion cannot renew
the original deadline. Matching reports must be newer than service completion
(a conservative evidence boundary); they remain cached corroboration, not proof
of an actual physical poll. Restoration/dispatch integration is still pending.
