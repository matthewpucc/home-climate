# Wiring, policy and everyday controls

Home Climate separates physical wiring, editable operating policy and room holds. Changes are validated together before being saved. Inspection of a plan is not evidence that the equipment has acted on it.

## Configure the policy

Open Home Climate under **Settings → Devices & services → Configure**. The single form contains **Allow live commands**, the independent protection fields, and these prepopulated structured policy objects:

| Form field | Stored policy key | Purpose |
| --- | --- | --- |
| Room profiles and weekly schedules | `rooms` | Base profiles, weekly periods and seasonal alternatives |
| Auxiliary comfort and peak hours | `auxiliary_policy` | Peak window, deficit/progress qualification and inside-band stop margin |
| Seasonal comfort selection | `seasonal_policy` | Outdoor averaging, warm/cool hysteresis and initial profile |
| Purifier speeds and room overrides | `purifiers` | Ordinary purifier baseline and optional separate Living purifier speed |
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
| Primary, 09:00–22:00 | 67–70 | Limits-only; scheduled fan 0%, raised to the purifier minimum (default 20%) |
| Living, cool-season 07:00–22:00 | 69; 68–72 | Comfort |
| Living, cool-season 22:00–07:00 | 66–75 | Limits-only |
| Living, warm-season 07:00–22:00 | 71; 69–73 | Comfort |
| Living, warm-season 22:00–07:00 | 68–75 | Limits-only |

Use a room climate card and its desired-temperature number for a target/range adjustment. Edits normally create **Temporary**, ending at that room's next scheduled profile change. Select **Hold** to retain the current desired temperature and range across schedule boundaries and normal restarts until explicitly selecting **Schedule**. Edits made in Hold stay in Hold. Selecting Temporary instead restores its ordinary room-specific expiry. A room with no scheduled transitions has no Temporary expiry; this legacy behavior does not silently become the new Hold mode.

Protection, shared-head arbitration, equipment timers and auxiliary manual ownership still apply. Hold enables comfort in that room; it does not freeze the scheduled priority or fan policy and does not turn the controller or Live permission on. The mode is independent of equipment operation. Valid existing holds and registered room identities are preserved where migration can match them safely. A displayed room range is that room's active comfort/hold range, not the heat-pump equipment's allowable setpoint range. See [persistent holds and MCP release](room-holds.md) for persistence limits and scoped restoration.

## Purifier baseline and boosts

The **Purifier speeds and room overrides** object defaults to:

```json
{"minimum_percentage": 20, "living_percentage": 100}
```

During active automated control, the ordinary Primary, Guest, Office and Entertainment purifiers run at least `minimum_percentage`. Their existing scheduled, conditioning and transfer rules can request a higher speed. Primary therefore retains 60% from 22:00 through 09:00; a zero scheduled speed during the day becomes the default 20% minimum. Both values must be finite numbers from 0 through 100, and `living_percentage` must be at least `minimum_percentage`. A zero minimum preserves ordinary strategy-driven Off behavior. A partial edit retains the other saved value.

Office and Entertainment now use [named, device-specific airflow profiles](directed-airflow.md). Office's Office-facing 100–190° sweep remains provisional; Entertainment has only a supported local circulation profile with an explicitly accepted fixed outlet. Optional `purifiers.airflow_profiles` edits adjust known endpoints and calibration status. Entertainment's `calibration_key` is a commissioning declaration pinned privately to its registered device; replacement requires verified recalibration and a new key. The native guarded adapter acknowledges reported sweep/oscillation settings, plus Office's switchable outlet, before transfer-dependent boosts; an unsupported backend retains the purifier minimum. Generic and legacy fan rules do not opt in automatically.

The optional, separately bound Living purifier uses `living_percentage` throughout active automated control; its default is 100%. Saving that setting does not create a device binding. It remains saved even when no Living purifier is bound. These settings apply to ordinary purifiers only: they never authorize the auxiliary heater or its fan alias and do not change auxiliary reserve, manual-ownership or shutdown checks.

