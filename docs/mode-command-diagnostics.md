# Heat-pump mode command attribution

Home Climate exposes numeric diagnostic sensors for each configured native
heat-pump head. Add the diagnostic entities to the Home Assistant Prometheus
integration's include filter if your exporter is not already collecting them.
Find them under the Home Climate device, with names starting **Head** and
ending in the listed field. The exporter uses ordinary
`homeassistant_sensor_state` and `homeassistant_sensor_unit_s` gauges; the
entity, friendly-name and domain labels are the usual Home Assistant labels.
Mode, path and reason are values, not labels.

- **Command** records the requested mode argument, path, reason, actual
  dispatch time, handler outcome and finish time. An unapplied plan or a
  request blocked before native service invocation is not a dispatch.
- **Handoff** keeps the latest shutdown-preset command and any matching
  feedback separately, so startup reconciliation or later normal commands
  cannot erase the previous shutdown's evidence. A return from HA's service
  handler is not a confirmation from the head.
- **Feedback** records a fresh, non-restored and non-assumed reported mode
  transition, or the first eligible report corresponding to an active
  command. Repeated identical reports do not extend its historical timestamp.
  A service-context match links Home Assistant events, not a physical sensor;
  the device's provider can still report optimistically. A match in mode and
  time alone is only consistency, not attribution. An external mode report
  never creates a synthetic command.

| Field | Codes |
| --- | --- |
| Mode | 1 Off, 2 Heat, 3 Cool, 4 Auto, 5 Heat/Cool, 6 Fan only, 7 Dry |
| Path | 1 normal worker dispatch, 2 shutdown handoff, 3 recovery |
| Reason | 1 worker command, 2 terminal handoff preset, 3 ownership cleanup, 4 owning-integration repair, 5 worker reconciliation |
| Outcome | 1 invoked and still outstanding, 2 HA handler returned, 3 failed, 4 cancellation or completion unknown |
| Feedback attribution | 1 unattributed, 2 matching HA service context, 3 same mode within the command window without a matching context |

Times are Unix seconds. Missing values mean no qualifying evidence and should
not be filled with zero. Historical records are bounded to the latest values
per head and saved locally without waiting on storage before controlling
equipment. A normal Home Assistant shutdown flushes the buffered record so the
next scrape can see it; a crash or failed storage write may lose recent events.
An unfinished saved dispatch is exposed as uncertain and cannot match a new
runtime's feedback. Nothing is backfilled for past events that were not captured.

For a combined list of thermal-history enum values and native telemetry codes, see the
[enum reference](thermal-history-enums.md).
