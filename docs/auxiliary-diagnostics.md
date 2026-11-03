# Native auxiliary diagnostics

Home Climate adds a fixed set of diagnostic sensors when a native auxiliary heater
is configured. No helpers, exporter extension or live configuration migration is
needed. The values are observations, never inputs to control decisions. The worker,
watchdog, authorization, retries and safety policy retain their existing behavior.

## Event meanings

Each event has a cumulative `*_count` sensor. Selected events also have a latest
`*_at` sensor, expressed as numeric Unix seconds in UTC (including fractions).
These use unit `s`, without a duration device class. HA timestamp entities discard
subsecond precision, so these intentionally remain numeric.
The pinned HA version serializes numeric states with 15 significant digits,
which preserves contemporary Unix times to about 10 microseconds. Sequence
numbers, rather than equal or rounded timestamps, establish observed event order.

| Event | Evidence recorded |
| --- | --- |
| `authorization_renewed` | A successfully accepted auxiliary authorization, at the actual stored authorization wall time; general heartbeats and status publication do not renew it. |
| `authorization_expired` | The safety latch being set after the monotonic lease check, which can be later than the scheduled deadline. An idle expired lease need not trip this latch. |
| `off_requested` | A worker or watchdog auxiliary Off operation accepted into its tracked queue, after initial request checks. It may still be rejected before dispatch. |
| `off_dispatched` | That operation entering the direct HA `set_hvac_mode(off)` call. Includes already-idle calls that create no safety receipt. |
| `off_completed` | That direct HA service call returned successfully. This is not a device acknowledgement or physical stop. |
| `off_failed`, `off_cancelled`, `off_not_dispatched` | Respectively a dispatched operation raising, a tracked task being cancelled, or a queued operation failing its dispatch checks. Cancellation does not prove that the device ignored a command. |
| `off_delegated`, `off_delegate_completed` | Entry into and successful return from the power guard's shutdown workflow. These are separate from native service dispatch/completion; the guard can acknowledge a report while its own service task remains pending. |
| `off_stale_callback` | A completed safety receipt rejected because ownership or its stop epoch changed. |
| `off_acknowledged` | The existing safety logic accepted ordered, fresh HA Off feedback for a completed receipt, with its required jobs drained. Still HA state evidence, not proof of electrical isolation. |
| `off_isolated` | The existing safety logic accepted confirmed power isolation instead of native Off feedback. |

Acknowledgement counts deduplicate repeated acceptance of the same receipt (or
isolation epoch). The download's sequence numbers provide event ordering if the
wall clock jumps; wall timestamps alone must not be used to infer elapsed time.
The diagnostics instrument worker/watchdog operations and the native controls'
accepted stop evidence. Independent guard-internal service attempts and relay/AC commands are outside
these worker/watchdog counters. The separate [shutdown observation stream](emergency-shutdown-diagnostics.md)
records guard actions, evidence and unattributed HA Off requests.

## Current values and freshness

- `authorization_valid` is 1 or 0 from the current **monotonic** lease and ownership
  conditions. It is unknown until an authorization is observed in this integration
  session. It is not a comprehensive permission-to-heat decision.
- `authorization_deadline_at` is the last renewal wall time plus the configured
  lease. It is a historical scheduled deadline, not the observed latch time and
  not the authoritative clock for validity. Manual takeover can revoke a lease
  before this timestamp.
- `ownership_retained` includes owned or pending auxiliary cleanup. It remains 1
  for restored obligations even when no new authorization has been observed.
- `pending_jobs` counts tracked worker/watchdog jobs for this heater (queued or
  running, positive or Off), excluding guard-internal tasks. `command_uncertain`
  reports the native controls' retained uncertain-job flag. `stop_pending` reports
  a pending stop epoch without its accepted acknowledgement.
- `ownership_generation`, `auxiliary_generation` and `stop_epoch` expose the
  current command fencing state as numbers, never metric labels.
