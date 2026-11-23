# Thermal history and heat-pump enum reference

These values are case-sensitive and come from the existing Home Climate
journal and diagnostics. The journal contains planned or reported evidence,
not measured compressor or blower operation.

## Read-only MCP thermal history

| Field | Values and meaning |
| --- | --- |
| Episode `demand` | `heat`: heating requested; `cool`: cooling requested; `idle`: a room reading and ordinary live/preview decision existed without either demand; `unknown`: the room reading was absent or the event did not carry a live/preview room decision. If both flags are set, the summary records `heat`; consult the decision's separate room flags. |
| Episode `end_reason` | `restart`: a newer writer session closed the episode at its last sample; `regime_changed`: the saved policy/wiring regime changed; `gap`: more than 180 seconds since the last sample; `clock_changed`: time did not advance; `demand_changed`: the summarized room demand changed; `context_changed`: other captured episode context changed. An open episode has no reason in MCP (`null` in the raw journal); this is not proof of present demand. |
| Decision `mode` | `off`: no shared heat-pump direction selected; `heat`: heating selected; `cool`: cooling selected. An omitted value means no mode in the projected record. This is not device acknowledgement. |
| Observation `timestamp_kind` | `device_observation`: configured explicit device-observation timestamp; `bluetooth_packet_receipt`: matching qualified Bluetooth receipt when the entity report alone does not establish freshness; `ha_report`: Home Assistant report evidence, not proof of new physical acquisition. When both are available, the raw record can show `ha_report` alongside a separate packet receipt. |

`raw_available` and `summaries_available` are booleans about rows in the
result; `coverage_complete` is deliberately always `false`.
`*_truncated` means matching rows or requested metadata could not be included
because of a limit; `false` is not proof of complete historical coverage.
`unknown` is an explicit recorded classification; omission or `null` is unset,
not zero or a negative result.

## Additional values in the existing HA action / raw journal only

| Field | Values and meaning |
| --- | --- |
| Decision / episode context `phase` | `new`: startup reconciliation; `stopping`: waiting for heads Off; `settling`: all-off/equipment off-time before a new direction; `running`: selected direction released to normal execution. `settling` alone does not imply a positive countdown. |
| Probe `freshness` | `qualified`: passed age/quality checks, possibly from HA report rather than physical acquisition; `change_only_unverified`: a change-only input did not establish a fresh observation. Unverified readings appear in decision context, not as raw qualified observations. |
| Window `state` / `observed_state` | `open`, `closed`, `unknown`. Observed is current normalized contact evidence; state is debounced. When present, `pending_state` or `last_confirmed_state` is `open` or `closed`. Diagnostic issue strings include `missing`, `restored`, `assumed`, `unknown`, `unavailable`, and `missing or invalid`. |
| Nested fan trial `stage` | `enrolled`: saved intent; `request_reserved`: dispatch reserved, not proof of execution; `active`: running; `restore_reserved`: restoration dispatch reserved, not proof of restoration; `complete`: finished; `needs_attention`: stopped for review; `cancelled`: cancelled or superseded. |

IDs, room/source names, free-form reasons and warnings, provider fan modes,
and arbitrary nested diagnostic values are not stable enums.

## Native heat-pump command, feedback and handoff telemetry

| Code family | Values |
| --- | --- |
| `mode_code` | `1=off`, `2=heat`, `3=cool`, `4=auto`, `5=heat_cool`, `6=fan_only`, `7=dry`. |
| `path_code` | `1=normal`, `2=shutdown_handoff`, `3=recovery`. |
| `reason_code` | `1=worker_command`, `2=terminal_preset`, `3=ownership_cleanup`, `4=integration_repair`, `5=worker_reconciliation`. |
| `outcome_code` | `1=invoked` (outstanding), `2=returned` (HA handler returned), `3=failed`, `4=cancelled_uncertain` (cancellation or unresolved completion). |
| `feedback_attribution_code` | `1=unattributed`, `2=matching_service_context`, `3=consistent_only` (mode/time consistent without matching context). |

`command_*` tracks the latest request; `handoff_*` retains the shutdown preset
and therefore has no separate path or reason code. `feedback_*` and
`handoff_feedback_*` describe reported mode; matching context relates HA
events, not physical evidence. No code `0` is defined. Time values are Unix
seconds. See [heat-pump mode command attribution](mode-command-diagnostics.md)
and [thermal history](thermal-history.md).
