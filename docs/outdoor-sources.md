# Outdoor sources (unreleased)

This guide describes the draft source-selection and freshness changes after
0.8.0. They are not available in the published 0.8.0 release. The new behavior
requires a future validated release; this draft does not change a running
installation or select any device automatically.

## Local primary and weather fallback

Add source descriptors to the existing private bindings file's `outdoor` list.
The following fragment uses synthetic IDs; keep the rest of your device map:

```json
{
  "outdoor": [
    {
      "entity_id": "sensor.local_outdoor_temperature",
      "source": "local_outdoor_probe",
      "role": "primary"
    },
    {
      "entity_id": "weather.regional_current",
      "source": "regional_observation_station",
      "observation_time_entity": "sensor.regional_observation_time"
    }
  ]
}
```

A valid primary is preferred even when a fallback reports a colder temperature.
When no primary is valid, valid fallbacks are used. Within the selected tier,
the coldest valid reading is used and aliases of one `source` count once. All
aliases of a source must use the same role. A failed alias does not discard a
valid sibling.

`role` is optional and defaults to `fallback`. Existing unmarked source lists
therefore retain their coldest-valid selection behavior; preference changes only
when a primary is explicitly marked. Optional `attribute` selects a sensor's
current numeric attribute instead of its state, and `unit` can explicitly supply
Fahrenheit or Celsius. Weather entities always use their current `temperature`
and `temperature_unit`; forecast arrays are ignored.

Before marking a local probe primary, verify its actual entity, units, reporting
cadence, timestamps, and placement. A probe's presence does not establish that it
measures representative outdoor air. There is no automatic sensor discovery or
new configuration-screen field for these bindings. After installing a future
release, file changes require the integration's normal reload procedure; they
are not hot-reloaded. Verify the plan with control disabled first.

## Bounded freshness

| Source entity | Maximum age |
| --- | ---: |
| `sensor.*` | Less than 1800 seconds (30 minutes) |
| `weather.*` | Less than 5400 seconds (90 minutes) |

Both limits expire exactly at the boundary. The reader prefers HA's original
`last_reported`, falling back to `last_updated` when report time is unavailable.
An unchanged temperature can remain valid when the integration actually reports
it again; polling the controller cannot renew its timestamp. These are fixed
source-type rules, not per-device lifetime options.

Unavailable, restored, future-dated, missing or malformed timestamps, invalid
units, and implausible temperatures are rejected. An invalid backup can appear
in warnings without invalidating a healthy primary. Controller protection
diagnostics expose `outdoor_selected_role` so the active tier can be checked.

If `observation_time_entity` is bound, its sensor state must be a timezone-aware
ISO timestamp for the temperature's actual observation, from the same source.
That observation must also be younger than the applicable limit above. The
source expires at the earlier report or observation deadline. A fresh HA write
cannot make an old observation fresh. A missing, unavailable, restored, future,
malformed, or expired bound timestamp invalidates that source; it does not
silently fall back to HA report time.

For example, the [NWS integration](https://www.home-assistant.io/integrations/nws/)
defines a separate **Latest Observation Time** sensor. Its actual entity must be
identified and enabled where needed; do not infer an ID or observation age from
the weather entity's name, availability, or update frequency. Aliases sharing
one station's observation are not independent measurements.

The [Met.no integration](https://www.home-assistant.io/integrations/met/) normally
polls approximately every 55–65 minutes. Its current temperature is derived from
the Locationforecast model. The 90-minute allowance accommodates that normal
publication cadence with a bounded margin; it does not promise trust through a
fully missed poll. HA report time, model issue time, forecast valid time, and a
physical observation time are different evidence. This reader does not guess
provider timestamp attributes or treat model issue time as a local observation.

Without an explicit observation-time binding, the age check establishes bounded
HA reporting liveness, not independently verified physical observation age.
Regional station or model temperatures can differ from conditions at a building.

## Qualification and protection

The existing default reference remains 32°F with a ±2°F deadband. Cold qualifies
at or below 30°F after 300 seconds of sustained valid input; warm qualifies at or
above 34°F after 300 seconds. A qualified regime persists within the deadband,
including exactly 32°F. Ordinary slow drift in that band does not repeatedly
change the floor or restart qualification.

The 300 seconds describes continuous validity of accepted cached evidence over
time, not several independent new weather observations. Valid unchanged reports
and refreshes overlapping the previous validity interval preserve qualification.
Expiry of one alias does not erase another selected alias's overlapping valid
coverage. If accepted coverage genuinely lapses, recovery requires a fresh
qualification interval even when a polling stall hid the gap between evaluations.
Only evidence selected by the reader establishes that continuity. If a primary
expires and returns entirely between polls, an unused fallback does not prove
which input would have been selected during the missed interval; recovery can
conservatively restart qualification. A fallback actually selected during an
overlapping valid handover can preserve continuity.

With no valid outdoor input, the state is **unknown**, not measured freezing.
The default conservative 65°F heating floor and heating tie preference apply.
The normal 60°F floor, cold 65°F floor, 80°F cooling threshold, immediate indoor
protection, compressor timing, electrical guard, and ownership safeguards are
unchanged. These are conservative operating buffers, not a guarantee against
pipe freezing. See [protection temperatures](protection-temperatures.md).
