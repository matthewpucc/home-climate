# Native metrics reference

This is the complete native Home Climate export contract, checked against the
entity definitions and Home Assistant **2026.10.4** with **prometheus-client
0.21.0**. It covers existing sensors, new diagnostics, native controls, and HA's
automatic export families. It does not inventory unrelated integrations, the
optional generated standalone helper package, or Prometheus process/runtime
collectors. No helpers or exporter extension are required.

## Reading the inventory

The tables use implementation keys, not promised entity IDs. Existing auxiliary
and shutdown sensor unique IDs are `<entry>_auxiliary_diagnostic_<key>`. New
diagnostics use `<entry>_numeric_<key>`. `{fan}` is the configured fan entity ID;
`{source}` is the stable 16-hex digest of the configured room/source identity
(outdoor uses a null room). One source family exists per physical source, with
aliases deduplicated; names show the configured room and source. Removing a
configured fan/source removes its obsolete diagnostic entities on reload.
Find the actual entity ID in HA's device page or entity registry: names, prior
renames, duplicate installations, and registry suffixes can change it.

Every numeric sensor is a **Prometheus gauge**, even the cumulative event counts.
Cumulative event/error counts and sequences use HA `total_increasing`; the current
`pending_jobs` count and other new diagnostics have no state class. All new sensors
are diagnostic entities enabled by default. Entity
IDs select meanings within the families below. No reason strings, event IDs,
timestamps, generations or profile names become metric labels. Entity counts
grow only with configured rooms, fans and physical sources, never with events.

| Unit/type | Default Prometheus family |
| --- | --- |
| Unitless flag, count, sequence, numeric code | `homeassistant_sensor_state` |
| Numeric Unix UTC seconds (`s`) | `homeassistant_sensor_unit_s` |
| Percentage (`%`) | `homeassistant_sensor_unit_percent` |
| Degrees (`°`) | `homeassistant_sensor_unit_u0xb0` (HA escapes the degree symbol) |
| Watts (`W`) | `homeassistant_sensor_unit_w` |
| Amperes (`A`) | `homeassistant_sensor_unit_a` |
| Room temperature (temperature device class) | `homeassistant_sensor_temperature_celsius` |

These are defaults; HA `namespace`, `default_metric`, `override_metric`, and
component configuration can change names. Temperature sensors convert Fahrenheit
to Celsius in this exporter. Numeric Unix seconds deliberately have no timestamp
or duration device class; HA preserves about 10 microseconds at contemporary
epoch sizes (15 significant digits), not exact floating-point timestamps.

Unknown is **not zero**. HA removes a previous numeric series when the entity
becomes unknown/unavailable and sets `homeassistant_entity_available` to 0. A
real reported zero remains exported. Do not use `or vector(0)` to turn absent
evidence into a negative verdict. Monitor scrape health as well as availability.

## Refresh, freshness and restart

New diagnostics share the existing group subscription: status updates and a
15-second interval, not one timer per sensor. The existing shutdown group keeps
its own shared interval. Refresh reads captured worker/guard results and raw
ownership only; it never calls safety getters, acknowledges commands, renews
leases, or evaluates control. The text Heater power guard sensor also reads the
captured publication. A stalled HA loop still prevents refresh and collection.

Worker snapshots expire after `max(30, 2 * poll_seconds)` seconds. Future-dated
snapshots are unknown. Fan report evidence additionally requires a trusted On/Off
report no more than 180 seconds old. Room evidence additionally obeys
`sensor_stale_seconds` (default 900 seconds), including explicit physical
acquisition time. Outdoor evidence uses the existing `valid_until`: strictly
less than 1800 seconds for sensors or 5400 seconds for weather, and the earlier
explicit acquisition deadline when bound. Outdoor captures come from the existing
protection evaluator; configurations without that evaluator have no captured
outdoor qualification. Qualification here is input validity, not completion of
outdoor-regime hysteresis or seasonal averaging.

Guard verdicts require both the normal guard publication and its underlying
reading evaluation to be within 30 seconds. Republishing an old evaluation
does not renew its evidence. Telemetry is evaluated on the existing schedule; a
publication timestamp is not a physical observation timestamp. Configured reserve
and explicit manual ownership do not expire with telemetry. Missing timestamps,
missing fields, unaccepted sources, untrusted reports and expired evidence remain
unknown, rather than implying a safe or inactive value. Report timestamps retain
their captured historical meaning even when current qualification expires; they
are not continuously retained after a later snapshot omits that source.

