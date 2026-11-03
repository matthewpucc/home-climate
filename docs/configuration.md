# Wiring, policy and everyday controls

Home Climate separates physical wiring, editable operating policy and temporary room holds. Changes are validated together before being saved. Inspection of a plan is not evidence that the equipment has acted on it.

## Configure the policy

Open Home Climate under **Settings → Devices & services → Configure**. The single form contains **Allow live commands**, the independent protection fields, and these prepopulated structured policy objects:

| Form field | Stored policy key | Purpose |
| --- | --- | --- |
| Room profiles and weekly schedules | `rooms` | Base profiles, weekly periods and seasonal alternatives |
| Auxiliary comfort and peak hours | `auxiliary_policy` | Peak window, deficit/progress qualification and inside-band stop margin |
| Seasonal comfort selection | `seasonal_policy` | Outdoor averaging, warm/cool hysteresis and initial profile |
| Recovery state after active control | `fallback` | Head range and circulation-fan setting when active control ends |
| Controller health lease (seconds) | `heartbeat_timeout_seconds` | Independently renewed controller-health deadline; default 300 |
| Auxiliary heat authorization lease (seconds) | `auxiliary_heartbeat_timeout_seconds` | Independently renewed automated-heat deadline; default 120 |

The object fields are structured editors, not a graphical weekly calendar. Policy edits are stored in the HA entry's `policy_settings`; they cannot replace device wiring or protection settings. The auxiliary lease cannot exceed the controller lease.

Room keys are `office`, `entertainment`, `guest`, `primary` and `living`. Each accepts `profile`, `schedules` and `seasonal_profiles`. A profile contains fields such as `target_f`, `low_f`, `high_f`, `priority`, `comfort_enabled` and `fan_percentage`. Temperatures in these objects are Fahrenheit. Setting `comfort_enabled: false` makes the profile limits-only: its own low/high bounds remain enforced, without seeking `target_f`.

For example, this **Room profiles and weekly schedules** edit changes a base profile while retaining its schedules:

```json
{
  "office": {
    "profile": {"target_f": 71, "low_f": 69, "high_f": 73, "priority": 1}
  }
}
```

Editing `profile` merges its fields. Supplying `schedules` replaces that room's schedule list, so preserve periods that should remain. A period places its profile fields alongside its start/end fields:

```json
{
  "name": "Workday comfort",
  "weekdays": [0, 1, 2, 3, 4],
  "start": "08:00",
  "end": "18:00",
  "target_f": 71,
  "low_f": 70,
  "high_f": 73,
  "priority": 2,
  "comfort_enabled": true
}
```

Weekdays are Monday `0` through Sunday `6`; omission means every day. Times use the configured timezone, `America/Los_Angeles` for the house baseline. An overnight period belongs to its starting day. Its start is inclusive and end exclusive. An all-day period uses `start: "00:00"`, `end: "00:00"` and `all_day: true`. Conflicting weekly overlaps are rejected; identical overlaps are allowed. The base profile covers any unscheduled interval, so inspect that profile rather than assuming a gap means Off. DST changes follow local time.

## Default weekly profiles

These are defaults, not a reading of the installed configuration. High comfort uses priority 2, medium/default comfort 1, and Primary sleep retains priority 10. Protection severity is separate from comfort priority.

