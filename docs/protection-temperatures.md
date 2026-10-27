# Protection temperatures (draft)

This draft separates protection settings from room comfort. Native house-policy
settings default to heating protection below 60°F, a 65°F floor in a qualified
cold outdoor regime, and emergency cooling at 80°F in any room. Existing generic
configurations with no protection object retain their legacy 66–75°F behavior.
The running 0.7.3 installation is not modified by this branch.

The HA Configure form saves all protection scalars atomically. Comfort-card
editing bounds and saved holds remain independent; adding outdoor wiring or
changing protection does not silently erase a valid room hold. A protection floor
can override a lower desired comfort target.

## Outdoor evidence and qualification

Private bindings optionally contain an `outdoor` list of source descriptors with
`entity_id`, an explicit physical/provider `source`, and optional `attribute` or
`unit`. Use sensor states or weather entities' **current** temperature attributes.
Weather uses `temperature_unit`; forecast arrays are ignored. Aliases of a source
count once; the coldest valid independent reading is used conservatively.
Sources require explicit F/C units and timestamps, must be less than 1800 seconds
old, and cannot be restored, unavailable, future-dated, or implausible. One valid
source is usable with a redundancy warning. No installed source is guessed.

The outdoor reference defaults to 32°F. A proposed ±2°F transition deadband and
five minutes of sustained valid evidence qualify a warm or cold regime. These
values are engineering draft defaults for review, not exact user-specified
requirements. A qualified regime persists within the deadband, including exactly
32°F: an already-qualified warm regime keeps its normal floor and cooling tie
preference there; an already-qualified cold regime keeps its higher floor and
heating preference. An initially unqualified boundary uses the disclosed higher
floor and configurable heating tie default.

Missing or stale outdoor evidence immediately uses the conservative 65°F draft
fallback and configurable heating tie default. It is labeled **unknown**, not
measured freezing. Validity and timer boundaries fence obsolete queued repairs
even if no entity value changes.

## Emergencies, reserves, and conflicts

A trusted room measurement below the active heating floor or at/above the cooling
threshold is an emergency immediately; there is no new indoor persistence timer.
The inherited probe trust model remains: median crossings act immediately, while
a lone disagreeing probe waits existing corroboration before starting positive
conditioning. Raw opposite-mode stop guards still act immediately on valid probe
extremes.

Emergency recovery releases one degree inside its bound by default, independently
of completion of the room's ordinary comfort target. Preventive protection still
uses the existing one-degree margin and ten-minute forecast; it is labeled a
**reserve**, not an actual measured emergency. Raw stopping retains its separate
one-degree margin and two-minute worsening-only forecast.

If both directions have emergencies, the larger degree violation wins. Only an
exact equal-magnitude tie uses qualified outdoor preference: warm favors cooling,
cold favors heating, and initially unqualified/unknown defaults are configurable.
No near-tie noise band is applied. Two already-active opposing emergencies confirm
a changed direction using the existing 120-second direction confirmation while
bypassing the 20-minute comfort dwell. Detection and preferred ranking update
immediately. A new emergency displacing ordinary comfort, or an infeasible old
direction, does not acquire that extra arbitration wait.

Priority cannot make a shared head safely heat a room when another attached room
is beyond its hot raw guard, or cool it when an attached room is beyond its cold
raw guard. Both directions may be infeasible. The plan keeps unsafe heads off and
reports the conflict rather than overriding hard limits. A shared outdoor unit
cannot satisfy simultaneous heating and cooling requests. No software guarantee
can replace adequate capacity, airflow, measurements, or a closed window.

## Draft setting limits

The atomic form exposes heating floors from 40–70°F, cooling threshold 75–95°F,
outdoor reference −40–60°F, release hysteresis 0.25–5°F, outdoor deadband 0–10°F,
and qualification 0–3600 whole seconds. It requires ordered floors and
nonoverlapping release bands. These are disclosed draft exposure limits.
Compressor acknowledgement/minimum-off timing, electrical limits, auxiliary-heater
cutoffs, and ownership/manual-control safeguards remain in force.

A release version and explicit merge/release/deployment approval are still needed
before this draft ships. No auxiliary-heater mode fix is included.