Counts, event timestamps, fan acknowledgments and diagnostic snapshots are
session-local and reset on reload/restart. Until new observations arrive, event
times and current evidence are unknown. Existing durable auxiliary manual
ownership and cleanup obligations still restore through their own storage.
No diagnostic evidence is restored as fresh proof. Prometheus/Recorder retain
their collected history; events between the last sample and restart can be lost.
Use session markers and event sequence for interpretation. Wall-clock jumps can
change epoch subtraction; authorization validity uses the existing monotonic
clock. Sensor `last_updated` measures publication, not device acquisition.

## Guard and ownership

Guard metrics exist only with the native power guard; manual ownership requires
native controls and an auxiliary heater. The reserve is the implemented configured
policy constant, currently 40%, not a newly editable setting.

| Key | Unit | Meaning |
| --- | --- | --- |
| `guard_ready` | flag | 1 only when captured Live is true, manual override false and guard blocked false; 0 otherwise. Unknown if any required field/snapshot is missing. This is guard readiness only, not complete permission to heat. |
| `guard_blocked` | flag | Captured guard block verdict, independently of preview/manual state. Unknown when stale. |
| `guard_reason_code` | code | Bounded diagnostic category below, not a new control gate or exhaustive cause. Unknown until a complete fresh guard capture. |
| `guard_reserve_percent` | % | Guard's configured battery reserve; unknown until first publication, then retained across snapshot expiry within the session. |
| `guard_sampled_at` | s | Time of captured guard publication; historical, not device telemetry time. |
| `auxiliary_manual_owned` | flag | Explicit persistent manual ownership of the auxiliary only. 0 means this flag is false, not that the controller owns or may activate it. |

Guard codes: `0` = `ready`, `1` = `preview`, `2` = `manual`,
`3` = `persistence`, `4` = `identity`, `5` = `manual_recovery`,
`6` = `emergency_ac`, `7` = `shutdown_fault`, `8` = `restoring`,
`9` = `latched`, `10` = `blocked`, `11` = `battery_reserve`,
`12` = `supply_disabled`.

If several apply, priority is persistence, identity, emergency AC, shutdown
fault/contradiction, manual recovery, manual, preview, restoring, battery reserve,
supply disabled, latched, other blocked, ready. Reserve/supply categories require
a captured block plus the corresponding captured telemetry. A category identifies
an observed condition, not causality or the actor who initiated it. Detailed text
remains in existing diagnostics. Every control/ownership/lease/dispatch gate still
applies even when `guard_ready=1`.

## Per-fan settings and acknowledgments

Each configured fan has this fixed family. Unsupported or inapplicable settings
remain unknown. Requested values require a fresh Live worker snapshot; Preview
retains the separate planned-speed sensor and makes no command claim.

| Key | Unit | Meaning |
| --- | --- | --- |
| `fan_{fan}_requested_percent` | % | Last executor speed target after minimum handling, direction gating and device speed quantization. Includes an attempted/deferred request, not proof of dispatch; zero is an Off target. |
| `fan_{fan}_reported_percent` | % | Captured trusted HA percentage, 0–100. Even an Off report without a percentage remains unknown. No physical motor-speed measurement is inferred. |
| `fan_{fan}_reported_at` | s | Captured HA last_reported, falling back to last_updated if absent; not physical acquisition time. Historical even when stale/untrusted. |
| `fan_{fan}_speed_acknowledged` | flag | Result of the executor's existing speed/settings match. 1 means accepted matching HA state, including already matching state without a new command; 0 means a known target was not accepted as matching. Unknown before evaluation or with stale/untrusted report evidence. |
| `fan_{fan}_speed_pending` | flag | Executor has an outstanding speed command at capture. 0 does not establish success: dispatch may be deferred or a failure in backoff. Unknown before evaluation or after snapshot expiry. |
| `fan_{fan}_speed_failed` | flag | Executor retains a speed failure/backoff record. Not a count; unknown before evaluation or after snapshot expiry. |
| `fan_{fan}_requested_lower_angle` | ° | Selected directional profile's lower bound, not proof the angle service was sent. Unknown without selected profile/Live snapshot. |
| `fan_{fan}_requested_upper_angle` | ° | Selected directional profile's upper bound, with the same request semantics. |
| `fan_{fan}_reported_lower_angle` | ° | Trusted HA lower bound, 0–360; unknown if absent, invalid or stale. |
| `fan_{fan}_reported_upper_angle` | ° | Trusted HA upper bound, 0–360; unknown if absent, invalid or stale. |
| `fan_{fan}_airflow_acknowledged` | flag | 1 for executor phase verified with current trusted report evidence; 0 for reduce/outlet/angles/retry. Unknown for minimum (no profile), unsupported, deferred, absent phase, Preview or stale report. This records the existing settings handshake, not physical bearing/thermal benefit. |
| `fan_{fan}_airflow_pending` | flag | 1 for reduce/outlet/angles/retry/deferred, including backoff; 0 for another known phase. Not the count of actual HA service jobs. Unknown without a fresh Live execution phase or for an unrecognized phase. |
| `fan_{fan}_airflow_phase_code` | code | Existing bounded execution phase below; unknown without a fresh Live execution phase. |

