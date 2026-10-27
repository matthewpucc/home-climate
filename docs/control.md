# Operating rules

The outdoor system supplies heat or cooling, one direction at a time. A direction change first requests Off from all heads and waits for acknowledgement before settling and starting the selected calls. A head that drops off Wi-Fi enters local recovery; healthy heads can continue when compatible with the shared direction.

## Temperatures and priorities

The fixed house bounds are 66–75°F. Office, entertainment, and guest normally target 72°F; living targets 68°F at priority 0.1. Primary has priority 10 during its nighttime 67–69°F band. Higher comfort priority favors a direction when rooms disagree; it does not command more compressor power.

External ambient readings determine room comfort. Median fusion tolerates one unavailable probe. Raw valid extremes preserve real room gradients for protection. If an area has no valid readings, its shared head is inhibited. Equipment temperatures are used only to compensate equipment setpoints, including the fixed 65°F minimum cooling setpoint on all four heads.

The source thresholds reserve 1°F before the house bounds: stop heating by 74°F and cooling by 67°F. A two-minute projection uses only a trend worsening the current call and may stop earlier. These guards create no opposite-direction request on their own. They run during fresh 15-second maintenance and override acknowledged minimum-run retention. The auxiliary heater also forecasts its earlier comfort cutoff.

The reserved margin responds to the observed room overshoot after a delayed Off. It is not a guarantee against arbitrary Wi-Fi outages, delayed probes, residual heat, or external loads.

## Shared-area comfort and house protection

A head serving several areas has to account for every attached area. Ordinary cooling stops at any area's lower comfort edge; ordinary heating stops at its upper edge. A comfort-only start or restart requires 0.5°F of headroom from those opposing edges. A cutoff remains latched across the 90-second decisions until that headroom returns, so a tiny rebound does not immediately start another short call. The margin reduces avoidable cycling; it cannot guarantee a five-minute run against changing loads or delayed measurements.

House protection takes priority over these soft comfort preferences. Confirmed ambient extremes and the existing forecast can request cooling near 74°F or heating near 67°F, reserving room before the 75°F/66°F bounds. A protective call may continue past another area's soft comfort edge until its originating area returns to its target or safe interior. Raw and forecast opposing house cutoffs always remain in force. If one attached area is too hot while another is too cold to condition safely, the head stays off and reports that conflict.

The same head eligibility rules apply to mode arbitration, fresh maintenance, and minimum-run retention. A protective call can bypass a comfort direction hold, but still requires device availability, acknowledged Off, settling, and hardware minimum off time. Room-card profile changes re-evaluate the current limits; they do not relax house protection.

## Forecasts after conditioning stops

A cooling or heating trend includes the effect of the head that produced it. When a room reaches its stopping target and no same-direction reserve request remains, the controller separates that completed conditioning from evidence for a new request. Before Off is reported, it does not project the previous call as another ten minutes of opposite-direction demand. After Off, new valid room readings build a separate post-stop forecast history.

This newer history survives a shared head restarting for another area and delayed active-state echoes. New measured warming or cooling can still create a protective request. A shared area that never requested the original conditioning retains its own forecast evidence. The original raw trend remains available to the two-minute temperature-stop guards.

An unstarted forecast request is rechecked while a head waits for minimum off time. If its forecast clears and no actual comfort or house-limit demand remains, the queued request is removed. Once a room has a reported protective start, that room's recovery can retain its target-reaching hold. A different room's newly forecast need does not inherit that hold merely because both use the same head.

## Timing and fan behavior

Comfort decisions run every 90 seconds; maintenance runs every 15 seconds. An acknowledged head run normally lasts at least 5 minutes, and a stopped head waits 3 minutes before restarting. A run begins after both the mode and exact target are confirmed. House protection, an opposing comfort boundary on ordinary calls, missing measurements, direction changes, and recovery may stop it sooner.

Primary's purifier is 60% from 10 p.m.–5 a.m. and off otherwise. Guest circulation accompanies conditioning. Office and entertainment fans move the bonus head's air toward the office. The living Dyson may circulate without heat where the room plan calls for it.

Transfer fans account for the source head's planned direction, reported mode/action, and possible residual conditioned air. Cooling at the source stops transfer into a destination at or below its target; heating stops transfer into a destination at or above its target. A warmer ambient source probe does not establish that an actively cooling head is supplying useful warming air.

