# Thermal experiment series

The purpose is to collect real observations that improve comfort, efficiency and
shutdown-evidence decisions. No values are fabricated. Learned predictions remain
advisory until validated against independent observations; the deterministic
controller remains the operating baseline.

This document prepares experiments. It does not arm an automation or authorize an
active overnight run. The earlier overnight trial remains canceled. Each active
run requires an approved plan, current readiness, locally enforced bounds and an
explicit end state. Passive read-only analysis can continue without operating
equipment or silently changing policy.

## Questions and evidence

| Question | Useful comparison | Major confounders |
| --- | --- | --- |
| How long can Living coast within its band? | Time from a known inside-band starting state to a preemptive boundary, with both local heat sources inactive | Outdoor temperature/trend, windows, sun, occupancy, adjacent rooms, ventilation, sensor lag |
| How quickly can the heat pump recover a room? | Qualified temperature response and circuit energy during a known shared-system direction | Other heads using capacity, equipment timers, setpoint strategy, defrost, source placement |
| When does auxiliary assistance earn its energy cost? | Matched heat-pump-only and assisted episodes under comparable starting conditions | Different room deficits/weather, shared cooling demand, battery charging, unrelated loads |
| When is a reported auxiliary Off electrically corroborated? | Command dispatch, native Off, dedicated-meter observation/receipt, relay state | Meter averaging/report cadence, cached values, fan power, optimistic feedback, scrape resolution |
| Can predicted band crossing improve arbitration? | Predictions made before an episode versus the subsequently observed crossing or censored run | Limited seasonal coverage, changing control rules, correlated samples, extrapolation |

Use a dedicated auxiliary meter for auxiliary energy. Aggregate battery output
includes other loads; input power includes charging and other behavior. A shared
heat-pump circuit measures the shared system, not an individual head. Do not
attribute the full shared circuit to one room without an identified allocation
method and its uncertainty.

## Experiment record

Store observations privately. Committed examples and replay fixtures must use
synthetic entity IDs and sanitized data, following [CONTRIBUTING](../CONTRIBUTING.md).

Each run records:

- Experiment ID, phase, absolute start/end timestamps with timezone, candidate
  commit/version, configuration revision and operator authorization.
- Physical source identity, entity aliases, units, corrections, location and any
  operating-state qualifier. Record unit-offset changes as a new measurement
  regime; do not blindly combine or double-correct earlier data.
- Raw individual room probes and the controller's fused result; active target,
  lower/upper bounds, demand, shared mode and reasons for holds.
- Local outdoor data, selected fallback/source tier, windows, fan settings,
  manually recorded occupancy/door/sun changes where practical.
- Requested command and actual dispatch timestamp, service outcome, native
  reported mode/setpoint/action, and applicable electrical evidence.
- Dedicated auxiliary watts, shared heat-pump circuit watts, battery percentage,
  AC-output status and aggregate power as separately labeled measurements.
- Ownership, manual takeover, pending/uncertain jobs, faults, stop/recovery stage,
  notification outcome, and why the run ended.

Keep these times distinct:

| Time | Meaning |
| --- | --- |
| Device acquisition/window | When the instrument measured the value, or the interval over which it averaged it; record only if actually available |
| Integration receipt/report | When the integration received or reported the measurement; identify the source of this timestamp |
| HA state update/report | HA's `last_updated` or `last_reported`; an unchanged state can have different semantics for these fields |
| Collection/scrape | When the recorder or Prometheus read HA's exported value |
| Command dispatch | When the command was actually sent, distinct from queueing or policy decision |

A repeated cached scrape is not a new physical sample. `last_updated` can change
because an attribute changed; it does not necessarily date the temperature
measurement. Preserve missing values, gaps and out-of-order reports. Do not fill
gaps in a way that creates fictitious observations or apparent deadline success.

## Readiness and local abort contract

An active experiment plan must supply the following fields before arming:

| Field | Required value |
| --- | --- |
| Scope | Named controlled devices and rooms; unrelated controls remain untouched |
| Ownership | Controller or explicit manual auxiliary owner, plus how takeover aborts the relevant phase |
| Time | Absolute allowed window, phase maxima, hard end and latest start allowing cleanup |
| Temperature | Per-room approved lower/upper bounds, preemptive margins and qualified boundary probes |
| Evidence | Required source ages, acquisition/report semantics, minimum coverage and disagreement handling |
| Operations | Allowed commands, ordinary control/timer constraints and independent observation/stop method |
| Abort | Local triggers, cleanup sequence, persistent state and notification route |
| End state | Explicit final device settings and ownership, including exceptions for manual takeover |

Use the currently approved room limits, not an unimplemented schedule or a stale
snapshot. The Primary ceiling discussed for the first supervised session was
71°F; it is not an automatically authorized limit for all future nights. Choose
and validate preemptive margins for each run. A thermostat limit cannot guarantee
the temperature at a different room probe.

Local aborts must cover: approaching a room bound; loss of qualified boundary
evidence; unacceptable probe disagreement; window/door conditions invalidating
the phase; unexpected mode or load; failed command/stop; missing supervisory
lease; manual takeover; and the absolute end time. Relevant hardware protection,
shutdown escalation, minimum cycles and shared-direction barriers remain active.

