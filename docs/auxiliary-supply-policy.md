# Auxiliary supply, ownership and recovery

Automated auxiliary heat requires a valid battery report **above 40%**, an available AC-output On state, current aggregate-output telemetry and an available heater that acknowledges its commands. Battery HA reports qualify directly for 900 seconds; an older unchanged SOC can also qualify through the bounded same-device activity rule below. Output reports expire after 180 seconds. Invalid required evidence inhibits automated heat. Queued activation and relay restoration recheck current evidence at dispatch.

Input, circuit, phase-voltage and aggregate-output totals have **no software watt ceilings**. The previous Emporia exception is removed. Positive output corroborates supplying loads; zero before startup remains inconclusive. Manufacturer protection, temperature protection and real shutdown-failure handling remain in force.

## Supply evidence

The optional power.ac_output_switch binding identifies the AC-output state independently of permission to cut shared AC. Without it, the existing power.emergency_ac_switch is the read binding. Binding a switch does not enable emergency cutoff.

Ordinary admission accepts corroborating signals rather than requiring a separate physical AC read. A valid, available unchanged On state can remain useful while fresh battery and output reports demonstrate telemetry continuity. Home Climate does not rewrite that switch's timestamp, command it to obtain freshness, or describe this combination as a physical read. Current physical feedback, if provided, must not contradict the reported state. Diagnostics distinguish device_report, corroborating_reports, reported_off, contradictory and unverified evidence. A cache write alone cannot replace missing battery or output telemetry.

This contract does not require selecting Bluetooth versus cloud control just to use ordinary corroborating signals. It does not claim either transport guarantees current physical state. Numeric report timestamps remain HA receipt evidence unless the source exposes acquisition time.

## Unchanged battery values and device activity

`last_updated` dates the last HA state/attribute change; `last_reported` dates the latest HA state write, including an identical write. An unchanged 100% value with an old `last_updated` already qualifies when its `last_reported` is current. Neither timestamp establishes physical SOC acquisition. For example, Bluetti Official v1.0.5 republishes cached sensor values on shared callbacks; its cloud/Bluetooth connection status alone does not guarantee a new battery measurement.

If the SOC report itself is older than 900 seconds, Home Climate can retain that valid available percentage when all of the following hold:

- Battery and mandatory output sensors share the same nonempty native HA registry device, config entry and platform; the source integration is loaded. An input sensor may corroborate only if it shares that identity too. Entity-name similarities, external circuit watts and unrelated devices cannot qualify.
- This guard runtime has first observed a numeric input/output baseline and then a real numeric power-value change with a later HA change timestamp. That activity remains eligible for **180 seconds**. Initial publication, identical cache writes, formatting changes, attribute-only writes and repeated polling do not renew it. Zero watts is still a valid measurement; changing to or from zero can demonstrate activity without proving that AC is enabled.
- SOC remains finite, between 0 and 100%, available, non-restored and non-assumed, with a valid nonfuture original report timestamp. Current valid output telemetry and all ordinary AC, reserve, stop and recovery checks are still required. **40% or lower remains blocked**, including with an activity witness.

The witness is process-local, never persisted. It clears on restart, source reload, identity changes, unavailable/invalid mandatory telemetry, removal/re-addition and clock rollback. Optional input loss withdraws input-derived evidence; it cannot invalidate an independent valid output witness. A registered mandatory source known to be unloaded blocks even otherwise recent cached values. After a reset, initial values only establish a baseline; another numeric change is required for the older-SOC exception. An output or input that is genuinely steady can therefore leave old SOC unqualified until another report or activity arrives.

Diagnostics expose `battery_evidence` (`ha_report`, `same_device_activity`, or `unverified`), the unchanged `battery_reported_at`, and separate `battery_activity_at` / `battery_activity_entity`. Device activity corroborates the latest retained SOC; it is not a new SOC sample and cannot detect every upstream cache or firmware failure. No timestamp is rewritten and no poll, power toggle or hardware command is issued to obtain freshness. Missing registry identity keeps the older-SOC exception inhibited; existing fresh-report behaviour remains available unless there is explicit source failure. No private binding or storage migration is required.

## Heating, circulation and manual ownership

Living's heat pump participates normally in shared-system arbitration. The auxiliary supplements eligible demand or provides local heat while other rooms cool, subject to configured peak/off-peak rules and device timers. The automated appliance thermostat ceiling is 80°F, distinct from the room target. Native HA control selects the highest supported native step at or below that ceiling. A whole-Celsius device selects 26°C (78.8°F, shown as 79°F in HA), not 27°C (80.6°F, shown as 81°F). Heat mode can mean idle. Dedicated watts distinguish a substantial load, low-load idle and unknown evidence; aggregate battery output is not heater-only power.

Low battery inhibits the entire automated Dyson, including circulation. With adequate supply and reserve, heater-only comfort restrictions need not stop eligible fan-only circulation. During an ordinary 60-second heat recovery hold, circulation requires a previously proved native Off, fresh low dedicated load, available supply and no actual fault, cutoff ownership or uncertain work. Low-load evidence must remain current while circulating; lost evidence or supply requests a new native Off. Failed-shutdown, retained ownership and legacy cooldown obligations remain blocking. Critical low-battery notifications remain active during manual ownership.