- `reported_off` is 1 for a trusted HA Off report without heating/cooling action,
  0 for another trusted reported mode, and **unknown** for missing, unknown,
  unavailable, restored, assumed, future or over-180-second-old reports. This is
  passive feedback, not acknowledgement of a command or electrical-stop proof.
- `sequence` has compact attributes for current ownership, report quality/time
  and the last observed event with request/dispatch/receipt provenance. A late old
  completion remains a historical event with its older provenance; it is not
  presented as a new command's completion.
- `observation_errors` counts failed best-effort event recordings. Such failures
  cannot block dispatch or safety handling and indicate a gap in the observations.

One read-only group refresh runs every 15 seconds and on existing status updates,
independently of worker progress. It does not evaluate or trigger safety actions.
Sensor publication and scraping can each lag events; subtract the event's own
`*_at` value for historical timing, not the sensor's `last_updated` value. A stalled
HA loop or failed scrape still prevents collection; this change does not diagnose
HA performance. Current feedback becomes unknown on the next refresh when stale.
Historical event timestamps stay historical and do not disappear when evidence ages.
`off_acknowledged_at` is the time safety logic accepted the feedback;
`off_ack_reported_at` preserves the HA report time of that accepted feedback, so
delayed recognition does not appear to be a newly arriving report.

Counters, timestamps, sequence and the last 16 events reset on integration reload
or HA restart. `session_started_at` identifies that boundary without a dynamic
label. There is no diagnostic persistence or new awaited I/O on safety paths.
Existing cleanup ownership still restores through its existing storage. Before a
new event occurs its timestamp is unknown, not epoch zero. Recorder and Prometheus
retain their collected history according to their own retention settings. Events
between the last successful sample and a restart can be lost; exact complete event
traces across restarts are not promised.

The integration's **Download diagnostics** contains only this bounded observation
history, counters and times, without configuration or raw live input snapshots.
The separate shutdown stream adds bounded trigger text that can contain installation-specific names;
it excludes raw service payloads and HA context/user IDs. It is an in-memory recent window, not a durable event journal.
History is not repeated in entity attributes on every update.

## Recorder and direct Prometheus collection

Recorder records enabled entities normally unless existing filters exclude them.
Keep the desired native sensors included in existing Recorder and Prometheus
filters. Merge filters into existing configuration only when installing an approved
change. The following is an example, not configuration applied by this patch:

```yaml
prometheus:
  filter:
    include_entity_globs:
      - sensor.home_climate_controller_auxiliary_*
```

Verify actual entity IDs in the device's diagnostic entities: HA may preserve
renamed IDs or add a suffix for another controller. This example assumes default
names. No `input_*` or template entities are needed.

With Home Assistant 2026.10.4's default exporter naming, numeric counts/flags use
`homeassistant_sensor_state{entity="sensor.…"}`; event times use
`homeassistant_sensor_unit_s{entity="sensor.…"}`. Each sensor's entity ID selects
its meaning. Existing `namespace`, `override_metric` or `component_config` settings
can change those metric names. No reasons, sequence IDs, generations or event
timestamps are added as metric labels. HA exports these sensor states as gauges,
including cumulative counts; their HA state class is `total_increasing`.

For example, after substituting the actual entity ID:

```promql
increase(homeassistant_sensor_state{
  entity="sensor.home_climate_controller_auxiliary_authorization_expired_count"
}[1h])
```

Interpret increases together with `session_started_at` and collection gaps. A
15-second scrape may miss a brief flag, while an event counter retains the count
for the current session. Latest timestamps retain only the most recent occurrence.
For current freshness, an absent `reported_off` series is unknown, not 0. HA removes
its previous numeric series on unknown/unavailable states and exposes
`homeassistant_entity_available` as 0. Alerting should also check scrape health.

`tests_homeassistant/test_auxiliary_prometheus.py` registers real native sensor
entities, feeds their serialized states through the pinned HA exporter, and renders
its actual Prometheus response. It checks every intended numeric sensor, subsecond
timestamp precision, fixed labels, transient event counts, and stale/unknown/
unavailable series removal. This validates the offline scrape path; no production
scrape or configuration change is performed.