**Allow live commands** and **Controller enabled** still gate ordinary activation. When active control ends, each purifier's safe recovery target is the higher of its saved minimum and the saved recovery fan percentage. With the defaults, ordinary purifiers recover to 40% and a bound Living purifier to 100%. Required initial Off cleanup may still stop a fan, and actual faults or unverified device identity can block activation. Completing recovery does not make Controller Off or Preview continuously enforce purifier speeds. Saving policy uses the integration's normal reload, which can trigger an existing ownership handoff before resumed control; it is not a command to keep a fan running through that transition. Confirm the new resolved policy and controller/recovery state after reload.

Generic room configurations keep a zero `minimum_percentage` on each fan control unless explicitly configured. The 20% baseline is a house-policy default, and existing room schedules and higher-speed requests are unchanged.

Existing unfinished recovery plans retain their saved targets; upgrading does not rewrite them. The native integration checks HA registry identities before dispatch. The standalone HTTP/CLI controller cannot establish those device associations, so its operator must verify that ordinary fan bindings are independent purifiers and never heater aliases.

### Room speed overrides

For routine changes, open the **Home Climate device** in Devices & services
(or add its entities to your dashboard). Each room with a configured purifier
has **<Room> purifier speed override**, a 0–100% number, and **<Room> resume
automatic purifier speed**, a button. Setting the number saves immediately and
wakes the controller for its next decision; no YAML/JSON edit or integration
reload is needed. Entering **0% means Off** during active control. Press the
button to clear only that room's override and resume its baseline, schedules and
thermal behavior. In automatic mode the override number is empty/unknown and
its `speed_mode` attribute reads `automatic`.

Dashboard edits persist across restarts and apply to all configured ordinary
fans in that room. Other rooms and the auxiliary heater are unaffected. They are
independent of temperature Hold and work in Preview too; Preview saves intent
without sending equipment commands. Live permission and ownership still govern
actual actuation. Existing device speed rounding and direction checks apply.

The dashboard setting takes precedence over that room's configured speed
including an existing `room_overrides` entry. Resume automatic explicitly
removes that speed for active control; it does not restore an older configured
fixed percentage. Dashboard intent remains valid while that room's fan rules
and bindings are unchanged. An intentional change to those rules supersedes
its saved dashboard setting; other rooms' settings survive. Already armed
recovery plans retain their saved targets, and newly armed recovery plans use
the effective override as their baseline, subject to the configured fallback.

The configuration editor remains available for baseline policy and unbound
room preferences. Its edits use the normal integration reload:

In **Configure → Purifier speeds and room overrides**, add `room_overrides` to
hold a requested speed independently for each room. For example:

```json
{
  "minimum_percentage": 20,
  "living_percentage": 100,
  "room_overrides": {"office": 70, "entertainment": 70}
}
```

This requests 70% for only Office and Entertainment throughout active control,
including subsequent decisions, schedule changes and missing room probes.
Primary and Guest keep their existing speeds. The room override replaces both
the ordinary baseline and any scheduled or thermal speed request for that room;
it is independent of temperature Hold. The ordinary fan's supported speed steps
still apply, rounding upward when the exact requested percentage is unavailable.

Use the stable room keys `primary`, `guest`, `office`, `entertainment`, and
`living`, even if a room has a different display name or migrated internal ID.
Values must be finite numbers from 0 to 100. Zero holds the purifier Off during
active control and issues no new aim commands; it does **not** mean automatic.
An optional Living override remains saved if its separate purifier is not bound.

The map is saved with integration options and survives normal reloads/restarts.
Supplying `room_overrides` replaces the entire saved map: remove only the room
key you want to release while retaining the others, or supply
`"room_overrides": {}` to restore automatic speeds everywhere. Omitting the
field on a partial settings edit retains the saved map. Clearing an override
restores the original baseline, schedules and thermal behavior.

