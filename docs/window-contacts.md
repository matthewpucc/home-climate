# Window contacts

Home Climate 0.8.0 adds optional private window wiring, diagnostics, and
sustained-effect comfort behavior. Confirmed-open cooling eligibility and
measured-effect demand weighting are separate: an open room may yield its normal
shared-head cooling comfort floor while its demand priority is still unchanged.
Hard temperature protection remains independent.

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
known closed to establish a closed room. Missing, restored, assumed, unknown, malformed,
and unavailable states remain explicitly unknown, and break pending confirmation.
They never become fabricated closed reports. Controller status and each room's
plan attributes expose the contact evidence and pending confirmation. This
contact evidence alone does not turn equipment off or discard a room.

## Confirmed-open cooling relief

After an opening is confirmed, that room's ordinary comfort floor and cooling
restart margin no longer veto a shared-head cooling request. No additional
temperature deficit or five-minute effect qualification is required. For example,
a room at 71.1°F with a 71–73°F comfort band must not indefinitely exclude cooling
for a hotter attached room merely because its normal restart margin is 71.5°F.

The boundary that remains is the **active configured emergency heating floor**,
including the existing raw/forecast guard and stop margin. The normal 60°F and
cold-weather 65°F defaults are examples of that existing policy, not new window
settings or fixed constants in this rule. Missing temperature evidence and the
stronger active cold-weather protection still inhibit unsafe cooling. A window
does not grant permission to bypass these checks, equipment timing, shared-mode
arbitration or manual ownership.

This deliberately permits an open room below its ordinary comfort floor while
another room is cooled. Its comfort target is unchanged. Confirmed closure
restores its normal constraint after the existing closing debounce; unknown or
untrusted contact data withdraws the exception immediately. The bounded
`confirmed_open_rooms` context exposes contact qualification independently of
`window_effects`.

This immediate exception is cooling-only. Opening a window does not by itself
relax a room's heating ceiling; the existing measured warm-window effect below
continues to govern that behavior.

The native Home Assistant adapter also checks an immutable, scoped authorization
from the eligibility snapshot immediately before sending a relief-dependent
positive head command. A changed contact, relevant probe/profile/outdoor input,
or expired permission defers the start until a fresh plan is available. Report
noise and another head's mode cannot renew or invalidate that permission.
An Off command never requires this ticket.

CLI/HTTP adapters without that guarded-dispatch capability defer positive starts
that rely on this cooling exception, rather than claiming native HA queue
freshness. Ordinary starts that do not need the exception and all shutdown
commands retain their existing paths. Eligibility in a plan is not proof that
an unsupported adapter sent the command.

## Sustained measured effect

After a confirmed opening, a valid fused room measurement must remain at least
0.5°F beyond its active comfort boundary for five minutes. An already-cold steady
room can qualify; new drift after opening is not required. This is an operational
association, not proof that the window caused the temperature. Heating deficits
lower only heating comfort priority; cooling deficits lower only cooling comfort
priority. The affected comfort priority becomes a nonzero 25% of its normal
value. The existing qualified warm-window effect can yield the room's opposing
soft ceiling for shared heating. Cooling relief instead uses confirmed opening
as described above; its raw/forecast hard cold guard remains unconditional.

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
