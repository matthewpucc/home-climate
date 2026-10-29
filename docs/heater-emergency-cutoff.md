# Heater emergency cutoff

Version 0.8.3 adds this opt-in native guard feature. Its new bindings are not valid
in 0.8.2 or earlier. Installing code does not select or arm a shared
cutoff automatically. Keep device commands disabled while commissioning wiring.

## Shutdown stages

The native heater guard remains independent of the comfort-enable switch:

1. Request the heater's native climate **Off** and require fresh mode/action
   feedback. A service returning successfully is not an Off acknowledgement.
2. If shutdown remains unconfirmed, cut the dedicated heater outlet when one is
   explicitly bound and still in the heater's physical power path.
3. If that stage is absent or fails to confirm shutdown, request the separately
   bound shared **AC output Off**, but only when its fallback flag is enabled.

Fresh heater-only current that contradicts Off prevents cached mode feedback
from counting as shutdown. Aggregate output alone cannot establish that the
heater remains on. A heater becoming unavailable after a confirmed outlet cut is
expected and does not by itself justify escalating to shared AC.

Shared AC can supply network equipment and other loads. Approve that collateral
outage and identify a local recovery path before enabling the fallback. An AC
output control is distinct from whole-station power, DC output and charging.
If the cut removes the command or feedback network, status remains **requested,
unconfirmed**; losing contact does not prove either success or failure.

### Sustained wall-load evidence

The fixed wall budget is now `12 × min(fresh phase A voltage, fresh phase B
voltage)` watts, replacing the 1350 W cap. There is no extra power-factor reserve
or nominal 120 V ceiling. It is a real-power budget, not measured RMS branch
current or a physical 12 A guarantee, and it applies to circuit/grid input rather
than battery-assisted AC output.

Circuit and grid input each need at least 30 seconds and three distinct fresh
reports at or above the budget to shed a running heater. Cached duplicates,
voltage-only updates and alternating reports from different sources cannot
complete that qualification. Invalid reports or reporting gaps invalidate it;
missing or invalid voltage has no nominal fallback. Pending qualification
defers new starts while retaining an existing call unless another protection
requires shutdown. Battery at/below 40%, AC output at/above 2000 W, temperature
protections and existing telemetry checks remain independent.

After confirmed native heater Off, continued charging or another circuit load
may keep wall consumption high. That remains a diagnostic and heating
inhibition, not proof of a failed heater stop or a new shared-AC escalation.
If the heater was already Off, fresh trusted feedback can provide passive
state-only evidence without a no-op command when no command, cleanup or meter
evidence conflicts. This is not a sent-command acknowledgement or physical
isolation proof. It expires and is not retained across restart; its first
evidence boundary cannot move backwards on a later report. A configured heater
meter must remain fresh, trusted and consistent with Off through recovery;
missing, restored, assumed or pre-boundary current cannot clear inhibition.
Passive evidence grants no power-restoration ownership and cannot acknowledge
manual or emergency recovery. An ordinary wall-load latch can clear only after
the existing safe-load and cooldown requirements are satisfied.
Switching AC output Off does not switch charging Off. Rearming retains the 85%
wall-budget threshold, 60-second safe-load qualification and five-minute trip
cooldown. Existing JSON bindings work without new thresholds or mappings;
upgrading does not clear persisted recovery latches.

## Private wiring

Merge these synthetic examples into `home_climate_bindings.json`; retain your
actual identities and every other section, including heads, rooms, fans,
windows and outdoor sources. This is not a replacement for the complete file.

```json
{
  "heater": "climate.auxiliary_heater",
  "power": {
    "heater_switch": null,
    "heater_current": null,
    "battery": "sensor.reserve_battery",
    "circuit_power": "sensor.shared_circuit_power",
    "input_power": "sensor.reserve_input_power",
    "output_power": "sensor.reserve_ac_output_power",
    "wall_phase_a_voltage": "sensor.supply_a_voltage",
    "wall_phase_b_voltage": "sensor.supply_b_voltage",
    "emergency_ac_switch": "switch.reserve_ac_output",
    "emergency_ac_fallback_enabled": false
  }
}
```

