# Operating rules

The outdoor system supplies heat or cooling, one direction at a time. A direction change first requests Off from all heads and waits for acknowledgement before settling and starting the selected calls. A head that drops off Wi-Fi enters local recovery; healthy heads can continue when compatible with the shared direction.

## Temperatures and priorities

The fixed house bounds are 66–75°F. Office, entertainment, and guest normally target 72°F; living targets 68°F at priority 0.1. Primary has priority 10 during its nighttime 67–69°F band. Higher comfort priority favors a direction when rooms disagree; it does not command more compressor power.

External ambient readings determine room comfort. Median fusion tolerates one unavailable probe. Raw valid extremes preserve real room gradients for protection. If an area has no valid readings, its shared head is inhibited. Equipment temperatures are used only to compensate equipment setpoints, including the fixed 65°F minimum cooling setpoint on all four heads.

The source thresholds reserve 1°F before the house bounds: stop heating by 74°F and cooling by 67°F. A two-minute projection uses only a trend worsening the current call and may stop earlier. These guards create no opposite-direction request on their own. They run during fresh 15-second maintenance and override acknowledged minimum-run retention. The auxiliary heater also forecasts its earlier comfort cutoff.

The reserved margin responds to the observed room overshoot after a delayed Off. It is not a guarantee against arbitrary Wi-Fi outages, delayed probes, residual heat, or external loads.

## Timing and fan behavior

Comfort decisions run every 90 seconds; maintenance runs every 15 seconds. An acknowledged head run normally lasts at least 5 minutes, and a stopped head waits 3 minutes before restarting. A run begins after both the mode and exact target are confirmed. Safety, missing measurements, direction changes, and recovery may stop it sooner.

Primary's purifier is 60% from 10 p.m.–5 a.m. and off otherwise. Guest circulation accompanies conditioning. Office and entertainment fans move the bonus head's air toward the office. The living Dyson may circulate without heat where the room plan calls for it.

Native heartbeat confirmation tolerates brief HA responsiveness delays. A delayed confirmation defers new starts and adjustments while keeping confirmed calls. The heat-pump lease is 5 minutes; the auxiliary heater has a separate 2-minute lease. Lease expiry and controller-wide stops are logged with their cause.

## Heater load shedding

The independent electrical guard runs on cached-state events and 5-second checks while native live commands are allowed. It remains active when comfort is disabled. Shadow mode performs no writes.

Fixed triggers:
- Complete Emporia circuit or Elite grid input at or above 1350 W. A low measured wall voltage reduces the budget.
- Elite output at or above 2000 W.
- Elite battery at or below 40%.
- Missing, stale, implausible, or incorrectly unit-labelled required measurements inhibit heating.

The circuit and Elite input are compared separately, never summed. Emporia measures real watts, not full-circuit RMS amps; the 1350 W input budget is a conservative proxy for the 12 A ceiling. Heater-plug amps are downstream of the battery and serve recovery confirmation only. The 12 A ceiling applies to circuit/input, not battery output; valid battery-assisted operation above 12 A at the heater plug is allowed. The 2000 W output limit is an operational shedding threshold, separate from the input limit.

The guard first requests Dyson thermostat Off. If a fresh Off report is not observed within 5 seconds, it requests Off from the dedicated heater plug. A hung thermostat service cannot hold that fallback behind the same await or lock. Other heat pumps and all network/battery outlets remain separate.

Power restoration requires battery strictly above 40%, stable safe load for 60 seconds, and a 5-minute cooldown. Only a plug cut by this guard may be restored. Restoration also requires fresh physical plug feedback and thermostat Off before new heating is allowed. Normal room demand decides whether heat resumes; battery recovery never forces heating.

The cutoff latch and restoration ownership persist through restart. Restoration retries are bounded. Actual dedicated-switch Off feedback can prove the heater is isolated even while its unpowered integration is unavailable, allowing shutdown cleanup to finish. A requested but unacknowledged power cut is not proof.

## Visibility and stopping

The device exposes room plans, controller/recovery status, current issues, last controller fault, and a separate heater-power-guard status. Local device warnings are distinct from controller faults.

Pausing comfort requests equipment Off and retains ownership until shutdown is confirmed. A settings save waits for cleanup before reloading. A Home Assistant unload also waits for ambiguous in-flight commands to become safe; it must not allow an old delayed activation to energize a heater after the guard has unloaded.

No automated integration reload, fallback outlet operation, or software timer can repair a failing heat-pump Wi-Fi adapter. Recovery retries isolate those failures and report the remaining block.
