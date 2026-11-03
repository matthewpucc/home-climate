# Operating rules

The outdoor system supplies heat or cooling, one direction at a time. A direction change first requests Off from all heads and waits for acknowledgement before settling and starting the selected calls. A head that drops off Wi-Fi enters local recovery; healthy heads can continue when compatible with the shared direction.

## Temperatures and priorities

The comfort-card editing bounds remain 66–75°F. Native protection defaults are separate: heating below an active 60°F or 65°F floor and cooling at/above 80°F. Qualified outdoor state selects the floor; unknown outdoor data conservatively uses 65°F without claiming measured freezing. Generic configurations with no protection object retain legacy 66–75°F behavior. Configurable weekly schedules define each room's target, band and priority; limits-only periods enforce their own scheduled bounds without target seeking. Primary retains priority 10 during its nighttime 67–69°F band. Higher comfort priority favors a direction when rooms disagree; it does not command more compressor power.

Explicitly bound room probes determine comfort, including an externally located heat-pump probe when assigned to its actual room. Aliases of one physical probe count once; a shared head's probe does not represent two rooms. Living's internal heat-pump reading remains excluded from normal room fusion pending comparison. Median fusion tolerates one unavailable probe. Raw valid extremes preserve real gradients for protection. If an area has no valid readings, its shared head is inhibited. Equipment setpoint compensation remains separate, including the 65°F minimum cooling setpoint on the four house heads. See [source evidence](source-evidence.md).

Raw opposing stop guards reserve 1°F inside the active protection bounds: with the native 80°F ceiling, heating stops by 79°F; cooling stops by 61°F for the normal 60°F floor or 66°F for the conservative/cold 65°F floor. Legacy 66–75°F protection still stops heating by 74°F and cooling by 67°F. A two-minute projection uses only a trend worsening the current call and may stop earlier. These guards create no opposite-direction request on their own. They run during fresh 15-second maintenance and override acknowledged minimum-run retention. The auxiliary heater also forecasts its earlier comfort cutoff.

The reserved margin responds to the observed room overshoot after a delayed Off. It is not a guarantee against arbitrary Wi-Fi outages, delayed probes, residual heat, or external loads.

## Shared-area comfort and house protection

A head serving several areas has to account for every attached area. Ordinary cooling stops at any area's lower comfort edge; ordinary heating stops at its upper edge. A comfort-only start or restart requires 0.5°F of headroom from those opposing edges. A cutoff remains latched across the 90-second decisions until that headroom returns, so a tiny rebound does not immediately start another short call. The margin reduces avoidable cycling; it cannot guarantee a five-minute run against changing loads or delayed measurements.

House protection takes priority over soft comfort preferences. A trusted room measurement below the active heating floor or at/above the cooling threshold triggers an actual emergency without a new indoor persistence delay. Existing corroboration still applies to lone disagreeing probes. Preventive reserves use the one-degree margin and existing forecast before the actual trip; diagnostics distinguish them. Explicit emergency priority releases one degree inside the bound by default, separately from ordinary comfort-target completion. A protective call can yield another area's soft comfort boundary while protection is required. Raw and forecast opposing house cutoffs always remain in force. If one attached area is too hot while another is too cold to condition safely, the head stays off and reports that conflict.

The same head eligibility rules apply to mode arbitration, fresh maintenance, and minimum-run retention. A protective call can bypass a comfort direction hold, but still requires device availability, acknowledged Off, settling, and hardware minimum off time. Room-card profile changes re-evaluate the current limits; they do not relax house protection.

## Optional window effects and outdoor conflict rules

A confirmed opening plus five minutes of a valid measured deficit at least 0.5°F
beyond the room's comfort edge can reduce that direction's comfort weight to 25%.
An already-cold steady room can qualify. Confirmed openings yield ordinary comfort vetoes for both Heat and Cool; the measured deficit controls the separate priority discount. Only the opposing soft boundary yields;
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

Primary's purifier is 60% from 10 p.m.–9 a.m., including the 5–9 a.m. morning period, and off otherwise. Guest circulation accompanies conditioning. Office and entertainment fans move the bonus head's air toward the office. The living Dyson may circulate without heat where the room plan calls for it and whole-device supply/fault restrictions permit it.

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

## Auxiliary heat startup

A temperature request does not necessarily turn a climate entity on. Some
integrations accept a combined temperature/Heat request but only program the
thermostat while its power remains Off. Auxiliary startup therefore confirms the
intended target and explicitly requests Heat when the device is not already in
that mode. An already-Heat device can update its target without a power cycle.

Target programming itself may enable heating on a powered device, so both stages
remain positive commands subject to the existing probe, ownership, heartbeat and
electrical interlocks. The sequence shares one existing command-timeout budget;
the native adapter carries its original deadline through HA's queue rather than
granting a new service window at the second stage. An adapter that cannot enforce
this deadline defers a staged start instead of sending an unbounded call.
Ordinary heat-pump commands and shutdown retain their existing paths.

Completion requires the requested target and Heat-mode feedback. An API receipt
or an optimistically updated mode is not independent proof of physical heating.
The separate power guard and meter supervision remain effective. Rejection,
timeout, changed intent or interrupted ownership uses existing Off reconciliation
and pending-job cleanup; it does not permit an old delayed Heat request to start
after a stop. No new startup grace, meter threshold, automatic shared-AC recovery
or configuration field is introduced.