Each possible conditioning direction remains tracked through pending starts and mode changes. Fresh idle feedback followed by the existing settling interval permits ambient transfer again. Unknown or contradictory source feedback is treated conservatively. An external direction change without an observed idle interval can keep transfer fans off until a later confirmed idle period. Useful idle transfer, compatible conditioning assistance, and independent scheduled fans retain their own rules.

Native heartbeat confirmation tolerates brief HA responsiveness delays. A delayed confirmation defers new starts and adjustments while keeping confirmed calls. The heat-pump lease is 5 minutes; the auxiliary heater has a separate 2-minute lease. Lease expiry and controller-wide stops are logged with their cause.

## Integration capability recovery

The native integration learns which managed modes a particular head normally advertises from three fresh, consistent reports over at least 120 seconds. Knowledge is stored locally for the owning integration, registered device identity, and explicit preset/operation context. Ordinary Heat, Cool, and Off transitions do not erase it. A changed device identity or operating context starts separate learning.

A first-seen shortened mode list is not proof of a failure: a head may genuinely support only cooling. If installation begins while modes are already missing, use that head's **Reload integration** action to obtain healthy reports before expecting automatic loss detection. A capability-store failure produces a warning and falls back to runtime observations; it does not stop climate control.

Previously established modes disappearing from current HA attributes starts a capability check. Automatic repair requires native live control to be enabled, at least three explicit failed refresh/rechecks spaced 60 seconds apart, and at least 120 seconds of persistent loss. Repeated reads of the same cached state are not extra failed device attempts. Startup, unavailable data, an ordinary Off state, and a valid cached last mode are not, by themselves, proof of capability loss. A restored report breaks the consecutive-failure sequence.

Loss of a mode needed by an eligible current request can justify coordinated repair. Loss of an unused mode is advisory and waits for a safe idle opportunity, particularly while a healthy head is providing house protection. Current decision inputs are checked before pausing and reloading; changed measurements, settings, or schedules defer to a fresh worker plan. The repair's own Off commands do not establish an idle opportunity. Repair applies to the owning integration entry, so configured sibling heads can pause and other devices in that entry can briefly become unavailable. Unrelated integration entries are outside that repair scope.

Before reload, a per-entry command barrier rejects old queued starts and waits for actual dispatched service jobs to finish. Healthy siblings must confirm Off before their integration is unloaded. The failed head's missing Off acknowledgement cannot itself prevent the reload intended to repair its feedback. Fresh safety Off requests remain permitted. If completion or healthy-head shutdown cannot be established, repair stays blocked and reports why.

Reload success is not proof of device Off. Rejoining requires fresh post-reload Off feedback and normal off-time checks. Calls then use the head's currently validated modes; a still-missing mode remains blocked without withholding all otherwise valid calls solely for that loss. The incident and attempt budget clear only after the frozen missing modes return in fresh, stable reports. Reload attempts use 15/30/60-minute backoff and pause after three attempts in one unresolved incident.

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

The device exposes room plans, controller/recovery status, current issues, last controller fault, and a separate heater-power-guard status. Local device warnings are distinct from controller faults. Shared-room constraints name the blocking room, current temperature, required headroom, and waiting request; protection overrides are identified separately. Full structured constraints remain available when a sensor state is shortened for HA's 255-character limit.

Controller status also retains up to 16 transition events per head. Command requests, service completion, device acknowledgement, and reported mode changes are separate events. Repeated unchanged reports do not displace command evidence. An unexpected Off report is recorded without assuming who issued it or that the physical compressor stopped; it does not alone initiate capability-loss repair.

Pausing comfort requests equipment Off and retains ownership until shutdown is confirmed. A settings save waits for cleanup before reloading. A Home Assistant unload also waits for ambiguous in-flight commands to become safe; it must not allow an old delayed activation to energize a heater after the guard has unloaded.

A timed-out or cancelled caller does not prove that its actuator service has finished. Actual jobs remain tracked through completion, including queued and delayed Off jobs. A late positive completion requires new superseding Off evidence, and stops from a previous ownership or repair generation cannot stop a later call.

No automated integration reload, fallback outlet operation, or software timer can repair a failing heat-pump Wi-Fi adapter. Recovery retries isolate those failures and report the remaining block.