| Room / period | Target and band, °F | Behavior |
| --- | --- | --- |
| Office, weekdays 08:00–18:00 | 71; 70–73 | High comfort |
| Office, other times | 67–73 | Limits-only; base comfort profile is 71 / 69–73 |
| Entertainment, weekday-start 16:30–02:00 | 70; 69–72 | High comfort |
| Entertainment, weekday daytime | 68–73 | Limits-only; Monday begins at 03:00 after Sunday's comfort |
| Entertainment, weekend-start 09:00–03:00 | 70; 68–73 | Medium comfort |
| Entertainment, Saturday 02:00–09:00 / Sunday 03:00–09:00 | 67–74 | Limits-only |
| Guest, weekday-start 17:00–01:00 | 70; 69–73 | Medium comfort |
| Guest, weekday daytime to 17:00 | 68–73 | Limits-only; starts 09:00 Tuesday–Friday and 10:00 Monday |
| Guest, weekend 10:00–22:00 | 70; 69–73 | Medium comfort |
| Guest, other times | 68–74 | Limits-only, including Saturday 01:00–10:00 and Sunday overnight through Monday 10:00 |
| Primary, 22:00–05:00 | 68; 67–69 | Sleep comfort, fan 60% |
| Primary, 05:00–09:00 | 69; 67–70 | Morning comfort, fan 60% |
| Primary, 09:00–22:00 | 67–70 | Limits-only, scheduled fan 0% |
| Living, cool-season 07:00–22:00 | 69; 68–72 | Comfort |
| Living, cool-season 22:00–07:00 | 66–75 | Limits-only |
| Living, warm-season 07:00–22:00 | 71; 69–73 | Comfort |
| Living, warm-season 22:00–07:00 | 68–75 | Limits-only |

Use a room climate card for a temporary target/range adjustment. The hold ends at the next scheduled profile change; protection still applies. Valid existing holds and registered room identities are preserved where migration can match them safely. A displayed room range is that room's active comfort/hold range, not the heat-pump equipment's allowable setpoint range.

## Seasonal and auxiliary defaults

The **Seasonal comfort selection** object defaults to:

```json
{
  "enabled": true,
  "average_seconds": 1800,
  "cool_below_f": 59,
  "warm_above_f": 61,
  "initial_profile": "cool"
}
```

The selected profile changes only after sufficient qualified averaging coverage. It changes to warm above 61°F and cool below 59°F, retaining its previous value at or between the thresholds. The last profile persists; after restart averaging evidence rebuilds. First startup uses `initial_profile`. Source-specific histories prevent a local/weather handover from becoming an unexplained mixed-location average. A qualified weather fallback requires actual observation-time evidence; forecast data is not a current physical observation. Missing data retains the profile and reports degradation. Freeze protection remains a separate decision.

Seasonal alternatives are full profile objects under `seasonal_profiles`, keyed by `cool` or `warm`, on the base room or a scheduled period. They are not partial field overrides. See [outdoor source wiring](outdoor-sources.md).

The **Auxiliary comfort and peak hours** defaults are:

```json
{
  "peak_start": "17:00",
  "peak_end": "21:00",
  "peak_deficit_f": 1,
  "urgent_deficit_f": 2,
  "peak_wait_seconds": 900,
  "progress_f": 0.5,
  "progress_window_seconds": 600,
  "stop_inside_band_f": 0.5
}
```

During peak hours, assistance requires the configured deficit/wait without meaningful recovery, or the deeper urgent deficit without the full wait. Missing trend evidence is unknown, not proof of no recovery. Peak auxiliary heat stops the configured margin inside the band. Off-peak assistance does not inherit the peak 1°F/15-minute prerequisite. The Living head remains a normal arbitration participant, and all equipment timing constraints remain.

The house baseline programs the automated auxiliary's thermostat to 80°F; this is not a room target and does not override room stopping rules. Normal admission uses reserve and corroborating supply signals, not software watt ceilings. Read [auxiliary supply and recovery](auxiliary-supply-policy.md) before changing control.

An ordinary heater-only hold permits fan-only operation only after proved native Off, low dedicated load and recovered reserve/supply. This allows circulation during the ordinary 60-second heat rearm interval. A real shutdown fault, owned cutoff, unresolved job or legacy cooldown still blocks the fan exception; low reserve under automated ownership keeps the whole unit Off.

Protection settings are independent. The default 60°F normal heating floor, 65°F conservative/cold floor and 80°F cooling emergency are protective boundaries, not summer comfort targets. The heating-floor form's upper validation bound is not a summer cooling limit. See [protection temperatures](protection-temperatures.md).

## Physical sources and private bindings

