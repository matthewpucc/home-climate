# Operating rules

The outdoor system supplies heat or cooling, one direction at a time. A direction change first requests Off from all heads and waits for acknowledgement before settling and starting the selected calls. A head that drops off Wi-Fi enters local recovery; healthy heads can continue when compatible with the shared direction.

## Temperatures and priorities

The comfort-card editing bounds remain 66–75°F. Native protection defaults are separate: heating below an active 60°F or 65°F floor and cooling at/above 80°F. Qualified outdoor state selects the floor; unknown outdoor data conservatively uses 65°F without claiming measured freezing. Generic configurations with no protection object retain legacy 66–75°F behavior. Office, entertainment, and guest normally target 72°F; living targets 68°F at priority 0.1. Primary has priority 10 during its nighttime 67–69°F band. Higher comfort priority favors a direction when rooms disagree; it does not command more compressor power.

External ambient readings determine room comfort. Median fusion tolerates one unavailable probe. Raw valid extremes preserve real room gradients for protection. If an area has no valid readings, its shared head is inhibited. Equipment temperatures are used only to compensate equipment setpoints, including the fixed 65°F minimum cooling setpoint on all four heads.

Raw opposing stop guards reserve 1°F inside the active protection bounds: with the native 80°F ceiling, heating stops by 79°F; cooling stops by 61°F for the normal 60°F floor or 66°F for the conservative/cold 65°F floor. Legacy 66–75°F protection still stops heating by 74°F and cooling by 67°F. A two-minute projection uses only a trend worsening the current call and may stop earlier. These guards create no opposite-direction request on their own. They run during fresh 15-second maintenance and override acknowledged minimum-run retention. The auxiliary heater also forecasts its earlier comfort cutoff.

The reserved margin responds to the observed room overshoot after a delayed Off. It is not a guarantee against arbitrary Wi-Fi outages, delayed probes, residual heat, or external loads.

## Shared-area comfort and house protection

A head serving several areas has to account for every attached area. Ordinary cooling stops at any area's lower comfort edge; ordinary heating stops at its upper edge. A comfort-only start or restart requires 0.5°F of headroom from those opposing edges. A cutoff remains latched across the 90-second decisions until that headroom returns, so a tiny rebound does not immediately start another short call. The margin reduces avoidable cycling; it cannot guarantee a five-minute run against changing loads or delayed measurements.

House protection takes priority over soft comfort preferences. A trusted room measurement below the active heating floor or at/above the cooling threshold triggers an actual emergency without a new indoor persistence delay. Existing corroboration still applies to lone disagreeing probes. Preventive reserves use the one-degree margin and existing forecast before the actual trip; diagnostics distinguish them. Explicit emergency priority releases one degree inside the bound by default, separately from ordinary comfort-target completion. A protective call can yield another area's soft comfort boundary while protection is required. Raw and forecast opposing house cutoffs always remain in force. If one attached area is too hot while another is too cold to condition safely, the head stays off and reports that conflict.

The same head eligibility rules apply to mode arbitration, fresh maintenance, and minimum-run retention. A protective call can bypass a comfort direction hold, but still requires device availability, acknowledged Off, settling, and hardware minimum off time. Room-card profile changes re-evaluate the current limits; they do not relax house protection.

## Optional window effects and outdoor conflict rules

A confirmed opening plus five minutes of a valid measured deficit at least 0.5°F
beyond the room's comfort edge can reduce that direction's comfort weight to 25%.
An already-cold steady room can qualify. Only the opposing soft boundary yields;
raw/forecast hard guards, missing-probe inhibition, fan safety and auxiliary
limits remain. Unknown/invalid data, confirmed closure, and profile-context changes
restore or requalify normal comfort behavior. This does not force thermostat Off.

When opposing actual emergencies coexist, the larger degree violation wins. Exact
ties use qualified outdoor regime; near-reference state is qualified with a draft
±2°F deadband and five-minute valid interval. Existing regimes persist in that band.
Unknown or initially unqualified ties default to heating, with configurable choices.
No unsafe shared head can be forced to run. See [protection rules](protection-temperatures.md)
and [window details](window-contacts.md) for engineering defaults and limitations.

## Forecasts after conditioning stops

A cooling or heating trend includes the effect of the head that produced it. When a room reaches its stopping target and no same-direction reserve request remains, the controller separates that completed conditioning from evidence for a new request. Before Off is reported, it does not project the previous call as another ten minutes of opposite-direction demand. After Off, new valid room readings build a separate post-stop forecast history.

This newer history survives a shared head restarting for another area and delayed active-state echoes. New measured warming or cooling can still create a protective request. A shared area that never requested the original conditioning retains its own forecast evidence. The original raw trend remains available to the two-minute temperature-stop guards.

An unstarted forecast request is rechecked while a head waits for minimum off time. If its forecast clears and no actual comfort or house-limit demand remains, the queued request is removed. Legacy protection can retain its target-reaching recovery hold after a reported start. Explicit protection uses its own release hysteresis while ordinary comfort keeps its target-reaching behavior. A different room's newly forecast need does not inherit that hold merely because both use the same head.

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

The wall real-power budget is `12 × min(phase A voltage, phase B voltage)` watts, using fresh, valid wall-phase measurements. For example, two valid phases whose lower voltage is 123 V give a 1476 W budget. There is no fixed 1350 W cap, extra 0.95 multiplier or nominal 120 V ceiling. Missing, stale or invalid voltage inhibits heating; it cannot supply a nominal-voltage fallback.