`null` explicitly means that the individual outlet or heater-current meter is
absent. Retire disconnected equipment from these roles: its Off state or old
reading is not evidence about the heater. A direct connection is supported
without requiring any particular smart-plug provider, but automatic auxiliary
Heat is inhibited until an available emergency AC path is explicitly enabled.
Supplying an AC entity without setting the boolean to `true` does not arm it.

When an approved dedicated outlet or meter is installed, bind its real
`switch.*` or current `sensor.*` entity instead of `null`. Current must be in A
or mA. Aggregate AC watts do not replace heater current, and an absent meter is
unknown rather than a fresh zero. Configured meters keep their existing
freshness checks. The dedicated and shared AC switches must be distinct.

### Dedicated load meter

**Unreleased; not accepted by 0.8.6:** a dedicated heater watt sensor can replace
the amp input through optional `power.heater_power`. Use an actual heater-only
sensor in W or kW, rather than aggregate battery output or wall power. Select
one meter; a configuration with both non-null `heater_power` and
`heater_current` is rejected explicitly.

```json
"heater_current": null,
"heater_power": "sensor.auxiliary_heater_power"
```

Existing amp configurations, absent/null watt bindings, switch-only wiring and
direct paths retain their existing meaning. Both meter kinds require finite,
nonnegative readings with trusted timestamps no older than 180 seconds. No
startup grace or stale-low exception is introduced. Unchanged HA reports can
renew `last_reported`; a Prometheus scrape alone cannot. HA entity report age
also does not prove the age of a physical device measurement.

The watt path uses **strictly below 100 W** as an operational low-load boundary,
allowing ordinary standby/fan load while distinguishing substantial heating.
It is not derived by converting the legacy 1 A bound to watts, a device rating,
or proof that electricity is physically isolated. Commission supported fan
modes and stop behavior before live use. The legacy amp boundary remains
strictly below 1 A; neither input implements a standalone RMS-overcurrent trip.

Fresh native **mode Off** remains mandatory for a requested stop. Trusted low
watts must be reported after that request to provide electrical corroboration;
pre-command, restored, assumed or expired readings do not qualify. Thermostat
idle is not mode Off. A valid native Off report can remain state-only while
asynchronous watt evidence arrives; this does not extend the five-second
native acknowledgement deadline or turn old high watts into a new sample.

Watt-based automatic outlet restoration needs both safe readings and a fresh
low-watt report after the actual Off request made during restoration. If that
proof does not arrive within the existing restoration budget, the guard cuts
again and retains inhibition. Relay-Off meter reporting must be commissioned
separately: a meter that stops reporting zero can safely prevent automatic
restoration after the cooldown. Switch-only wiring still requires manual
verification instead of automatic outlet restoration.

Meter identity participates in persisted ownership. Adding or changing a watt
meter during a retained claim requires manual wiring recovery; unchanged
legacy bindings do not acquire a mismatch merely because the new optional
field is absent or null. Keep comfort control and live permission Off before
editing private bindings, explicitly reload the integration in Preview, then
verify units, freshness, ownership and any required acknowledgement before
coordinating live use. This feature never enables shared AC fallback or
automatically restores shared AC.

## Optional household alerts

Local persistent notifications remain the default. To additionally send
emergency-latch notices through an existing HA notify group, merge this property
into the `power` object:

```json
{
  "emergency_notify_service": "notify.household_alerts"
}
```

That name is synthetic. Intentionally select the group's recipients and delivery
channels in Home Assistant, then use its actual `notify.<service_name>` action.
This is a service name, not a device entity or a recipient list. Leaving the
field absent or `null` sends no group notification. Configuring a group does not
arm shared AC fallback, send a test message or alter cutoff ownership. Verify
delivery separately before relying on it; a notification cannot confirm a
physical shutdown or replace local recovery access.