Private wiring lives at `/config/home_climate_bindings.json`, outside `custom_components`, and survives HACS updates. Required sections are `heads`, `rooms`, `fans`, `heater` and `power`; optional sections are `windows`, `outdoor` and `notifications`. The old filename `house_climate_bindings.json` is used only when the current filename is absent. An invalid current file does not silently select an older map. Unknown fields, missing roles and invalid domains are rejected.

Existing room sensor-ID lists still work. Explicit descriptors support source identity, attribute-based temperatures and qualified observations. This synthetic example assigns a head's external room probe to Office:

```json
{
  "entity_id": "climate.shared_head",
  "attribute": "current_temperature",
  "unit": "°F",
  "source": "office_desk_probe",
  "offset_f": 0,
  "report_on_change": false
}
```

Place that descriptor in the appropriate `rooms` list. Verify its actual units, physical location and reporting behavior; do not infer these from the entity name. Optional `observation_time_entity` binds a sensor containing the physical measurement timestamp. Optional `require_active_entity` binds a fan/switch whose active state is required for that probe to qualify. Do not invent timestamps or turn a device on merely to manufacture freshness.

Use the same `source` for aliases of one physical probe. Aliases count once, and a physical source cannot be assigned to different rooms. The shared head's probe on the Office desk belongs only to Office; Entertainment still needs its own measurement. Living's internal heat-pump probe remains outside normal fusion pending its planned comparison. Other externally located head probes may be normal inputs when correctly mapped. Do not double-apply a correction already made in the equipment.

Descriptors declare physical identity and location; Home Climate cannot inspect the actual installation to verify those declarations. Without an acquisition timestamp, a changed fresh temperature report can support a trend, but repeated unchanged HA cache writes do not establish new physical observations. The actual weather fallback's observation-time binding remains a deployment check.

`report_on_change` is an explicit source contract, not a way to certify physical freshness. A cached value can retain its change-only admission behavior while still being unsuitable evidence of new progress. Diagnostics distinguish actual measurement time, HA report time and uncertain freshness. Prefer independent, representative room sources; extrema still protect room limits. Source changes require the integration's normal reload and subsequent readiness checks.

The auxiliary uses a dedicated watt or current meter for electrical evidence. Battery output includes other loads; wall input and circuit values overlap and must not be summed. The optional `power.ac_output_switch` is an eligibility read binding. Shared AC cutoff requires a separately bound, explicitly enabled emergency fallback; it is not enabled by the read binding alone.

## Explicit manual control and recovery

**Controller enabled** controls ordinary automated comfort. **Allow live commands** is the device-write permission. Preview does not grant new comfort activation, but turning permission off does not erase existing ownership or unresolved cleanup obligations. Inspect the recovery status before assuming a transition has completed.

Turn **Auxiliary manual control** on for explicit persistent takeover of the configured auxiliary only. It does not send a takeover preset or turn the rest of the system manual. Other heads/fans remain automated. Automatic schedules, battery-reserve cutoff and the recovery preset do not reclaim the manually owned unit; critical low-reserve alerts and genuine fault obligations remain. A native app adjustment alone is not persistent takeover.

Turn that switch off to return control after the unit is natively Off and its current readiness/recovery checks pass. Pending or uncertain commands must be resolved before transfer. A saved takeover survives restart and is bound to the actual auxiliary identity; an equipment remap is not permission to take over a different device.

The **Recovery state after active control** object defaults to:

```json
{"heat_low_f": 68, "cool_high_f": 73, "fan_percentage": 40}
```

It applies whenever active ownership ends: requested disable, graceful restart or fault. The intended result is supported dual-setpoint head operation at the configured range, auxiliary Off and circulation fans at the configured percentage. Manual auxiliary ownership is excluded. Unsafe devices do not receive positive activation merely to make the preset appear complete; partial failures remain visible and require attention. A crash cannot send commands while HA is down; unfinished obligations are reconciled after startup, without blindly overwriting later manual intervention. A clean Preview startup does not activate the preset.