Airflow codes: `0` = `minimum`, `1` = `reduce`, `2` = `outlet`,
`3` = `angles`, `4` = `verified`, `5` = `retry`, `6` = `unsupported`,
`7` = `deferred`. `99` represents an unrecognized future phase; it is not a
success. Minimum leaves the physical angle unchanged. Verified requires the
existing fresh matching bounds/oscillation handshake; switchable outlets also
require Forward, while accepted fixed outlets have no runtime direction telemetry.
Requested angles and reported angles describe settings, not current instantaneous
motor bearing. See [directed airflow](directed-airflow.md) for calibration limits.

## Per-source reports and qualification

Room sources use the existing newest accepted alias. Outdoor sources use the
existing accepted coldest alias per physical source and role, including valid
fallback sources when primaries are selected. Qualification is not a selected-tier
flag. Rejected aliases do not create independent physical sources or measurements.

| Key | Unit | Meaning |
| --- | --- | --- |
| `source_{source}_reported_at` | s | Captured accepted alias's HA report time. Unknown if no accepted evidence exists; an expired prior snapshot can retain this historical time. |
| `source_{source}_packet_received_at` | s | Validated native H5100 raw-packet receipt time. Unknown on other paths. Keeps communication freshness separate from physical measurement and HA report time. |
| `source_{source}_measurement_at` | s | Explicit bound physical acquisition timestamp only. Unknown when absent; HA report time never substitutes for it. |
| `source_{source}_qualified` | flag | 1 for captured qualified evidence still within its deadlines; 0 only for explicitly current `change_only_unverified` evidence. Missing, unaccepted, stale, future or expired-snapshot evidence is unknown. The existing evaluator currently emits change-only-unverified only with old/future reports, which therefore remain unknown here. |
| `source_{source}_reason_code` | code | Bounded evidence explanation below. It can explain stale/missing evidence while qualified is unknown. Before any capture it is unknown. |

Source codes: `0` = `qualified`, `1` = `change_only_unverified`,
`2` = `not_accepted`, `3` = `snapshot_expired`, `4` = `report_expired`,
`5` = `future`. `99` means an unrecognized future freshness category. Code 2 does
not invent a rejection cause; it covers missing, unavailable, invalid, restored,
assumed or inactive-dependent sources omitted by the existing evaluator. Specific
causes remain in existing warnings. Code 1 is reserved for explicitly unverified
evidence whose report itself is current; old change-only values use code 4. A
qualified HA report or packet receipt without measurement_at does not prove physical acquisition
time, a new independent measurement, or qualified progress history.

## Auxiliary event and current-state sensors (existing)

These exist with native controls and a configured auxiliary heater. Counts start
at zero, event times start unknown. Each row's time is its latest observed event,
not the HA entity update time. Counts/times do not expire with telemetry. Every
event below has a count; only the explicitly listed events have timestamp sensors.