If the experiment uses automated phases, its tested local supervisor must retain
phase/cleanup ownership, reject stale queued commands, abort on restart and
complete or visibly retain unfinished cleanup. It must not resume heating solely
because HA restarted. A long delay or an end timer alone is not sufficient when
HA might be unavailable at expiry. A supervisor inside HA cannot execute during
an HA outage; any independent protection must be verified rather than assumed.

No autonomous experiment supervisor is implied by this release. Until a suitable
supervisor is implemented, reviewed and validated, active phases are supervised
manual sessions only. Do not rely on a remote assistant's polling to enforce
room limits or the end time.

## Staged collection plan

Progress only when the preceding stage supplies usable evidence. The stage names
are not scheduled nights; weather, occupancy and readiness determine when a run
is appropriate. Keep changes small enough to identify their effect.

### Stage 0 — passive baseline

Observe ordinary operation with no experiment commands. Confirm entity identity,
units, physical-source aliases, updates under stable values, recording coverage
and fan-state effects. Mark heating/cooling/idle/manual periods and software or
offset changes. Identify comparable episodes before proposing an active run.

Deliver a coverage report: observation span, gaps, unique physical reports where
known, eligible room references and unresolved source issues. Availability true
or a recently replaced battery is not sufficient by itself.

### Stage 1 — ordinary auxiliary Off observations

During naturally justified, supervised heating, record several ordinary Off
transitions without cutting the dedicated relay merely for measurement. Include
Heat-but-idle and fan-only baseline observations when naturally available. Do not
turn heating on just to produce a shutdown if the room lacks safe headroom.

Measure command-to-native-report and command-to-meter-report separately. Claim
physical stop latency only if acquisition timing or independent instrumentation
supports it. A roughly 15-second exporter cadence cannot establish a five-second
physical deadline. An intermediate high report may represent an averaging window
or delayed report; investigate its semantics before classifying it.

Deliver individual event timelines, sample bounds and the source contract. Use
them to propose an electrical-verification policy, not to silently change the
existing five-second native-Off deadline or suppress genuine contradictory draw.

### Stage 2 — heat-pump-only recovery

Choose an ordinary small room deficit within approved limits. Exclude auxiliary
heating for the phase through the supported owner/control path, not a guard
bypass. Avoid introducing unnecessary opposing demand in other rooms. Record
all participating heads, actual power and each room's response.

Finish at the agreed inside-band condition or abort boundary. Keep minimum run,
minimum Off and shared-mode timers. Distinguish a head reporting Heat from actual
capacity delivery. Repeat under comparable conditions before assigning a stable
recovery rate.

### Stage 3 — passive Living coast

Begin from a documented point inside Living's active band. Confirm both local
heat sources inactive with the applicable evidence. Let other rooms cool only
when they already need it and shared-system constraints permit; do not create an
artificial cooling load to make Living drift faster.

End before the approved lower/upper excursion limit. If the run ends for time,
manual intervention or another abort before crossing, record it as a censored
observation: the actual time to cross is unknown. A run that starts outside the
band cannot establish time to drift out of that band.

### Stage 4 — cooling recovery

Observe a room that actually needs cooling, with initial deficit and all shared
loads documented. Hold unrelated experimental inputs constant when possible.
Record recovery into the band and energy, without forcing the room warmer first.
Compare episodes only after accounting for open windows, other heads and changing
outdoor conditions.

### Stage 5 — auxiliary contribution and energy

Use ordinary demand to compare qualified heat-pump-only and assisted episodes.
Where appropriate, observe auxiliary-only Living heat while the shared system
cools other rooms. Do not simultaneously force unnecessary heating and cooling
for a benchmark. Preserve the current reserve policy under automatic ownership;
manual takeover is not an experimental shortcut around it.

Integrate measured power over time using a documented method and gap policy.
Report watt-hours, duration, room-temperature change and uncertainty together.
State whether fan/standby energy is included. Do not infer heat output, COP or
money saved directly from electrical watts without the additional evidence and
tariff assumptions needed for those claims.

### Stage 6 — repeat and expand coverage

Repeat useful phases across comparable nights, then across materially different
outdoor conditions and seasons. Vary one factor at a time where practical. Keep
independent later episodes for validation rather than fitting and judging a
model on the same observations.

## Analysis and proposed fixes

Each read-only analysis report should contain:

1. What was observed, with source and configuration identity.
2. Which episodes were excluded and why; source gaps and timing uncertainty.
3. The range of conditions represented, sample count by independent episode and
   whether any crossing/recovery event was censored.
4. A deterministic replay or advisory prediction, its uncertainty and the
   conditions under which it should not be used.
5. A proposed change with a concrete expected benefit, counterexample/risk and
   offline regression cases.
6. Whether further data or a supervised verification is needed before adoption.

Report finite-sample ranges without presenting them as guaranteed worst cases.
Repeated scrapes of one reading do not increase the independent sample count.
Do not extrapolate a cool-weather episode into summer performance or assume one
three-hour session learns the building's thermal behavior.

Keep learned output advisory until the policy change is reviewed, tested and
approved for deployment. Analysis may recommend a fix; it must not automatically
change live thresholds, schedules, ownership or equipment operation.
