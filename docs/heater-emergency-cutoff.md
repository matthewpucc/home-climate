# Heater emergency cutoff (unreleased)

This guide describes an opt-in draft after 0.8.2. Its new bindings are not valid
in the published 0.8.2 release. Installing code does not select or arm a shared
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
and successful persistence. A configured heater meter must also report fresh
low current. The button clears the manual-recovery requirement only; it never
energizes equipment. Ordinary cooldown and safe-load qualification still apply.
Acknowledgement can also let a deferred unload finish after actual jobs drain;
it does not permit new activation during shutdown.

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