Heads must support HA's `heat_cool` dual-setpoint mode; an unrelated `auto` mode is not a substitute. The handoff requires fresh device confirmation and waits through existing Off, minimum-Off and settling barriers. Confirmed preset completion releases routine head/fan ownership and leaves **Controller enabled** Off for an explicit disable or active-control fault. Explicit On, or startup with saved On and Allow Live, supersedes remaining preset confirmation through safe takeover.

For an incomplete handoff, **Retry recovery settings** is an explicit retry of the **whole saved head/fan plan**, not just its failed devices. Review the saved targets and scope first: retry can reapply every saved head and fan. It requires:

- Controller Off and a saved, authorized recovery plan.
- No actual pending head/fan service work.
- Every involved head reporting native Off without active heating/cooling, restored or assumed state; each report must be no older than 180 seconds and newer than the failed-request/restart boundary.
- A successful durable save of retry intent before proceeding through normal Off/settling timing again.

Failed persistence retains the prior blocked plan. This button does not clear auxiliary shutdown faults, cutoff ownership or pending auxiliary cleanup, and it does not reclaim a manually owned auxiliary. An unconfirmed dispatched preset is not permission for blind repetition. Controller On may instead supersede that preset and reconcile actual jobs and fresh shared-head Off before ordinary control. Saved On plus Allow Live does the same on restart; it never clears uncertain commands or auxiliary fault ownership. Legacy storage without saved On remains Off until explicitly enabled.

## Notification routes

Configure the optional `notifications` section in the private bindings file. An entity-targeted normal route uses this synthetic example:

```json
{
  "notifications": {
    "normal": {
      "action": "notify.send_message",
      "target": {"entity_id": "notify.operator"}
    }
  }
}
```

An action name and a notify entity ID are different things. Home Climate supplies the incident-specific message/title; route data cannot override them. A provider-specific notify action may additionally carry its supported `data`.

Configure `critical` separately with a supported provider route and `critical_capable: true` only after verifying that route's capability. HA 2026.10.4's `notify.send_message` schema accepts message and title, not a generic critical payload. If critical routing is absent or unverified, the condition remains critical in HA, uses the normal route where available, and explicitly reports that critical delivery is unverified. A successful service call is not proof that a phone received, displayed or sounded the alert. Verify delivery on the intended destination before treating the route as ready.

Low battery remains an alert condition during manual takeover. Failed shutdowns and incomplete recovery handoffs need urgent attention. Repeated observations of one incident are deduplicated; notification failures remain visible without blocking device cleanup. The legacy `power.emergency_notify_service` field remains a service-name interface; it must not be populated with a notify entity ID.

## Migration and acceptance

HA entries migrate in place. Private wiring determines physical identity; explicit `policy_settings` holds operating edits. Old embedded `configuration` tuning is not silently imported as new policy. Review the resolved objects after upgrading, especially weekly transitions, source assignments and temporary holds. Installation does not clear saved manual ownership, cutoff claims or actual shutdown failures.

Pre-upgrade ownership records lack authorization for the new positive recovery preset. They retain their existing Off cleanup obligation; the first successful new live ownership claim arms and persists the configured head/fan preset. Do not interpret that migration as permission to discard a retained auxiliary claim or energize equipment on a clean Preview startup.

If using generated HA YAML, regenerate and install the package through the existing generation procedure before pairing it with the new worker. It now includes a separate auxiliary-authorization timestamp helper and renders both configured lease limits. The house configuration sets `auxiliary_dynamic_stop: true` so its generated probe check follows the active room stop policy; generic configurations default to `false` and retain their fixed-cutoff contract. Preserve the intended configuration when regenerating rather than toggling this internal compatibility setting to bypass a stop. Native-only installations do not need a second generated package.

Use the [release acceptance checklist](release-baseline-acceptance.md) to record offline checks, installed artifact identity and physical outcomes separately. The [thermal experiment series](thermal-experiment-series.md) prepares future data collection but does not arm an overnight test. Neither guide asserts that live acceptance has already passed.
