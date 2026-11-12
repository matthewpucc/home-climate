# Home Climate

For direct numeric diagnostics and every native Prometheus export, see the
[metric reference](docs/metrics.md), including units, bounded codes, freshness,
restart behavior, and requested versus reported evidence.

A Home Assistant integration for a shared heat-pump system, room fans and a battery-backed auxiliary heater. Room profiles, weekly schedules, priorities, peak hours, seasonal selection and the recovery state are configurable. Physical device bindings stay in a private Home Assistant file; room climate cards provide temporary comfort adjustments.

Check the published release and installed version separately; this guide does not establish release availability or passed live acceptance.

## Install and inspect

Use the [installation guide](docs/hacs.md) to add this repository through Home Assistant **Settings → Marketplace** (HA 2026.11+), or install the release ZIP.

1. Place the private device map at `/config/home_climate_bindings.json`. [bindings.example.json](bindings.example.json) contains synthetic IDs only.
2. Install the integration, restart Home Assistant, and add **Home Climate** under Settings → Devices & services, or let the existing entry migrate.
3. Open **Configure** to review live permission, protection settings and the prepopulated policy objects. Inspect plans and source/ownership diagnostics before allowing active control.
4. Resolve any retained shutdown or recovery obligation through its supported procedure. When ready, permit live commands and turn on **Controller enabled**.

Existing installs update in place. The old private filename `house_climate_bindings.json` remains supported when the current filename is absent. An upgrade does not clear saved cut ownership or shutdown faults. A successful installation or passed historical commissioning test does not prove current equipment readiness.

See the [configuration guide](docs/configuration.md) for the editor, default schedules, source descriptors, manual takeover and notification routing.