A failed staged start is held for the remainder of that continuous heat request.
Internal Off reconciliation does not start another On loop. Rearming requires a
non-heat request, trusted fresh Off feedback, and completion of actual pending or
uncertain service work. Controller toggles alone are not a recovery
acknowledgement. Existing independent persisted guard latches remain separate.

## Heater load shedding

The independent electrical guard runs on cached-state events and 5-second checks while native live commands are allowed. It remains active when comfort is disabled. Clean Preview does not command equipment. Saved ownership can still require negative cleanup, and an explicitly armed terminal handoff retains its saved live authorization; switching to Preview does not erase those obligations. The explicit retained-cut acknowledgement can save its result in Preview without commanding equipment.

Automated auxiliary eligibility requires battery above 40%, reported AC output enabled corroborated by current supply telemetry, and valid aggregate output. Positive output corroborates supply; zero is inconclusive before startup. Input, circuit, output totals and phase voltage have no software watt ceiling. Input/circuit/phase telemetry stays diagnostic. Missing dedicated watts are unknown, not a normal admission fault; fresh low load remains necessary for explicit recovery and electrical-stop proof.

The [auxiliary supply policy](auxiliary-supply-policy.md) specifies report provenance, ages, migration prerequisites and approval semantics. Ordinary corroboration does not require a separate physical AC read. The existing five-second native-Off acknowledgement and state-only confirmation remain, with dedicated cutoff and explicitly enabled shared AC fallback on genuine failure. Aggregate-stop corroboration remains separate from watt ceilings.

Successful ordinary stops can recover after 60 seconds of stable safe evidence. Failed shutdown, persisted cutoff ownership and emergency recovery require explicit operator action; the existing five-minute failed-shutdown/legacy cooldown remains. Dedicated restoration retains its ten-second verification window and post-Off low-watt evidence. Shared AC restoration remains manual. No ownership or durable cleanup obligation is discarded to enable other rooms.

Unresolved auxiliary cleanup is saved independently of head/fan ownership, so normal room comfort can continue after its own barriers clear. Live permission is required to claim active room control; Preview does not run comfort. Previously armed terminal recovery and outstanding negative cleanup retain their separate durable authorization. Supervised commissioning retains its existing stopped-controller prerequisite and timing.

## Visibility and stopping

The device exposes room plans, controller/recovery status, current issues, last controller fault, and a separate heater-power-guard status. Local device warnings are distinct from controller faults. Shared-room constraints name the blocking room, current temperature, required headroom, and waiting request; protection overrides are identified separately. Full structured constraints remain available when a sensor state is shortened for HA's 255-character limit.

Controller status also retains up to 16 transition events per head. Command requests, service completion, device acknowledgement, and reported mode changes are separate events. Repeated unchanged reports do not displace command evidence. An unexpected Off report is recorded without assuming who issued it or that the physical compressor stopped; it does not alone initiate capability-loss repair.

Transitions away from active control use the configured recovery handoff, including ordinary disable, restart and fault. House defaults request dual-setpoint Auto 68–73°F on eligible heads, auxiliary Off and fans at 40%. A manually owned Dyson is excluded. Actual faults, pending commands and shared-system barriers can prevent parts of the handoff; incomplete work remains owned and visible rather than falsely acknowledged. A settings save waits for bounded handoff/cleanup before reloading. Unload must not allow a delayed old activation to energize a heater after its guard has gone away. A process crash cannot issue commands at the instant of failure; startup reconciles saved obligations.

A timed-out or cancelled caller does not prove that its actuator service has finished. Actual jobs remain tracked through completion, including queued and delayed Off jobs. A late positive completion requires new superseding Off evidence, and stops from a previous ownership or repair generation cannot stop a later call.

No automated integration reload, fallback outlet operation, or software timer can repair a failing heat-pump Wi-Fi adapter. Recovery retries isolate those failures and report the remaining block.

### Taking control after fallback

Native Controller On supersedes an unfinished fallback preset. An incorrect reported Auto range does not have to be corrected to 68–73°F before takeover. Ownership is retained while actual old calls finish; queued fallback writes are fenced, and fresh shared-head Off must be confirmed before ordinary acquisition and its equipment timers. Unavailable heads or uncertain dispatched commands still block admission. The auxiliary retains its independent fault, cut-ownership, stop and manual-control protections.

The native controller persists explicit On/Off intent. With saved On and Allow Live enabled, startup uses this same reconciliation path. Graceful stop, unload and options reload preserve intent; explicit Off, active-control faults, unexpected worker exit and heartbeat expiry inhibit automatic resumption. Preflight/readiness errors before a lease is acquired remain retryable under existing behavior. Legacy storage without this field starts Off; the first explicit On establishes it. Shadow startup does not grant control. A completed fallback followed by On is also acquired through normal startup reconciliation.

Status exposes `enabled_intent` and `takeover_pending`. While takeover waits, the internal worker admission remains Off even if the requested Controller switch is On. A delayed worker release from that cleanup overlay cannot cancel the newer request. Turning Controller Off during takeover cancels admission and retains the terminal fallback obligation.

This change adds no positive fallback retry. Off cleanup continues under the existing supervisor; each fallback preset is sent once, with the existing 120-second feedback window and explicit recovery review on failure. No shutdown-event ordering or timeout changes are included.
