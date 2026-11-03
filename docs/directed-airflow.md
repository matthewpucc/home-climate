# Demand-directed airflow

The house policy uses supported named sweeps for each explicitly bound purifier.
Room-facing aim is separate from the fan's `forward`/`reverse` outlet setting.
This feature provides comfort airflow and conditional transfer assistance. It
does not claim that fan-only circulation cools a room, satisfy temperature demand,
or remove heat-pump calls. No occupancy or instantaneous motor-bearing sensor is
inferred.

| Device role | Profile | Native sweep | Outlet / oscillation | Evidence |
| --- | --- | --- | --- | --- |
| Office | `toward_entertainment` | 43–75° (center 59°, span 32°) | Forward / On | Accepted actual sweep; the nominal lower request was 44° |
| Office | `toward_office` | 100–190° (center 145°, span 90°) | Forward / On | **Provisionally accepted; physical calibration pending** |
| Entertainment | `entertainment_circulation` | 100–190° (center 145°, span 90°) | Forward / On | Accepted after base repositioning; local circulation only |

These are different device-specific profiles. No Office-facing profile is known
for the Entertainment purifier, and none is inferred from the Office device,
180° inversion, or reverse outlet. Historical 70%/80% comfort observations do not
change the existing 60% policy speed.

## Selection and speed

During comfort periods the existing room temperature and target can guide aim
even inside the HVAC deadband: a deviation over 0.5°F starts a comfort preference,
with 0.25°F retention. Cool comfort alone can aim at the purifier minimum without
an operating head or useful temperature gradient. Heating aim requires useful
source warmth or compatible selected shared-head output. Limits-only periods
use their existing active boundary demand and inward stop target; ordinary
nominal-target deviation does not invent comfort demand.

A compatible selected shared-head call or useful ambient gradient can retain the
existing configured boost. Ambient transfer uses a 0.75°F start / 0.25°F retention
gradient. Source/HVAC guards, stale-probe guards, and protection limits still
apply. These are reasons to try airflow, not measurements of its thermal benefit.
Room measurements still govern HVAC independently.

If multiple destinations are plausible, retain a still-valid selected profile;
otherwise stay at the purifier minimum. Direction changes require 120 seconds
of continuous candidate eligibility and 300 seconds since the last selected
direction. Invalid or opposing intents lose their boost immediately, including
during dwell. Fast maintenance may reduce a cached speed but cannot increase it
or start a different route before the next comfort decision.

With no supported direction demand, leave the physical angle unchanged at the
purifier minimum. Restart likewise retains the device's existing angle until a
new supported intent is selected. This baseline is **not thermally neutral**.
The existing terminal fallback speed and mandatory Off cleanup remain in force.

## Native dispatch and reported evidence

Direction-dependent operation requires the native Home Climate adapter and an
explicitly registered, independent `hass_dyson` purifier fan. It requires speed,
outlet-direction and oscillation capability, the `fan.set_direction` service,
the `hass_dyson.set_oscillation_angles` service, and fresh angle/outlet/oscillation
feedback. Heater fan aliases and duplicate purifier bindings to one physical
device are rejected.

The executor reduces to the supported purifier minimum, requests Forward,
waits for a newer matching outlet report, sends one atomic lower/upper pair,
then waits for a newer matching settings report. With a zero minimum, the
reduction is Off; acknowledgement may occur while Off only if the device actually
reports the required settings. Subsequent boost explicitly powers on the fan.
An integration that does not report those settings while Off safely withholds
the boost; select a positive purifier minimum if powered acknowledgement is needed.

The angle service sets custom oscillation **and enables oscillation**, despite
its preservation docstring. The adapter requires final Forward and both
`oscillating=true` and the product-state-backed `oscillation_enabled=true`.
Matching stored bounds, service acceptance, restored/assumed state, or an older
report alone cannot complete the handshake. HA settings feedback cannot prove
physical motor arrival, obstruction-free movement, or actual room airflow.

The final device-targeted service remains attributed to the configured fan for
ownership, heater-alias checks, live/shadow permission, stop/reload generation,
repair epochs, dispatch deadlines and actual in-flight lifetime. Before dispatch,
the adapter resolves its current registered identity and recomputes current room
need/source compatibility. It also rechecks the original and current speed
entitlement before boost. A late profile completion cannot issue its old boost.
Direction and speed have separate pending queues. Capability failures retain
ordinary purification where allowed, expose a reason, and use per-device retry
backoff capped at five minutes. No raw device service bypass is provided.

`Controller status` includes `fan_airflow`: selected destination/profile,
reason, provisional status and known route limitation; requested angles/outlet,
requested and effective speed; reported settings; pending/error state; and last
verified settings command. The flags explicitly distinguish settings evidence
from physical bearing and thermal benefit.

The adapter contract was inspected against
[hass-dyson services at 0.38.0](https://github.com/cmgrayb/hass-dyson/blob/da351525e1650a40abd25bd9cc3d857fb5abdd7c/custom_components/hass_dyson/services.py#L151-L162),
[the atomic angle implementation](https://github.com/cmgrayb/hass-dyson/blob/da351525e1650a40abd25bd9cc3d857fb5abdd7c/custom_components/hass_dyson/device.py#L3484-L3539),
and [its fan feedback attributes](https://github.com/cmgrayb/hass-dyson/blob/da351525e1650a40abd25bd9cc3d857fb5abdd7c/custom_components/hass_dyson/fan.py#L683-L740).
Integer endpoint validation supports the actual 43° lower endpoint even though
the integration's UI uses 5° increments.

## Configuration and migration

House policy changes its former two fixed Entertainment-to-Office fan rules to
the profiles above. Purifier minimums, the 60% configured boost, unrelated fan
rules, auxiliary policy, HVAC policy, and protection settings are preserved.
No new physical IDs are inferred and no private bindings need to be published.

Generic schema-v2 users opt in per fan with `strategy: "directed"` and an
`airflow` object containing named `profiles`. Each profile specifies `source`,
`destination`, `purpose` (`transfer` or same-room `circulation`), `lower_angle`,
`upper_angle`, and `calibration` (`verified` or explicitly accepted
`provisional`). Only Forward/On sweeps are currently supported. Optional
`minimum_dwell_seconds` and `confirmation_seconds` default to 300 and 120.
Existing generic and legacy fan configurations keep their old rules unless
explicitly migrated. The CLI/add-on backend lacks the native guarded adapter:
directed configurations there retain minimum purification and report an
actionable unsupported-capability issue, with no direction-dependent boost.

After later physical calibration, adjust only known profile endpoints/status
through Configure → purifier settings, for example:

```json
{
  "minimum_percentage": 20,
  "living_percentage": 100,
  "airflow_profiles": {
    "office": {
      "toward_office": {
        "lower_angle": 100,
        "upper_angle": 190,
        "calibration": "provisional"
      }
    }
  }
}
```

Change `calibration` to `verified` only after checking physical aim. Unspecified
known endpoints retain their accepted defaults. This editor cannot fabricate a
new Entertainment-to-Office route or change device wiring.

## Later rollout

This PR is code and CI only; it does not deploy or send equipment commands.
After a separately authorized release/install, first inspect the shadow plan
and profile metadata, confirm each bound fan's independent registry identity,
then supervise minimum-speed outlet/sweep acknowledgement before allowing a
direction-dependent boost. Check both Office sweeps physically, especially the
provisional Office-facing sector, and recheck Entertainment after any base move.
Keep HVAC demand visible during comfort airflow, and use ordinary room outcomes
to assess benefit rather than treating an acknowledged angle as proof of cooling.
