# Window contacts (draft)

This draft adds optional window wiring and diagnostics. It does not yet change
comfort priorities, relax comfort boundaries, or change temperature protection.
The comfort and temperature-impact policy needs review before this feature is
merged. The running 0.7.3 integration is unchanged by this development branch.

Keep device bindings private. An optional `windows` object in
`home_climate_bindings.json` maps existing room roles to contact entities:

```json
{
  "windows": {
    "office": ["binary_sensor.example_window_open"]
  }
}
```

Add that optional object beside the existing required bindings; the example is
not a complete bindings file. Omitting `windows` preserves existing behavior.
An explicitly mapped contact can affect multiple rooms; each room retains its
own temperature measurements. Entity names and HA areas do not infer mappings.

Use a `binary_sensor` whose HA state is **on when open** and **off when closed**.
Door, opening, and window device classes are accepted; a device class is not
required. A raw "closed" signal with opposite semantics needs an HA template or
canonical open-state helper before it is mapped. Do not map duplicate raw
signals from one physical contact as if they were independent windows.

Known open and closed states require 120 seconds of continuous evidence, timed
with a monotonic clock. Identical report-on-change states remain usable without
periodic temperature-style freshness checks. A genuine `last_changed` revision
detects a close/reopen between worker snapshots and restarts confirmation;
`last_reported` timestamp noise does not.

One known open contact establishes an opening. Every configured contact must be
known closed to establish a closed room. Missing, restored, unknown, malformed,
and unavailable states remain explicitly unknown, and break pending confirmation.
They never become fabricated closed reports. Controller status and each room's
plan attributes expose the contact evidence and pending confirmation. This
diagnostic foundation does not issue contact-triggered device commands.

Before comfort adjustments are added, review whether an affected room should
only lose shared heating/cooling priority or also yield a shared head's soft
comfort boundary. Neither choice may remove raw or forecast house-temperature
protection, missing-probe inhibition, electrical limits, or equipment timing.
Debounced state and any later temperature-impact transitions must also invalidate
obsolete repair decisions at their timer boundaries; raw contact-state hashing
alone is insufficient once those transitions affect control.

This branch intentionally retains the current release version. A version decision,
full validation, and explicit merge/release approval remain necessary before it
ships.
