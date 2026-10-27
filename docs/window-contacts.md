# Window contacts

Home Climate 0.8.0 adds optional private window wiring, diagnostics, and
sustained-effect comfort behavior. It lowers affected-direction comfort priority
and can yield the room's opposing soft shared-head boundary. Hard protection
remains independent.

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
contact evidence alone does not turn equipment off or discard a room.

## Sustained measured effect

After a confirmed opening, a valid fused room measurement must remain at least
0.5°F beyond its active comfort boundary for five minutes. An already-cold steady
room can qualify; new drift after opening is not required. This is an operational
association, not proof that the window caused the temperature. Heating deficits
lower only heating comfort priority; cooling deficits lower only cooling comfort
priority. The affected comfort priority becomes a nonzero 25% of its normal
value, and its opposing soft boundary can yield to a shared head serving another
room. For example, a cold affected room can yield its soft floor to shared
cooling, while its raw/forecast hard cold guard still stops unsafe cooling.

Unknown/invalid contact or room temperature restores baseline comfort handling.
Confirmed closure restores the normal priority and soft constraint after the
120-second closing confirmation. Target, schedule, and hold-context changes reset
the effect evidence. Actual protection and preventive reserves are not discounted;
missing-probe inhibition, hard guards, fan-direction safety, auxiliary/electrical
limits, and equipment ownership/timing remain effective.

This is deprioritization, not a thermostat Off rule. With no competing eligible
request, the affected room can still receive conditioning. Window and outdoor
timer boundaries publish a monotonic validity deadline so an old repair plan
cannot authorize a reload after the underlying policy state should change.

Automated policy and Home Assistant registry tests cover these rules. They do
not establish physical room response, sensor placement, airflow, or equipment
capacity; check the reported evidence when validating an installation.