| Key | Unit | Meaning |
| --- | --- | --- |
| `authorization_renewed_count` | count | Accepted auxiliary authorization renewals, excluding general heartbeats. |
| `authorization_renewed_at` | s | Last stored authorization renewal wall time. |
| `authorization_expired_count` | count | Auxiliary authorization expiry latch events. An idle expired lease need not latch. |
| `authorization_expired_at` | s | When expiry was observed/latched, not its scheduled deadline. |
| `off_requested_count` | count | Worker/watchdog Off operations accepted into their queue; dispatch may still reject. |
| `off_requested_at` | s | Latest queue acceptance. |
| `off_dispatched_count` | count | Entry into direct native HA Off call, including already-idle calls without a safety receipt. |
| `off_dispatched_at` | s | Latest direct dispatch entry. |
| `off_completed_count` | count | Successful direct service returns, not device acknowledgments. |
| `off_completed_at` | s | Latest successful direct return. |
| `off_failed_count` | count | Direct operations that raised. |
| `off_cancelled_count` | count | Tracked task cancellations; no proof the device ignored a command. |
| `off_not_dispatched_count` | count | Queued operations rejected by dispatch checks. |
| `off_delegated_count` | count | Entries into the independent guard shutdown workflow. |
| `off_delegate_completed_count` | count | Successful guard workflow returns, separate from native service completion. |
| `off_stale_callback_count` | count | Completed safety receipts rejected after ownership/stop-epoch changes. |
| `off_acknowledged_count` | count | Existing safety logic accepted ordered fresh HA Off feedback for a completed receipt with required jobs drained; deduplicated per receipt. |
| `off_acknowledged_at` | s | Last time safety logic accepted that feedback. |
| `off_isolated_count` | count | Confirmed power isolation accepted instead of native feedback; deduplicated per isolation epoch. |
| `off_isolated_at` | s | Last acceptance of isolation. |
| `authorization_deadline_at` | s | Last renewal wall time plus configured auxiliary lease. Historical; manual takeover may revoke earlier. |
| `session_started_at` | s | Start of this native controls diagnostic session; identifies reset without a dynamic label. |
| `off_ack_reported_at` | s | HA report time of the last accepted Off acknowledgment, distinct from recognition time. |
| `authorization_valid` | flag | Current monotonic lease plus existing active/live/enabled/ownership/not-manual/not-recovering conditions; unknown before a renewal in this session. Not comprehensive permission to heat. |
| `ownership_retained` | flag | Owned auxiliary or retained cleanup obligation, including restored obligations. |
| `stop_pending` | flag | Pending stop epoch without accepted acknowledgment. |
| `pending_jobs` | count | Tracked queued/running worker/watchdog jobs for the heater, positive or Off; excludes guard-internal jobs. |
| `command_uncertain` | flag | Native controls retain uncertain heater command work. |
| `ownership_generation` | integer | Current ownership fencing generation; not a label or event count. |
| `auxiliary_generation` | integer | Current independent auxiliary fencing generation. |
| `stop_epoch` | integer | Current auxiliary stop epoch. |
| `reported_off` | flag | Trusted HA Off without heating/cooling action = 1; another trusted mode = 0. Missing/unknown/unavailable/restored/assumed/future/over-180-second report = unknown. Passive feedback, not command acknowledgment or electrical proof. |
| `observation_errors` | count | Best-effort diagnostic recording failures; gaps cannot block safety work. |
| `sequence` | count | Event order in this session; compact last-event attributes, no exported dynamic labels. |

Ownership, jobs, epochs and uncertainty are current raw state; they do not age out
with a worker snapshot. Counts exclude guard-internal attempts and relay/AC calls.
See [auxiliary diagnostics](auxiliary-diagnostics.md) for receipt provenance and
the bounded download-only 16-event history.

## Guard shutdown and HA Off observation sensors (existing)

These exist with the native guard. Counts and event times reset with the guard
session, start at zero/unknown respectively, and remain historical as inputs age.
Guard and HA events overlap and must not be added as independent incident counts.