Use the [room purifier speed controls](docs/configuration.md#room-speed-overrides) for persistent per-room percentages without reloading. Supported [Govee H5100 packet freshness](docs/bluetooth-freshness.md) can keep stable readings eligible when valid raw packets continue.

Supported purifiers can use [demand-directed room airflow](docs/directed-airflow.md), with named per-device sweeps, provisional calibration metadata and fresh settings acknowledgement before directional boosts. Comfort airflow does not replace HVAC demand.

## Configurable comfort

Office and Entertainment share the Bonus head; Guest, Primary and Living have their own heads. Living participates in normal shared-system arbitration. The auxiliary can supplement Living heat or provide it while the shared heat pump cools other rooms; it does not replace Living's head unconditionally.

The default weekly policy uses `America/Los_Angeles`. Overnight periods belong to the day they start. Limits-only periods enforce their room's lower and upper bounds without seeking a target. The configuration guide lists the explicit weekend transitions and the distinction between base profiles and schedules.

Living's seasonal profile uses a 30-minute outdoor average: select warm above 61°F, cool below 59°F, and retain the current profile at or between those thresholds. Local observations are preferred; a qualified current weather observation can supply fallback. Missing evidence retains the profile and shows degradation. Seasonal comfort is separate from conservative freeze protection.

Room inputs have explicit physical-source identities. Independently located heat-pump probes can be normal room inputs when correctly assigned and qualified. Aliases of one physical probe count once, and one shared-head probe cannot stand in for two rooms. An internal equipment reading is not automatically a representative room measurement. Report timestamps, acquisition timestamps and uncertain freshness remain distinct in diagnostics.

Default comfort decisions run every 90 seconds, with 15-second maintenance for fresh cutoffs and acknowledgements. Five-minute acknowledged head runs, three-minute minimum Off periods and shared-direction timing remain in force. Confirmed open windows can yield ordinary comfort vetoes in either direction; actual protection and hardware constraints still apply. See [operating rules](docs/control.md) and [protection settings](docs/protection-temperatures.md).

## Auxiliary power and ownership

During automated ownership:

- Auxiliary heat requires battery **above 40%**, available AC output corroborated by current supply telemetry, and normal device/command readiness. Positive output supports evidence of supply; zero output before startup is inconclusive.
- Input, aggregate output and whole-circuit watts have **no software ceilings**. Their diagnostic values do not silently become admission or recovery limits.
- The auxiliary's internal heating setpoint is **80°F**. Room demand and stopping rules determine the call; 80°F is not the room comfort target.
- Peak assistance is efficiency-biased. Off-peak assistance does not inherit the peak 1°F/15-minute prerequisite. Equipment timers and genuine fault handling continue to apply.
- Low reserve stops the entire auxiliary, including its fan, and creates a critical alert condition.

**Auxiliary manual control** is an explicit, persistent takeover of the auxiliary only. Other heads and fans remain automated. While taken over, schedules, reserve cutoff and the recovery preset do not reclaim that device; low-battery alerts and genuine fault obligations remain. Return to automation is explicit and requires current readiness. Merely changing the appliance in another app is not this persistent ownership transfer.

Shutdown retains its five-second native-Off deadline and distinguishes reported Off, electrical verification pending, corroborated stop and contradictory draw. Heat mode may be idle; dedicated watts provide separate evidence. Genuine failed shutdown requires explicit recovery, with durable ownership and late jobs still accounted for. The dedicated cutoff and any explicitly enabled shared-AC last resort remain separate from normal supply eligibility. Read the [auxiliary supply and recovery guide](docs/auxiliary-supply-policy.md).

An ordinary heater-only hold can permit fan-only operation after native Off and low dedicated load are proved and reserve/supply have recovered, even while heat waits through its 60-second stable-supply interval. Genuine shutdown faults, cut ownership, unresolved jobs and legacy cooldowns still block that exception.

## Recovery and visibility

Leaving active control through disable, restart or fault uses the configured recovery state: by default, supported dual-setpoint head operation at **68–73°F**, auxiliary Off and circulation fans **40%**. A manually owned auxiliary is excluded. Unsafe or failed transitions retain an actionable recovery state rather than claiming completion. A crash cannot issue commands while HA is down; retained work is reconciled on startup. A clean Preview startup is not permission to activate equipment. Native Controller On intent now survives a graceful restart when Allow Live remains enabled. Taking control supersedes an unfinished fallback preset after real commands and fresh shared-head Off are reconciled; it does not require matching the fallback range. Legacy storage without saved On intent starts Off.

The head preset requires `heat_cool` support, confirmed shared-head Off and completed prior commands, then applies Auto immediately without the normal minimum-Off or settling delay. The heat pump manages its own compressor timing during this handoff. Pre-upgrade ownership records without saved preset authority retain Off cleanup only; a successful new live ownership claim arms the durable preset. A failed or interrupted preset is not blindly replayed over later operator changes. **Retry recovery settings** explicitly retries the whole saved head/fan plan after its readiness checks, preserving auxiliary faults and manual ownership. Completion leaves **Controller enabled** Off; resuming automation is explicit.

Controller health and auxiliary heat authorization use separately renewed leases, with default limits of **300 seconds** and **120 seconds**. A healthy controller heartbeat alone does not prove current auxiliary supervision. An external watchdog for a complete HA outage is not included.

Diagnostics distinguish requested commands, reported device state, operation corroboration, ownership, source quality and the active reason for a hold. Shared heat-pump changeover sends Off at 0, 15 and 30 seconds, stopping early on confirmation; an unconfirmed transition is reported at 45 seconds and remains blocked. Heat-pump activation retains a 120-second acknowledgement budget.

Configure normal and critical notification routes explicitly. HA service acceptance is not proof that a phone received or sounded an alert. In particular, `notify.send_message` targets notify entities but has no generic critical-payload field in HA 2026.10.4. Critical routing needs provider-specific verification; an unverified route remains visible as such.

## Validation and development

The [release acceptance checklist](docs/release-baseline-acceptance.md) separates software checks, artifact/installation verification and supervised equipment evidence. The [thermal experiment series](docs/thermal-experiment-series.md) starts with passive observations and prepares later controlled runs. It does not arm overnight experiments. New learned coast/recovery predictions remain advisory until validated; analysis does not automatically deploy policy changes.

The canonical pure controller is in [`custom_components/house_climate/controller`](custom_components/house_climate/controller). Private wiring is separate from explicit policy edits, and upgrades retain registered room identities and valid holds where possible. Room cards support Schedule, Temporary and explicit persistent [Hold](docs/room-holds.md), with a per-room Resume schedule button for scoped automation access.

Run core tests with `python -m unittest discover -s tests -v`. Native tests use Python 3.14 and Home Assistant 2026.10.4: `python -m unittest discover -s tests_homeassistant -v`. Follow [CONTRIBUTING](CONTRIBUTING.md) for the complete release and packaged-source checks. Synthetic tests establish software behavior, not field capacity, sensor representativeness, network reliability or physical compressor operation. This documentation does not assert that live acceptance has passed.

The standalone CLI and container retain their file-configuration compatibility; they do not install the native house-specific electrical guard. Regenerate installed HA packages for this version: the generated watchdog now uses a separate auxiliary-authorization helper. Do not pair a new worker with an old generated watchdog. The configuration guide explains the house/generic dynamic-stop distinction.

MIT licensed.