A positive override is independent purification, like the existing configurable
minimum. Directed fans continue selecting their existing supported profiles and
acknowledging outlet/sweep changes at the overridden baseline; no new route or
thermal benefit is inferred. An unsupported direction adapter retains that
purification speed. Live permission, controller ownership, device identity,
repair/fault checks and mandatory Off cleanup still apply. The override does
not continuously enforce a speed while the controller is disabled: safe
recovery uses the higher of the override baseline and configured fallback speed,
including when an active override was zero. Previously saved recovery plans
keep their existing targets.

Generic schema-v2 configurations can set `speed_override` on an individual
room's fan rule; omit it or use `null` to resume automatic behavior. The
house-policy editor maps its room overrides to these fan rules without changing
private device wiring. Existing configurations need no migration and default to
automatic behavior.

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

The `fans` section still requires `primary`, `guest`, `office` and `entertainment`. It optionally accepts `living`, which must identify a separate ordinary purifier. For example, merge `"living": "fan.living_purifier"` into that section while retaining its four existing roles. This is a synthetic entity ID: use the verified device's actual ID. Do not bind a fan alias of the auxiliary heater or another configured climate device. Entity names alone cannot establish physical independence. Native setup rejects a known shared device identity with the auxiliary. Activating a configured positive purifier minimum also requires both entities to have distinct HA device-registry identities when an auxiliary heater is configured; missing identity evidence inhibits that fan's activation. Verify the actual registry mapping rather than inventing one. Missing `living` leaves that purifier unmanaged; no ID is inferred. A wiring change requires the integration's normal reload and readiness checks before ordinary control resumes.

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

It applies whenever active ownership ends: requested disable, graceful restart or fault. The intended result is supported dual-setpoint head operation at the configured range, auxiliary Off and circulation fans at the higher of the saved recovery percentage and each fan's saved purifier minimum. The default recovery setting remains 40%; a bound Living purifier with its default 100% minimum therefore retains a 100% recovery target. Required initial Off cleanup and safe activation checks still apply. Manual auxiliary ownership is excluded. Unsafe devices do not receive positive activation merely to make the preset appear complete; partial failures remain visible and require attention. A crash cannot send commands while HA is down; unfinished obligations are reconciled after startup, without blindly overwriting later manual intervention. A clean Preview startup does not activate the preset, and completed recovery does not continuously enforce fan speeds while ordinary control is Off.

Heads must support HA's `heat_cool` dual-setpoint mode; an unrelated `auto` mode is not a substitute. The handoff retains confirmed shared-head Off and actual-command cleanup, then applies Auto immediately without the normal minimum-Off or settling interval. The heat pump manages its own compressor timing; fresh preset confirmation is still required. Confirmed preset completion releases routine head/fan ownership and leaves **Controller enabled** Off for an explicit disable or active-control fault. Explicit On, or startup with saved On and Allow Live, supersedes remaining preset confirmation through safe takeover.

During an uninterrupted handoff, a completed preset command with a fresh mismatched HA report can be retried automatically, up to three total attempts per device. Each retry waits at least 15 seconds after the prior service handler finishes and keeps the original feedback deadline (120 seconds by default). Completed devices are skipped. No retry overlaps an actual command, crosses an ownership change or restart, overwrites an observed operator change, or changes a head out of its reported dual-setpoint Auto mode. Missing, stale, restored, assumed or uncertain evidence inhibits retries. Retry progress is visible in each saved handoff request's `attempts`, `first_at`, `dispatched_at` and `finished_at` fields. HA handler completion and HA reports do not establish physical acquisition times.

A fresh matching report can complete recovery after a reported mismatch or exhausted retry budget. For example, an operator can correct a synthetic head's Auto range to its saved requested range; the existing handoff accepts that report once actual command work has finished and no uncertainty remains. It does not need another Controller toggle. Exhausted or inhibited retries retain ownership and the critical recovery notice; they do not guarantee that an already-failed HA reload will resume.

For an incomplete handoff, **Retry recovery settings** remains an explicit retry of the **whole saved head/fan plan**, not just its failed devices. Review the saved targets and scope first: this button can reapply every saved head and fan. It requires:

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
