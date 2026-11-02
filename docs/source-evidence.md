# Room and outdoor evidence

Room inputs are explicitly mapped physical sources. A heat-pump entity can be a
normal room input when its temperature comes from a suitable external probe;
the entity's `current_temperature` attribute is selected explicitly. A probe
in one room must not be assigned to another room merely because both share a
head. No manufacturer, display name or entity domain establishes placement.

Existing room bindings containing sensor IDs continue to work. A descriptor
allows aliases, explicit acquisition time and operating-state qualification:

```json
{
  "entity_id": "climate.shared_head",
  "source": "office_desk_probe",
  "attribute": "current_temperature",
  "offset_f": 0
}
```

Use the same `source` for aliases of one physical probe. Only one contributes
to a room's median or corroboration count; the newest valid alias is preferred.
Assigning a physical source to different rooms is rejected. Room bindings do
not automatically add heat-pump probes. Internal readings whose room placement
has not been accepted remain separate diagnostics by leaving them out of the
room mapping. An offset already applied by the equipment must not be reapplied
to an independent room sensor or duplicated in `offset_f`.

Optional `observation_time_entity` identifies a sensor whose state is the
timezone-aware timestamp of this source's actual temperature acquisition. Both
the temperature report and this acquisition must qualify. Rewriting a cached
HA state does not refresh an old acquisition timestamp. Without this binding,
diagnostics explicitly label the timestamp as an **HA report**, and physical
measurement time remains unknown.

Optional `require_active_entity` selects a fan or switch that must report On
before this probe is used. A fan with a reported percentage must report a
finite positive percentage. This is appropriate only when the configured
probe's representativeness depends on that circulation; it is not a blanket
rule for sensors inside appliances. Restored or assumed probe/condition states
are rejected.

New auxiliary progress decisions consume distinct accepted temperature
observations. An explicit acquisition timestamp can establish a new unchanged
measurement. Otherwise, a changed temperature with a new accepted HA report
can advance the observation marker; repeated identical values or metadata-only
writes do not manufacture a sequence of flat measurements. Missing qualified
history remains unknown. Existing `report_on_change` compatibility can retain
an unchanged room value, but an old report is labeled **freshness unverified**
and cannot establish qualified progress.

## Seasonal comfort

Seasonal comfort uses a time-weighted 30-minute average, separately maintained
for each outdoor physical source. Samples from different locations are not
stitched together when the selected source changes. Valid held values cover
time only until their source deadline; repeated controller polls do not extend
that deadline or count as new measurements. Initial history starts when the
controller actually sees the source, not at a backdated cache timestamp.

Above 61°F selects the warm comfort profile; below 59°F selects the cool
profile. The previous profile is retained between and at these boundaries.
The selected profile persists across restart; averaging history does not.
An installation without saved state uses the configured initial profile
(cool by default) while rebuilding qualified history.

Local primary inputs are preferred. An airport/weather fallback must provide
current temperature and its explicit physical observation timestamp; forecast
values and provider publication times are not substitutes. A synthetic wiring
example is:

```json
{
  "entity_id": "weather.regional_current",
  "source": "regional_observation_station",
  "role": "fallback",
  "observation_time_entity": "sensor.regional_observation_time"
}
```

Verify actual entities, units, acquisition semantics and cadence before adding
these private bindings. Weather state availability or a recent HA update alone
does not establish observation age. Existing outdoor limits remain less than
30 minutes for sensor reports and less than 90 minutes for weather reports;
an explicit acquisition timestamp also expires at the applicable limit.

When no source qualifies, the last seasonal profile remains selected and the
degraded-source reason is visible. Seasonal diagnostics separately identify
warming, qualified, unavailable, unverified and disabled evidence, so ordinary
initial averaging is distinguishable from source loss. When a configured local primary is missing,
a warmer regional fallback cannot clear local freeze protection. That separate
protection retains its conservative heating floor; seasonal comfort can still
use a qualified regional observation. Neither regional weather nor these
software floors guarantees that every part of a building is above freezing.

## Verification before deployment

Check the physical location/source identity of each newly mapped head probe,
any equipment-applied offset, and aliases already in the room mapping. Verify
the observation timestamp belongs to the same physical value, and test the
circulation qualifier if one is configured. Compare room and outdoor source
diagnostics in Preview before permitting actuation. No live entity names or
private bindings are included in the examples above.