## Optional aggregate corroboration

Aggregate confirmation is disabled unless `power.aggregate_stop_confirmation`
contains explicit calibration. It uses **AC output**, never grid input, net
battery flow or a presumed heater wattage. It requires a fresh baseline before
the stop, fresh native Off after the request, and distinct output reports after
Off. Re-reading one cached timestamp does not add samples.

The example below illustrates the schema, not validated load thresholds. Measure
ordinary non-heater loads and actual report cadence before choosing values:

```json
{
  "aggregate_stop_confirmation": {
    "minimum_drop_w": 700,
    "residual_ceiling_w": 350,
    "noise_margin_w": 30,
    "settle_seconds": 30,
    "timeout_seconds": 120,
    "max_age_seconds": 45,
    "minimum_samples": 3
  }
}
```

Merge that property into the existing `power` object. The three watt values are
required; no default load drop or residual ceiling is inferred. A qualifying
drop is at least `minimum_drop_w + noise_margin_w`, residual output is at most
`residual_ceiling_w + noise_margin_w`, and the post-Off range must remain within
the noise margin. If the baseline was already low, stable low output can instead
corroborate an already-idle heater, still requiring native Off.

Defaults are two distinct reports, a 10-second settling span, a 45-second window
and a 30-second maximum report age. Choose timings for the actual cadence: three
30-second reports need more than a 45-second window, with margin for latency.
Future, stale, out-of-order or noisy reports and missing native Off leave the
result pending or unknown. Changing inference workloads or other loads can
imitate a drop, so this is **operational corroboration**, not isolated electrical
proof. Inconclusive aggregate data alone does not trigger a shared AC cut when
fresh native Off is acknowledged and no heater-only reading contradicts it.

The Heater power guard diagnostic exposes `shutdown_stage`,
`shutdown_confirmation`, `aggregate_stop` and the shared emergency-latch state.
`state_only` and `aggregate_corroborated` describe different evidence; neither
claims that a dedicated relay physically isolated the heater.

### Relative drop option (0.8.4 or later)

The `relative_drop_enabled` option was introduced in 0.8.4 and is not accepted by
0.8.3. Leave it absent on that older version. It defaults to `false`, preserving the absolute
low-output behavior above. Setting it to `true`
also permits a qualified relative drop even when other shared loads remain above
the absolute ceiling:

```json
{
  "aggregate_stop_confirmation": {
    "minimum_drop_w": 1000,
    "residual_ceiling_w": 160,
    "noise_margin_w": 20,
    "settle_seconds": 30,
    "timeout_seconds": 180,
    "max_age_seconds": 90,
    "minimum_samples": 3,
    "relative_drop_enabled": true
  }
}
```

This is a schema example, not validated calibration for an installation. All
three watt fields remain required. In relative mode the residual ceiling is
used only for the separate `already_low` path; the relative-drop path does not
require the final output to be below it. With these example values a qualifying
drop is at least **1,020 W**, including the noise margin. Other loads could
therefore remain at 800 W if the qualified pre-stop output was at least 1,820 W.

The relative path requires a recent stable pre-command window as well as stable
post-Off evidence. Each window needs at least `minimum_samples` distinct reports
spanning `settle_seconds`, with its power range no greater than
`noise_margin_w`. Pre-stop collection requires fresh native Heat mode with a
`heating` action, excluding restored or assumed feedback; an interruption clears
the collected window. The baseline uses the lowest qualified pre-stop reading,
not a historical maximum or a transient peak; the highest post-stop reading
must still satisfy the required drop. Reports must meet `max_age_seconds` and
the existing ordering, gap and freshness checks. The pre-stop buffer is bounded
to 128 reports; if the retained reports cannot establish the required window,
the relative path remains unknown. Relative-mode configurations reject more
than 128 required samples or a settling span longer than the maximum report
age. Relative mode also requires `minimum_samples` of at least three; the legacy
default of two remains unchanged when `relative_drop_enabled` is `false`.
Taking the baseline snapshot never delays sending Off.