| Key | Unit | Meaning |
| --- | --- | --- |
| `shutdown_guard_trip_count` | count | First latch events with stored trip reason/time. Not necessarily a dispatched action. |
| `shutdown_guard_trip_at` | s | Latest guard trip time. |
| `shutdown_guard_requested_count` | count | Guard queued Off jobs for native heater, dedicated relay or shared AC; excludes positive restoration. |
| `shutdown_guard_requested_at` | s | Latest queued guard Off job. |
| `shutdown_guard_dispatched_count` | count | Entry into actual HA service await after dispatch checks. |
| `shutdown_guard_dispatched_at` | s | Latest guard dispatch entry. |
| `shutdown_guard_completed_count` | count | Successful service job returns; no device acceptance claim. |
| `shutdown_guard_completed_at` | s | Latest successful guard service return. |
| `shutdown_guard_failed_count` | count | Failed jobs, including pre-dispatch rejection. |
| `shutdown_guard_failed_at` | s | Latest observed job failure. |
| `shutdown_guard_cancelled_count` | count | Jobs ending cancelled; an integration ignoring cancellation may still finish. |
| `shutdown_guard_cancelled_at` | s | Latest job cancellation. |
| `shutdown_guard_native_ack_count` | count | Existing guard accepted native Off evidence, once per request boundary; can precede service completion. |
| `shutdown_guard_native_ack_at` | s | Latest guard acceptance time. |
| `shutdown_guard_evidence_count` | count | Changed evidence/confirmation category, including initial, unknown and contradictory categories. Not an emergency count. |
| `shutdown_guard_evidence_at` | s | Latest evidence-category transition. |
| `shutdown_ha_off_requested_count` | count | HA observed explicitly targeted Off requests for the three bound roles. Actor, emergency intent and outcome unknown. |
| `shutdown_ha_off_requested_at` | s | Latest such HA request event time. |
| `shutdown_ha_off_reported_count` | count | Trusted HA Off transitions/reports with optional bounded temporal association; no causality or physical-stop claim. |
| `shutdown_ha_off_reported_at` | s | Time this Off observation was recorded; original report time is in bounded event detail. |
| `shutdown_sequence` | count | Guard observation event order, separate from auxiliary sequence. |
| `shutdown_observation_errors` | count | Best-effort guard observation recording failures. |
| `shutdown_sampled_at` | s | Latest captured guard diagnostic publication time. |
| `shutdown_isolation_confirmed` | flag | Existing guard isolation verdict; 0 means unconfirmed, not proof power is present. Unknown after 30-second snapshot expiry. |
| `shutdown_electrical_stop_confirmed` | flag | Existing dedicated-meter stop verdict; unknown without usable meter reading or after 30-second snapshot expiry. |
| `shutdown_dedicated_load_w` | W | Qualified dedicated load, only for configured watt evidence; no inferred zero or A-to-W conversion. Requires reading evaluation and snapshot each within 30 seconds. |
| `shutdown_dedicated_load_a` | A | Qualified dedicated current with the same freshness requirements. |
| `shutdown_aggregate_drop_w` | W | Qualified whole-supply baseline minus latest output for drop/relative_drop only; uses qualified baseline for relative_drop. Unknown for already_low/pending/inconclusive/unknown or stale evidence. Not heater-only draw or causality. |

These preserve the earlier diagnostic contract. Detailed confirmation, trigger,
request association and up to 16 recent guard events are described in
[shutdown observations](emergency-shutdown-diagnostics.md). External actor identity
cannot be recovered from an HA state report or shared service context.

## Other existing native entities and their exports

