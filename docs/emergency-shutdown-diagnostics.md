# Guard and outside-HA shutdown observations

The native auxiliary diagnostics also include 28 numeric shutdown sensors when
Home Climate's power guard is configured. They supplement the worker/watchdog
stream and use a separate bounded 16-event window. Neither stream is a control
input. No helper entities, policy changes, live configuration changes or external
instrumentation are required.

## Known actors and unknown callers

A `guard_*` action records the known executor as `home_climate_guard`. Its immediate
caller is `unknown`: a person, automation, or assistant could have requested a stop
that the guard then executes. The observer does not upgrade a local executor into
proof of who originally requested it. A first guard latch captures its actual trip
time and bounded reason, such as battery reserve or contradictory shutdown evidence.
Guard Off jobs capture their reason and stage at queue time and retain that original
context through later completion. A queued job's reason describes the guard's
current status, not a proven trigger supplied by an outside caller. Guard operations include ordinary shutdown and
maintenance cleanup as well as emergency actions; a guard trip alone does not prove
that any command was sent, particularly in Preview.

The observer also listens to HA's existing service-call events, limited to explicit
entity IDs matching the configured native heater, dedicated relay, or shared AC
switch. It recognizes `climate.set_hvac_mode(off)` and `turn_off` for climate,
switch, or homeassistant domains. These `ha_off_requested` rows always have
`initiator`, `reason`, outcome and emergency intent unknown, except that a boolean
records whether HA supplied a user context. A shared HA token or context does not
identify an assistant. Local guard commands also produce HA service events; the
HA-observed and locally instrumented counters are separate views, not additive
incident counts. They are not deduplicated by guessing from timing.

Fresh, non-restored, non-assumed Off state-change reports are recorded separately.
A request can be paired temporally only when the report follows it within 180
seconds; otherwise an observed transition to Off has no request attribution.
`context_matches_request` says only whether HA propagated the same context. It
does not identify the actor or prove physical success. A temporal pairing has
`causality: not_established`, and external service completion remains unknown.
Only the most recent direct request for each of the three roles is retained for
matching; an older concurrent request cannot be reliably identified. Same-value
reports that do not raise HA state-change events are not individually journaled.

An external assistant action is therefore captured as an unattributed HA request
or observed Off transition **when HA exposes one**. Device/app actions bypassing HA
services have no known command outcome or trigger; a state transition may still be
observed. Changes while the integration is stopped, targets expressed only as
areas/devices/groups/all without a matching downstream explicit-entity event,
and actions on other equipment are outside this observer. Direct electrical
telemetry does not identify who caused the load change. The integration cannot
claim to identify every external assistant emergency action.

## Event and field contract

Each event below has native `shutdown_<event>_count` and
`shutdown_<event>_at` sensors. Counts are cumulative for the current integration
session; timestamps are the last observed occurrence, numeric Unix seconds with
fractions. Event sequence preserves ordering across wall-clock adjustments.

| Event | Meaning |
| --- | --- |
| `guard_trip` | First guard latch with the actual stored trip time and trigger/reason. |
| `guard_requested` | A native heater, dedicated relay, or shared AC Off job queued by the guard. Positive restoration commands are excluded. |
| `guard_dispatched` | Entry to the actual HA service await, after any pre-dispatch check passes. |
| `guard_completed` | Actual job returned successfully; no claim of device acceptance. |
| `guard_failed` | Actual job raised, including a pre-dispatch rejection. Compare with dispatch evidence; a failed queued job need not have reached HA. |
| `guard_cancelled` | Actual job ended cancelled. An integration ignoring cancellation remains pending and can later complete successfully. |
| `guard_native_ack` | Existing guard logic accepted native Off evidence for the current request boundary. Can precede handler completion; counted once per boundary. |
| `guard_evidence` | A changed guard evidence/confirmation category, including initial, unknown, contradictory, dedicated low-load, or isolation observations. This is an evidence transition, not an emergency counter. |
| `ha_off_requested` | HA reported an explicitly targeted Off service request; initiator, trigger, completion and emergency intent are unknown. |
| `ha_off_reported` | A trusted observed HA Off state transition/report, with optional bounded temporal request association. No physical or service-completion claim. |

The other eight sensors are:

- `shutdown_sequence`: event sequence, with a compact last-event attribute and
  explicit unknown external attribution. Detailed history is download-only.
- `shutdown_observation_errors`: best-effort observation failures; these cannot
  withhold safety commands. A nonzero value signals a diagnostic gap.
- `shutdown_sampled_at`: time the existing guard diagnostics were captured, not a
  fabricated physical sample time.
- `shutdown_isolation_confirmed`: 1/0 from the guard's existing confirmation.
  Zero means not confirmed, not proof that power is present.
- `shutdown_electrical_stop_confirmed`: the existing dedicated-meter stop verdict;
  unknown when the meter or its reading is unavailable. Low dedicated load is not
  a measured before/after power drop.
- `shutdown_dedicated_load_w` / `shutdown_dedicated_load_a`: qualified dedicated
  load in its actual configured units, never a fabricated A-to-W conversion.
- `shutdown_aggregate_drop_w`: difference only for the guard's qualified `drop`
  or `relative_drop` classification. Relative drops use `qualified_baseline_w`,
  not the single start sample. `already_low`, pending, unknown, inconclusive and
  missing baselines remain unknown. This is whole-supply corroboration, not a
  heater-only power measurement or proof of causation.

Evidence events preserve the available aggregate classification, baseline, qualified
baseline, latest value and qualification duration. Guard request boundary times
are historical fields and may be restored by the existing safety storage; they do
not imply a request was observed in this diagnostic session. No new persistence is
added. Captured load values also require the guard's reading evaluation to be no
older than 30 seconds. Current electrical/isolation sensor values become unknown
when the captured snapshot is over 30 seconds old or future-dated. Historical
counts/events stay historical. Sensor refresh uses the captured values and never
calls guard safety getters or evaluates a shutdown decision.

All counts, matching slots and history reset with the guard's integration session.
Recorder/Prometheus retain previously collected values; missed samples or events
before a restart can be lost. The two history windows hold at most 32 events total,
and request matching holds at most three roles. Reasons are truncated to 256
characters and can contain installation-specific text. Raw service payloads and HA
context/user IDs are not retained in downloadable events or metric labels.

## Direct export

The native entities use the same device and filter pattern documented in
[auxiliary diagnostics](auxiliary-diagnostics.md). Default HA Prometheus families:

- Counts, sequence and flags: `homeassistant_sensor_state`.
- Numeric Unix times: `homeassistant_sensor_unit_s`.
- Dedicated watts and aggregate drop: `homeassistant_sensor_unit_w`.
- Dedicated current: `homeassistant_sensor_unit_a`.

Entity ID selects the field. Reasons, initiators, target roles, contexts and request
IDs never become metric labels. Unknown values remove prior numeric series through
the real HA exporter. All new fields are tested through real native entities and
that exporter; no production scrape or hardware test is performed.