Explicit **Dyson manual takeover** transfers only the auxiliary to the user. It persists across schedules and restarts until explicitly returned; other heads and fans remain automated. During takeover Home Climate does not apply its reserve cutoff or fallback preset to deliberate manual operation. Ordinary app changes do not silently grant takeover. Outstanding jobs and genuine shutdown faults require reconciliation, and takeover never clears saved cut ownership. Return requires supported stop/readiness checks; low reserve can then keep automated heat inhibited without preventing ownership return.

## Auxiliary startup and target acknowledgement

The native adapter obtains the climate entity's native temperature unit, range, step and display precision on HA's event loop. HA converts and rounds displayed temperatures but leaves `target_temp_step` in native units; the state attributes alone therefore cannot define a converted target grid. The advertised native minimum anchors the step grid. Missing, invalid or unsupported native capability metadata inhibits the auxiliary with a local diagnostic; independent heat pumps continue under their own checks.

Startup first programs the selected target, waits for a fresh trusted report matching HA's exact rendering and the entity's unrounded native target, then sends explicit Heat. The same existing activation deadline covers both stages. Entity identity, units, range, step and precision are rechecked immediately before each queued positive command. A changed target at explicit Heat dispatch also defers activation. Capability changes during startup and failed acknowledgements retain the existing Off and actual-command cleanup requirements. No broad temperature tolerance, new watt ceiling, or relaxed shutdown acknowledgement is introduced.

Native target readback and HA report timestamps remain integration-reported evidence, not proof of physical heating or source acquisition time. Device integrations can report targets optimistically; dedicated watts remain the evidence of actual load. The whole-Celsius regression uses a synthetic real-HA climate provider reproducing the public hass-dyson v0.38.0 contract. Installed integration metadata must still be verified during supervised acceptance; this regression does not establish a particular installation's version or physical behaviour.

## Shutdown evidence

The existing **five-second native-Off deadline** and state-only confirmation behavior remain. There is no new five-second requirement to receive both native Off and low watts. Dedicated low load means below **100 W**, or below **1 A** when an explicitly bound current meter is used. These are evidence thresholds, not a watt-to-amp conversion or manufacturer rating.

Diagnostics distinguish:

- **corroborated_off:** native Off with qualifying low dedicated load, or verified isolation without contradictory load evidence.
- **reported_off_unverified:** native Off but no qualifying electrical evidence.
- **contradictory:** accepted dedicated-load evidence contradicts the requested stop.
- **not_observed:** the required Off evidence has not been observed.

The existing contradiction rule accepts one qualifying high report; no new sustained-high delay is implied. A HA report after a command does not necessarily date physical acquisition. The supervised acceptance protocol measures this distinction before proposing a deadline or filtering change. Missing data is unknown, not a fabricated zero or proof of continued heating. Aggregate stop corroboration remains separate from removed operating watt ceilings.

## Explicit recovery and migration

A reserve/supply hold with successful native Off can recover after 60 seconds of continuously safe evidence. Genuine failed shutdown requires explicit approval; its five-minute cooldown remains, as do legacy persisted cooldowns. Installation does not discard ownership, latches, pending work or recovery obligations.

- **Relay already On:** after manual heating is stopped, use **Acknowledge heater power recovery**. Matching saved wiring, fresh native Off and low dedicated load after the recovery boundary, qualified supply, no pending auxiliary work and durable storage are required. A retained already-On claim can be acknowledged in Preview. Acknowledgement sends no activation command and does not immediately clear every safe-interval/cooldown hold.
- **Guard-owned relay Off:** explicitly call house_climate.approve_heater_restore with confirm true after resolving the cause. Live permission, matching ownership, the applicable cooldown, a dedicated meter and fresh physical relay-Off evidence are required. Approval lasts 120 seconds and is not restored from storage. One bounded restoration attempt uses the existing ten-second relay-On/native-Off verification window. Failure recuts and requires renewed approval; the overall restore-attempt cap remains three.

For supported Matter dedicated switches, recovery performs one read-only OnOff attribute request and uses the returned device value, including unchanged On or Off. It does not cycle power, re-interview the device or fabricate a HA report. Wrong identity, endpoint, disconnection or failed reads fail closed. Other relay sources require the explicit physical snapshot contract. This stronger recovery proof is distinct from ordinary AC supply corroboration.

Auxiliary recovery ownership remains independent of other comfort ownership. Other heads and fans can continue once their own barriers clear. On ordinary exit, restart or fault, configured recovery handoff applies to devices Home Climate owns; a manually owned Dyson is excluded. Genuine unresolved faults can prevent parts of the handoff and require a critical alert rather than false success. A stopped HA process cannot issue commands until service resumes.

See [release acceptance](release-baseline-acceptance.md) and the separately armed [thermal experiment series](thermal-experiment-series.md). Neither installing the integration nor reading those protocols starts an experiment.