A fresh native mode-Off report after the request is mandatory for both paths.
A thermostatically idle heater may have no large drop: only qualified absolute
low output can then corroborate an `already_low` result. If the baseline is
unknown or the unchanged output is above the low-output ceiling, fresh native
Off remains `state_only`; neither condition proves continued heating.
Unrelated AI, TV or other load changes can imitate or obscure a drop even when
both windows are stable. Relative corroboration is still operational evidence,
not heater-only current measurement or proof of electrical isolation.

Aggregate acquisition remains asynchronous. The example's 180-second window
does not extend the separate **five-second native-Off acknowledgement** deadline
or postpone emergency escalation when that acknowledgement is absent. Fresh
heater-only current contradicting Off remains a shutdown failure; stale,
missing or inconclusive aggregate data alone does not request a shared AC cut.
Corroboration is revoked when the required current evidence is lost. The
top-level confirmation label follows that loss instead of retaining a stale
aggregate-corroborated headline; this display correction does not clear a
physical-cut confirmation or a recovery latch.

## Latches and manual recovery

A shared AC emergency is persisted, blocks new heater starts and is not a
power-cycle loop. The guard never sends shared AC **On**, including after HA
restart or battery recovery. A returned or cancelled caller cannot hide a
still-running device service job. Delayed or unavailable feedback remains
unconfirmed, and acknowledgement is refused while relevant jobs are pending.

After resolving the cause, manually restore AC if it is Off and verify the
heater is Off.
The **Acknowledge heater power recovery** button requires fresh AC-On feedback
for a shared emergency, a fresh native heater-Off report after recovery, fresh
safe electrical readings, battery above 40%, no pending service/cleanup work,
live-command permission and successful persistence. A configured heater meter
must also report fresh low current. The button acknowledges manual, wiring and
shared-emergency recovery without energizing equipment or restoring old outlet
ownership. Ordinary cooldown and safe-load qualification still apply.
Acknowledgement can also let a deferred unload finish after actual jobs drain;
it does not permit new activation during shutdown.

Enabling live commands starts the independent protection guard even if
**Controller enabled** remains Off. Do not turn live permission on merely to
clear a warning before verifying the wiring and recovery prerequisites. Saving
that permission reloads the integration, so a fresh native Off report after the
reload is required for recovery. Thermostat Heat/idle is not mode Off.

### Restart recovery correction (0.8.5 or later)

After a restarted acknowledgement, verified manual Off supplies its own
runtime-only freshness boundary. It does not fabricate an actual Off command,
restore outlet ownership or enable comfort control. The ordinary latch still
requires 60 seconds of continuously safe load and the remaining five-minute
trip cooldown before normal demand becomes eligible. New reports must stay
valid; restarting again does not retain that manual evidence as fresh proof.

Acknowledgement rechecks evidence and pending work after its durable write.
If recovery changed, data expired or cleanup began while storage was waiting,
the guard retains inhibition in memory and storage and rejects the old
acknowledgement. No shared-AC On command is issued by this process.

### Dedicated outlet recovery

Dedicated-outlet automatic recovery remains separate: it requires a valid heater
meter, battery above 40%, stable safe load and cooldown, and can restore only
guard-owned power. Shared AC is never included in that automatic restoration.

Stored cutoff claims are tied to heater/cutoff/meter identities. An old claim
without matching identity is quarantined for manual acknowledgement rather than
transferred to new equipment. Disabling the shared fallback in JSON does not
erase an existing emergency latch. Do not clear recovery by editing `.storage`.

These checks preserve the existing temperature and electrical limits. They are
software controls dependent on device reports and communication, not a fire
protection guarantee. Manufacturer load suitability, independent device
protection, actual isolation and recovery behavior require commissioning.