The complete circuit and Elite grid input qualify separately, never summed. A source must remain at or above its budget for at least 30 seconds across at least three distinct fresh reports before the wall-load condition sheds a running heater. Reports from different sources cannot be combined to reach that count. Cached duplicates and voltage-only changes do not add power reports; an unchanged value with a genuinely newer report can count. Invalid feedback or a reporting gap invalidates pending qualification. Three reports may take longer than 30 seconds at the source's actual cadence.

Pending wall-load qualification defers new heater starts but does not itself stop an existing call. The other fixed protections remain independent:

- Elite AC output at or above 2000 W sheds immediately.
- Elite battery at or below 40% sheds immediately.
- Missing, stale, implausible or incorrectly unit-labelled required measurements inhibit heating under the existing telemetry rules.
- Temperature protections and shutdown acknowledgements retain their existing behavior.

This is an explicit real-watt budget, not measured RMS branch current or a physical 12 A guarantee. Power factor, unknown supply phase, measurement error and report timing limit that inference. Heater-plug amps are downstream of the battery and remain recovery evidence only. Battery-assisted output may exceed grid input; neither the wall budget nor a 12 A output ceiling is applied to that output. The separate 2000 W output limit is an operational shedding threshold, not an inverter rating.

The guard first requests Dyson thermostat Off. If fresh Off is not observed within 5 seconds, it uses the optional dedicated heater plug and then an explicitly enabled [shared AC emergency fallback](heater-emergency-cutoff.md). A hung thermostat service cannot hold fallback behind the same await or lock. High grid charging or another circuit load can remain after the heater confirms Off. That retains the wall-load diagnostic and inhibits new heating; it is not heater-stop failure and does not create another shared-AC cutoff reason. AC output Off is not charging Off.

A wall-only event can also begin while the heater already reports fresh, trusted Off. With no outstanding command boundary, actual service/cleanup work or conflicting meter evidence, the guard can retain that passive state-only evidence without sending another Off. Passive evidence is separate from a sent-command acknowledgement, expires with its source and is discarded on restart or a real Off attempt. It neither establishes physical isolation nor clears ownership or recovery latches. A configured heater meter must remain consistent with Off; an absent meter remains unknown.

Rearming requires circuit and input strictly below 85% of the current voltage-derived budget, battery strictly above 40%, 60 seconds of continuously safe load and the remaining 5-minute trip cooldown. Existing output/current recovery requirements remain in force. Only an eligible dedicated plug cut by this guard may be restored, with fresh physical plug feedback and thermostat Off. Shared AC recovery stays manual. Normal room demand decides whether heat resumes; battery recovery never forces heating.

These are fixed policy rules, with no new tuning fields. Existing power and voltage bindings are sufficient; no JSON threshold or mapping changes are needed. Installing the change does not erase persisted cutoff or manual-recovery latches.

The optional dedicated-watts feature selects `heater_power` (W/kW) instead of `heater_current` (A/mA); configuring both is rejected. Its strict low-load bound is 100 W, while the legacy amp path retains its strict 1 A bound. These classify load after native Off rather than enforce an RMS-current ceiling. Meter reports must stay fresh within 180 seconds; neither stale-high nor stale-low values authorize activation, rearm, restoration or electrical-Off proof. Native mode Off and post-command meter feedback remain separate requirements. A thermostat-idle transition is not an Off acknowledgement. New watt-based restoration also requires fresh low watts after its actual Off request within the existing restoration deadline, otherwise it cuts again and remains inhibited. The five-second native-Off deadline and independent wall/output/battery/temperature protections are unchanged.

The cutoff latch and restoration ownership persist through restart. Restoration retries are bounded. Actual dedicated-switch Off feedback can prove the heater is isolated even while its unpowered integration is unavailable, allowing shutdown cleanup to finish. A requested but unacknowledged power cut is not proof.

## Visibility and stopping

The device exposes room plans, controller/recovery status, current issues, last controller fault, and a separate heater-power-guard status. Local device warnings are distinct from controller faults. Shared-room constraints name the blocking room, current temperature, required headroom, and waiting request; protection overrides are identified separately. Full structured constraints remain available when a sensor state is shortened for HA's 255-character limit.

Controller status also retains up to 16 transition events per head. Command requests, service completion, device acknowledgement, and reported mode changes are separate events. Repeated unchanged reports do not displace command evidence. An unexpected Off report is recorded without assuming who issued it or that the physical compressor stopped; it does not alone initiate capability-loss repair.

Pausing comfort requests equipment Off and retains ownership until shutdown is confirmed. A settings save waits for cleanup before reloading. A Home Assistant unload also waits for ambiguous in-flight commands to become safe; it must not allow an old delayed activation to energize a heater after the guard has unloaded.

A timed-out or cancelled caller does not prove that its actuator service has finished. Actual jobs remain tracked through completion, including queued and delayed Off jobs. A late positive completion requires new superseding Off evidence, and stops from a previous ownership or repair generation cannot stop a later call.

No automated integration reload, fallback outlet operation, or software timer can repair a failing heat-pump Wi-Fi adapter. Recovery retries isolate those failures and report the remaining block.