| Entity / unique-ID suffix | Export and interpretation | Availability/freshness |
| --- | --- | --- |
| Room temperature / `room_<room>` | `homeassistant_sensor_temperature_celsius`: fused room temperature; HA native °F, exporter °C. `independent_sources`, provenance and progress are attributes, not separate metrics. | Available if room exists in latest status. Existing presentation does not independently expire a stalled worker snapshot; use new source qualification and scrape health. |
| Fan planned speed / `plan_fan_<fan>` | `homeassistant_sensor_unit_percent`: policy speed before execution/quantization; valid in Preview too. | Available with live/shadow plan and fan in plan. Existing presentation has no independent snapshot-age timeout. |
| Controller status / `status`; plan summary / `overview`; recovery status / `recovery`; current issues / `issues`; last controller fault / `last_fault`; heater guard / `heater_power_guard`; room plan / `plan_room_<room>`; auxiliary plan / `plan_auxiliary` | Text states: no sensor-value series. Only HA automatic metadata below. Counts, temperatures and reasons inside attributes are not individually exported. | Existing entity-specific plan/room conditions apply. Text and last-fault history are not current telemetry. Guard text uses captured publication, without calling safety getters. |
| Room comfort mode / `comfort_mode_<room>` | Enum Schedule/Temporary/Hold is text, no numeric sensor-value series. | Unavailable while runtime is stopping. |
| Controller enabled / `enabled` | `homeassistant_switch_state`: 1/0 for durable explicit On/Off intent, including takeover waits. | Unavailable while runtime is stopping; 1 does not imply live commands are admitted. |
| Auxiliary manual control / `auxiliary_manual` | `homeassistant_switch_state`: 1/0 explicit persistent takeover of auxiliary only. | Unavailable while runtime is stopping. Same ownership intent as numeric manual_owned, independent of telemetry. |
| Same auxiliary manual switch | `homeassistant_switch_attr_persists_until_returned`: constant gauge 1 from its boolean metadata attribute. HA exports numeric switch attributes automatically. | Describes the manual mode's persistence contract even when the switch is Off; it is not another ownership verdict. Removed when the entity is unknown/unavailable. |
| Desired room temperature / `desired_<room>` | `homeassistant_number_state_celsius`: requested comfort point, converted from Fahrenheit when needed. | Editable request, not measured temperature; unavailable while stopping. |
| Room comfort climate / `climate_<room>` | `homeassistant_climate_target_temperature_low_celsius`, `homeassistant_climate_target_temperature_high_celsius`: requested comfort band, converted to °C. `homeassistant_climate_current_temperature_celsius`: fused room temperature if present. No single target-temperature series because this entity exposes a range. | Targets remain usable in Preview or probe loss. Current temperature has the existing latest-room-status freshness limitation; missing attributes can leave the exporter's prior attribute series until entity unavailable/removal. |
| Same room climate | `homeassistant_climate_mode{mode="heat_cool"}` = 1 (supported request mode, not shared-device mode). `homeassistant_climate_preset_mode{mode="Schedule\|Temporary\|Hold"}` is one-hot, with separate series for each actual mode string. | Existing request state; unavailable while stopping. |
| Same room climate | `homeassistant_climate_action{action="…"}` one-hot for HA HVACAction vocabulary. Off in Preview/disabled; otherwise based on HA reported equipment action, not power corroboration. Missing/unresolved action is not inferred from a target or mode. No fan-mode series is produced by these room entities. | Underlying existing action presentation does not independently enforce report age. A missing attribute can leave earlier exporter attribute series; do not treat it as new physical proof. |
| Reload head integration, Resume room schedule, Retry recovery settings, Acknowledge heater power recovery buttons | No button-value family in this exporter; automatic metadata only. Last press is not command completion or proof of recovery. | Existing button availability applies. |

## HA automatic families

For every included native entity, including text sensors/buttons, HA emits:

| Family | Type/unit | Meaning and limits |
| --- | --- | --- |
| `homeassistant_entity_available` | gauge, flag | 1 when HA state is neither unknown nor unavailable; 0 otherwise. Does not validate age or physical truth by itself. |
| `homeassistant_last_updated_time_seconds` | gauge, Unix s | HA entity last_updated; not report time, acquisition time, or lease renewal. |
| `homeassistant_state_change_total` | counter, events | Exporter state handling count (including initial handling), not controller action/event count. Resets with exporter lifetime. |
| `homeassistant_state_change_created` | gauge, Unix s | prometheus-client counter creation time when created-series emission is enabled. Exporter lifetime, not diagnostic session start. |
| `homeassistant_entity_info` | gauge = 1 | If entity/device has an area: mapping labels entity and area. Metadata, not device evidence. |
| `homeassistant_area_info` | gauge = 1 | HA area metadata: area, area_name, floor labels; only configured HA areas. |
| `homeassistant_floor_info` | gauge = 1 | HA floor metadata: floor, floor_name, floor_level labels; only configured HA floors. |

Value/availability/time/state-change series normally carry entity, friendly_name
and domain. Climate enum families additionally carry mode or action. Registry
metadata uses the labels above. Name/area edits can change label sets; bounded
controller reason codes do not prevent that normal HA behavior. Unknown states
remove value series but retain availability, state-change/time and entity-info
series. Exporter restart and entity removal have their own lifetime semantics.

## Collection and verification

Include the desired native entity IDs in existing Recorder/Prometheus filters.
These sensors need no template/input helpers. No production filters are changed
by this implementation. Scraping every 15 seconds can still miss short flags;
existing event counts retain observed events until reload, not indefinitely.

`tests_homeassistant/test_auxiliary_prometheus.py` exercises every existing
auxiliary/shutdown numeric key through real HA entities and the actual
`/api/prometheus` renderer. `test_numeric_prometheus.py` covers every new numeric
key, fixed labels, units, actual zero, stale-series removal, manual persistence,
restart, and reads that reject safety getters. It also checks this reference
contains every implemented sensor key and every bounded code.

Software tests establish serialization and observation semantics only. They do
not verify physical fan aim, heater isolation, sensor placement, a production
scrape, or real hardware timing. The separate 10-minute timeout investigation
is outside this change.
