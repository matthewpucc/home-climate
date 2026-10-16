# Control behavior

## Sensor fusion and priorities

The controller polls every 15 seconds by default. It rejects stale, unavailable, nonfinite, or implausible probe readings, converts units, applies configured offsets, and takes a median across physical sources. Freshness uses `last_reported` where available, falling back to `last_updated`.

Single raw extremes immediately inhibit conditioning that could worsen the opposite limit. Extremes persisting for the confirmation interval also contribute to demand. A bounded recent temperature trend provides up to ten minutes of anticipation by default.

Heat/cool arbitration compares actual limit violations, forecast limit pressure, configured comfort priority, and severity, in that order. Safety-tier urgency uses the worst room rather than summed votes. Every room attached to a head must have usable readings before that head may run.

Heat/cool direction uses hysteresis, dwell, and sustained competing demand. Default limits are 66–75°F, with proactive demand near 67°F/74°F. These are configurable control objectives; they cannot guarantee physical bounds.

## Device execution

Startup, direction change, mixed reported modes, and recovery request all heads off. Every head must report off before the settling timer begins. Selected heads then receive one shared direction. Pending commands time out when state acknowledgement does not arrive.

Changing active rooms within the same direction does not restart all heads. Device temperature ranges, steps, and fan percentage steps are respected. HA-reported state is the available acknowledgement, not proof of the compressor's physical state.

The provisional settling interval is 180 seconds. Verify it for the equipment. Software timing supplements manufacturer protections.

## Auxiliary heating

The heater's internal thermostat is bypassed operationally by requesting a verified high setpoint and explicitly turning the heater off from external room readings. Hysteresis uses the active room profile. A hot individual probe stops heating even when the median is cold.

Heating and fan-only are mutually exclusive. A heat-to-fan transition first commands off and confirms it on a subsequent poll. Fan-only never sends the high heating setpoint.

The separate HA watchdog acts while the controller owns the heater. Every 15 seconds and on relevant state/startup events, it commands off when disabled or when the heartbeat is older than 120 seconds. When heating is possible, no valid external probe or any valid probe at the independent cutoff also triggers off. Confirmed fan-only operation is allowed above the heating cutoff unless the device reports heating.

A mapping signature prevents live control with an outdated watchdog package. The watchdog does not depend on the Python worker reporting itself healthy, but it does depend on HA and the device integration continuing to work.

## Ownership and failure behavior

Shadow mode rejects service writes at the API adapter. Diagnostic entities still expose proposed decisions. Live operation requires explicit configuration, the HA enable helper, and successful preflight.

While enabled, House Climate owns its mapped devices. Disable it before manual operation or using competing automations. Normal disable/shutdown attempts off for all owned devices. It does not restore an earlier device schedule.

An unavailable actuator or unacknowledged command stops live execution and triggers shutdown attempts. Recovery begins from the all-off barrier. If auxiliary shutdown is unconfirmed, ownership remains armed for watchdog retries.

The native integration serializes its worker lifecycle and refuses a second loaded instance. Reload waits for shutdown; a stuck worker blocks successful reload. The standalone CLI additionally has a host-local process lock. Neither provides a distributed lock across separate hosts.

If the process stops, shared-system heads can retain their last native setpoints. Only the auxiliary heater has an independent HA watchdog. If HA or the host stops, its automations cannot run. Physical device protections remain necessary.

## Current scope

One shared heat/cool system, one optional auxiliary heater, daily schedules, and percentage-capable fans are supported. Multiple outdoor systems, occupancy/calendar schedules, preconditioning, automatic calibration, and restoration of prior device settings are not implemented.
