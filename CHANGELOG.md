# Changelog

## 0.9.8

- Add direct native numeric guard readiness/block/reason and configured reserve, persistent auxiliary manual ownership, per-fan requested/reported speed and sweep bounds with separate acknowledgment/pending evidence, and per-source report/acquisition times and qualification. Reuse captured observations and the existing 15-second sensor group; stale evidence becomes unknown without evaluating safety or control during refresh. Preserve all control behavior and gates. See [the complete native metric reference](docs/metrics.md), including existing exports, bounded codes, freshness, restart and physical-evidence limits.

## 0.9.7

- Accept Home Assistant's integer feature flags in native purifier airflow capability checks while rejecting boolean and malformed masks. Preserve required feature bits, services, outlet calibration and all existing dispatch/acknowledgement gates.

## 0.9.6

- Extend bounded shutdown-recovery retries to heads which remain Off after a completed preset, including Off reports without target ranges. Require post-completion feedback, no intervening mode transition, current compatible shared-head states and no pending, uncertain or repair-held head work. Mode and range retries share three total attempts, 15-second spacing and the original feedback deadline; retain ownership, operator, persistence, restart and queued-dispatch safeguards.
- Check Kumo's current cached Auto profile before recovery writes, independently of its advertised HA modes. Report rejected or unverified support without repeating unsupported commands; explicit service validation rejections also inhibit retries. This reads provider evidence only and does not repair, reload or modify an integration. No deployment or live configuration changes.

## 0.9.5

- Add demand-directed per-device purifier sweeps with explicit room intent, provisional calibration metadata, direction confirmation/dwell, and stable minimum-speed baseline. Office can use its accepted Entertainment-facing sweep or provisionally accepted Office-facing sweep; Entertainment retains only its known local circulation profile. Comfort aim does not create or satisfy HVAC demand, and limits-only periods do not invent comfort demand from nominal targets.
- Stage outlet and atomic sweep acknowledgement before direction-dependent boosts through a guarded native hass-dyson adapter. Retain purifier minimums and existing speed quantization; recheck identity, ownership, lifecycle, current demand and allowed speed at actual dispatch. Expose requested/reported settings and independent failures in Controller status. Generic/legacy rules remain opt-in; CLI/add-on directed configurations safely withhold unsupported boosts. See [directed airflow](docs/directed-airflow.md) for migration and later calibration. No deployment, live binding or protection-policy changes.
- Distinguish switchable Office outlet feedback from Entertainment's explicitly accepted fixed-outlet local sweep. Pin fixed calibration privately to its registered device across reloads; never infer support from a default Forward string, issue unsupported direction writes, or command reverse. Both modes still require fresh matching sweep and enabled-oscillation feedback before boost.

## 0.9.4

- Extend native diagnostics with bounded guard trip/Off-job/electrical evidence and explicitly unattributed HA Off requests. Keep local guard execution separate from unknown external callers, service completion separate from feedback, and whole-supply drop corroboration separate from dedicated load. No control-policy or live configuration changes. See `docs/emergency-shutdown-diagnostics.md`.

- Add native auxiliary authorization and Off-operation diagnostic sensors with cumulative event counts, precise event times, ownership/freshness evidence and bounded downloadable history. Export numeric states directly through Home Assistant Prometheus without helpers. Diagnostics are observational and reset on integration reload; control policy, safety timing and command behavior are unchanged. See `docs/auxiliary-diagnostics.md`.

## 0.9.3

- Preserve saved purifier minimums when deciding whether a late fan report cancels a bounded recovery retry. A report below the saved floor or fallback target keeps the valid retry available instead of consuming it without dispatch.

- Keep ordinary house purifiers at a configurable 20% minimum while preserving higher scheduled and thermal-assist speeds. Thermal transfer vetoes remove the boost, not the independent purification floor. Add an optional explicitly bound separate Living purifier, default 100%; no auxiliary-heater fan alias is inferred or controlled. Generic configurations retain zero minimum unless selected.
- Preserve saved purifier minimums during safe recovery handoff, above the configured fallback fan speed, while retaining mandatory Off cleanup and fault/ownership gates. Reject registered heater aliases before setup and every native fan dispatch; explicit minimums require distinct registered fan/heater device identities before activation. Add purifier policy fields to Configure; existing four-fan bindings remain valid, and the optional fifth purifier requires an explicit private binding. No live configuration, auxiliary policy or equipment timing changes.
- Retry a completed fallback preset with a fresh mismatched HA report up to three total attempts per device, at least 15 seconds after the previous service call finishes, within the original configured feedback window (120 seconds by default). Skip completed devices and retain operator-change, mode, ownership, persistence, interrupted-restart and uncertain-command barriers. A matching late report can still complete recovery without another command. Expose exhausted retries through the existing critical recovery notification. No equipment timing change or live configuration migration.
- Retain one bounded HA unload operation across caller cancellation, including storage waits, actual cleanup and platform removal. Give configured fallback its mandatory minimum Off and feedback windows instead of failing a healthy three-minute settle after two minutes. Keep unresolved operations and ownership on timeout, expose unload phase and sampled remaining time, and prevent a new worker during HA shutdown. Late cleanup does not automatically reset HA's failed-unload state.
- Reconcile delayed auxiliary Off feedback before evaluating its authorization deadline. Retain the completed stop's command ownership, stop epoch and dispatch time; require ordered fresh native feedback and drained, certain heater jobs. Reject restored, assumed, precommand and future reports. Confirmed power isolation remains independent stop proof. This neither renews authorization on Off nor clears an already latched expiry, and leaves shared-head cooling independent.

## 0.9.2

- Let explicit native Controller On supersede incomplete fallback preset confirmation, retaining real command lifetimes, uncertain-job ownership and fresh shared-head Off before normal acquisition. Fence queued fallback writes and invalidate earlier stop evidence after late head activation. Preserve independent auxiliary shutdown, cutoff and manual ownership.
- Persist explicit Controller On/Off intent separately from temporary execution state. Resume after graceful restart only with saved On and Allow Live; operational faults inhibit resumption. Legacy storage without saved intent starts Off. Expose takeover progress without claiming the abandoned fallback matched. No new positive fallback retries, equipment timers, watt limits or live configuration changes.
- Add explicit per-room Hold alongside Schedule and Temporary. Persist the desired temperature, range and Hold mode across schedule boundaries and normal restarts; edits retain the active mode. Keep existing Temporary expiry, room validation, protection and independent auxiliary manual ownership. Save before publishing changes, including release. Add a per-room Resume schedule button and comfort-mode sensor for narrow Assist/MCP release with readback; exposure remains an explicit HA setup step. No live enablement, new equipment commands or private bindings migration.

## 0.9.1

- Normalize automated auxiliary targets against the actual native climate unit, range and step, rounding down to preserve the configured Fahrenheit ceiling. Match HA's exact rendered target plus native target readback instead of confusing a native Celsius step with a Fahrenheit display step. For a whole-Celsius heater, an 80°F ceiling selects 26°C (78.8°F, displayed 79°F); 81°F does not acknowledge it. Recheck the native capability contract before both startup commands, retaining explicit Heat, fresh feedback, deadlines and uncertain-command cleanup. Missing native metadata inhibits auxiliary use locally. No configuration migration or watt-limit change.

- Show `manual_recovery_verified` after a durable heater recovery acknowledgement while fresh native Off and low dedicated-load evidence remain valid. Keep an outstanding latch or stable-supply wait visible, and withdraw the confirmation when its evidence is lost. This diagnostic-only correction does not invent a stop command, restore proof across restart, change equipment commands or alter recovery gates, timers, leases or persistence. No configuration migration is required.

## 0.9.0

- Make house room schedules, comfort bands, priorities, peak hours and terminal recovery settings editable while preserving private device bindings. Add start-day weekly overnight schedules, room-specific limits-only operation and seasonal Living profiles selected by a qualified 30-minute outdoor average with 59–61°F hysteresis. Retain the last profile during source loss; regional weather fallback needs an actual observation timestamp. Existing temporary holds remain bounded by schedule transitions.
- Include the Living heat pump in ordinary shared-system arbitration. Use auxiliary heat as supplementary or independent local heat, with a stronger efficiency bias during the configured peak window. Qualified observations govern sustained-deficit and recovery decisions; missing trends remain unknown. Set automated auxiliary heat to 80°F and let confirmed-open rooms yield ordinary comfort constraints in either direction while preserving protection limits and equipment timers.
- Replace software input/output watt ceilings and the circuit exception with battery above 40% and corroborating supply signals. An available reported AC-On state plus fresh battery/output reports can qualify ordinary admission without a separate physical AC read; zero output before startup is inconclusive. Keep totals diagnostic and retain existing shutdown timing, state-only acknowledgement and genuine fault handling. Required new AC wiring is explicit; see `docs/auxiliary-supply-policy.md`.
- Add explicit persistent manual takeover of the auxiliary only. Other room comfort continues; deliberate manual operation is not reclaimed by reserve policy, schedules or recovery presets. Low-battery critical alerts and genuine fault obligations remain. Returning control requires supported current evidence and no outstanding ownership or command uncertainty.
- Persist terminal recovery intent for controller-owned heads and fans when active control ends, including disable, restart and fault. Apply configured dual-setpoint recovery and circulation only after safe stop and protective timers. Preserve incomplete handoffs, manual changes and late jobs rather than claiming success. Retain auxiliary cleanup independently; failed-shutdown power restoration requires explicit approval and durable evidence. Read-only Matter feedback supports retained-cut acknowledgement without manufacturing freshness or cycling power.
- Renew controller health and auxiliary authorization independently, with centralized defaults of 300 and 120 seconds. Retain 120-second head activation confirmation; retry shared-head changeover Off at 0, 15 and 30 seconds, blocking changeover at 45 seconds if unconfirmed. Auxiliary native-Off timing remains five seconds; later cleanup is still tracked.
- Add explicit physical-source descriptors, alias deduplication, optional acquisition timestamps and operating-state qualification. Expose requested, reported and electrically corroborated operation separately, together with source quality, policy holds, ownership, recovery and lease status. Add configurable notification routes with incident deduplication and explicit critical-delivery uncertainty when the configured route cannot prove that capability.
- Document migration, exact-artifact acceptance, supervised shutdown observations and staged thermal experiments. New learned coast predictions remain advisory pending validation. Upgrades do not enable control, clear retained faults or verify private source/notification bindings; generated standalone packages must be regenerated for the new helper/signature contract.

## 0.8.14

- Allow explicit recovery of a matching saved heater-cutoff claim when the dedicated relay is already On after restart. The existing acknowledgement button is available for dedicated relays and can release this claim in Preview with the comfort controller stopped, trusted post-restart relay-On and native-Off reports, low dedicated load, safe electrical readings, no pending jobs and a durable acknowledgement. Recheck evidence after storage waits; stale, changed or failed evidence retains inhibition. Acknowledgement sends no equipment commands and preserves the ordinary latch, cooldown, telemetry protections and circuit/battery output exception. No configuration or storage-schema migration is required.

## 0.8.13

- Bypass the aggregate-output 2000 W trip and below-1600 W recovery requirement while fresh, valid, trusted circuit power is positive and battery charge is above 40%. Use the same qualification for activation, acknowledgement, commissioning and restoration. Positive circuit consumption is not proof of charging; zero or unusable circuit reports withdraw only the exception and do not independently prove an outage.
- Recheck restoration evidence at actual queued relay-On dispatch as well as after persistence. Reject restored/assumed battery reports and expose the current output-limit exception in diagnostics. Preserve grid-input qualification, battery reserve, required telemetry, native Off and dedicated-meter stop proof, aggregate stop corroboration, ownership, cooldown and persistence safety. No configuration migration, hardware-protection change or live enablement; existing latches remain intact.

## 0.8.12

- Start auxiliary heat with a confirmed target followed by an explicit Heat-mode request when needed, rather than assuming a combined temperature call powers on every integration. Keep one existing activation deadline across both stages and native dispatch, recheck interlocks before each positive command, and retain Off reconciliation on failure or interruption. Already-Heat updates do not cycle power. Adapters without bounded staged dispatch defer these starts explicitly. No JSON or protection-policy change.

## 0.8.11

- Separate confirmed-open cooling eligibility from measured window-effect demand weighting. A confirmed-open room can yield its ordinary shared-head cooling comfort floor and restart margin without first spending five minutes at least 0.5°F outside the band. Retain the active configured emergency floor and existing raw/forecast guard margins, including stronger cold-weather protection. Do not add immediate heating-ceiling relief or change the five-minute priority discount, mode holds, compressor timing or ownership rules.
- Carry bounded confirmed-open context through planning and minimum-run retention. Unknown, unavailable, restored or assumed contacts withdraw the exception; ordinary report-on-change timestamps and the existing contact debounce remain supported. No JSON migration or fan/hardware change is required.
- Fence relief-dependent positive starts at native HA dispatch against the original scoped contact/probe/profile/outdoor evidence and its expiry. Defer changed or expired authorization; retain Off and ordinary command paths. CLI/HTTP adapters lacking guarded dispatch defer exception-dependent starts explicitly.
- Publish the bounded relay-commissioning result on the existing Heater power guard sensor, including terminal status, reason and validated start/finish timestamps after restart. Keep interrupted tests latched for manual recovery; historical success never resumes a test or authorizes equipment commands.
- Distinguish an active shutdown check from completed recovery. Inactive diagnostics show `shutdown_active: false`, an idle stage, inactive confirmation and no aggregate session, so an old optional aggregate timeout is not presented as a new recovery failure. Preserve current safety evidence, control decisions, thresholds and unattended recovery behavior.

## 0.8.10

- Stage only explicitly supervised watt-meter relay commissioning: retain the ten-second relay acknowledgement, require trusted dedicated output below 100 W reported after actual On feedback and within 30 seconds of On dispatch, then maintain low-watt evidence with a maximum 30-second reporting gap while native control becomes ready within at most 300 seconds of On dispatch. Require native Off acknowledgement within ten seconds and new low-watt evidence within 30 seconds of actual Off dispatch, including acknowledgement time. Clip every stage by the existing 600-second authorization limit including cooldown.
- Fail and immediately request dedicated Off on high, invalid, missing or expired evidence, lost continuity, cancellation or interrupted ownership; retain a persisted single attempt and manual recovery hold. Track actual late commands and cleanup past the authorization deadline without treating requested Off as physical cutoff proof. Preserve ordinary 180-second freshness, normal unattended ten-second recovery and three-attempt limit, the amp path and independent protections. Never restore shared AC; retain comfort inhibition until proof and actual cleanup complete. Hardware timing and physical recovery remain unverified by software tests.

## 0.8.9

- Add the explicitly confirmed `house_climate.commission_heater_relay` action for a supervised dedicated-outlet recovery test while comfort control is Off. Require live permission, fresh trustworthy native Off and low dedicated load, healthy electricals/battery, idle ownership/jobs and an already disabled shared AC fallback. No JSON changes are required.
- Persist maintenance intent before the real dedicated cutoff and use the normal safe-load interval, cooldown and bounded restoration. Keep the ten-second energized verification maximum; stop after one failed restoration, restart/unload, manual interruption, lost observation continuity or a ten-minute overall limit. Retain manual recovery and block comfort enable through actual outstanding cleanup; never start heat or switch shared AC during this test.
- Expose bounded commissioning progress and result diagnostics. Automated tests cover the action and failure paths; physical reconnection timing and end-to-end recovery still need supervised commissioning.

## 0.8.8

- Make whole-circuit power advisory only. Its high, invalid, unavailable or stale readings no longer shed the heater, defer activation, block acknowledgement or prevent recovery. Retain the source binding and measurements, explicitly report that it is not enforced, and publish advisory-health changes without high-frequency numeric status updates.
- Keep the Bluetti grid-input operating budget at 12 times the lower fresh wall-phase voltage, with three distinct reports and 30 seconds of exceedance. Only input contributes qualification or its 85% rearm test; circuit telemetry cannot replace missing input or voltage. Battery reserve, output, temperature, heater-meter and cutoff protections are unchanged. No JSON migration is required.
- Separate verified guard-owned relay isolation from energized heater-meter supervision. A dedicated meter that stops reporting while its relay is Off remains unknown, while fresh external electrical/battery readings can qualify bounded recovery after the existing safe-load interval and cooldown.
- Require continuous trusted cutoff ownership, new relay-Off evidence after restart or observation loss, and revalidation after persisting restoration intent. Manual relay changes, unavailable/restored states and fresh contradictory load cannot authorize automatic On.
- Keep comfort inhibited through one ten-second restoration verification window, requiring native Off and fresh post-request low-meter evidence before recovery. Failed/cancelled restoration recuts; late On completions are compensated, manual interruption cannot resurrect old ownership, and unload no longer recursively requeues its own confirmed cutoff.
- Preserve meter freshness while energized, output/battery/temperature protections and manual-only shared AC recovery. Physical heater reconnection within the restoration window still requires supervised commissioning.

## 0.8.7

- Add an optional dedicated `heater_power` binding in W/kW as an explicit alternative to the existing A/mA heater-current meter. Reject configurations selecting both meters instead of silently bypassing either. Existing amp, switch-only and direct-path configurations remain valid.
- Use fresh, trusted native mode-Off feedback and selected-meter evidence consistently for stop corroboration, passive Off, manual recovery and cooldown. Watts use a strict 100 W operational low-load boundary; legacy current keeps its strict 1 A boundary. Neither is a device rating or an RMS-overcurrent limit.
- Retain the 180-second meter freshness requirement, five-second native-Off acknowledgement, existing restoration deadlines and independent electrical/temperature protections. Watt-based outlet restoration additionally requires a fresh low reading after the actual Off request; old low readings, stale measurements and uncertain jobs cannot restore power. Preserve manual shared-AC recovery and quarantine real meter changes during retained claims without manufacturing mismatches for unchanged legacy bindings.

## 0.8.6

- Replace the fixed 1350 W circuit/input proxy with a real-power budget of 12 times the lower fresh wall-phase voltage. Qualify circuit and grid input independently for at least 30 seconds and three distinct fresh reports before shedding; pending qualification defers new starts while retaining existing calls. Missing/invalid evidence cannot qualify, and cached or other-source reports do not advance the count. This is not measured RMS current or a physical 12 A guarantee.
- Keep high charging/circuit load after confirmed heater Off as a diagnostic and heating inhibition, not a new heater-stop failure or shared-AC cutoff reason. Preserve immediate output/battery protections, temperature guards, 85% rearm margin, safe-load/cooldown requirements and persisted recovery latches. No new JSON fields or mapping changes are required.
- Accept fresh, trusted passive Off feedback for an already-stopped heater during a wall-only event when no tracked command, cleanup or meter evidence conflicts. Keep that volatile state-only evidence separate from actual command acknowledgements and physical isolation. Register shutdown/service jobs before execution so reentrant HA state callbacks cannot launch duplicate stops.

## 0.8.5

- Preserve verified manual-Off evidence separately from actual command timestamps after a restarted heater wiring/emergency acknowledgement. Allow the existing safe-load and trip cooldown to finish without a new device command, restoring only eligibility for normal demand; never restore shared AC or old outlet ownership.
- Recheck current recovery evidence and pending work after acknowledgement persistence. If readings expire, recovery changes or cleanup starts during the write, retain durable inhibition and require a new acknowledgement.

## 0.8.4

- Add opt-in relative aggregate-output corroboration using a qualified recent pre-stop window and sustained post-Off reports, allowing other shared loads to remain above the absolute low-output ceiling. Preserve the existing default, native-Off acknowledgement and escalation timing, unknown-evidence behavior, and manual shared-AC recovery.
- Revoke the top-level aggregate-corroboration label when its current evidence expires or native Off is no longer fresh, without changing physical-cut confirmation or actuator behavior.

## 0.8.3

- Add a separately bound, explicitly enabled shared AC last resort after native heater Off and an optional dedicated outlet cutoff. Direct connections and absent heater-only meters are explicit; disconnected-device feedback and aggregate watts never become heater-current evidence.
- Persist shared emergency inhibition with manual recovery only, retain uncertain service jobs, and never automatically restore shared AC. Quarantine legacy or changed-wiring cutoff ownership until fresh, safe manual acknowledgement.
- Add optional calibrated aggregate AC-output corroboration using a fresh pre-stop baseline, native Off and distinct stable post-Off reports. Preserve state-only confirmation when aggregate evidence is inconclusive; unrelated load changes do not prove continued heating or isolated electrical shutdown.
- Allow an explicitly selected existing HA notify group for emergency-latch alerts alongside local persistent notifications. The optional service binding does not arm cutoff fallback or select recipients automatically.

## 0.8.2

- Restore the native heater power guard's periodic checks by separating HA's UTC-datetime interval callback from its state-event callback. The previous shared callback could raise every five seconds before checking telemetry expiry or recovery deadlines.
- Preserve state-driven manual outlet ownership, cutoff acknowledgements, persisted latches, electrical thresholds, and cooldown/restoration rules. Real HA interval regressions verify time-only telemetry expiry without callback errors, listener cancellation, and manual On-to-Off ownership handling.

## 0.8.1

- Add opt-in primary outdoor sources with fallback only when no primary is valid. Preserve coldest-valid fusion for existing unmarked bindings and within the selected tier; source aliases retain one identity and role. Expose the selected tier in protection diagnostics.
- Give weather entities a fixed 90-minute report-age limit while retaining 30 minutes for sensor inputs. Optionally bind an actual observation-time sensor and expire at the earlier report or observation deadline; fresh HA writes cannot renew old physical observations.
- Restart outdoor qualification after a genuine validity gap even when a polling stall hides the gap. Preserve unchanged valid reports and overlapping accepted coverage, and retain the existing five-minute qualification, outdoor deadband, conservative fallback, temperature limits, and equipment/electrical/ownership safeguards.

## 0.8.0

- Add optional private window/contact mappings with 120-second open and close confirmation. A confirmed open window plus a room deficit of at least 0.5°F beyond its comfort band for five minutes reduces only the affected direction's comfort priority to 25% and yields its opposing shared-head soft comfort boundary. Closed, unknown, or invalid evidence restores ordinary comfort behavior; hard protection remains active.
- Separate configurable protection temperatures from dashboard comfort ranges. Native defaults are heating protection below 60°F normally, a 65°F floor in a qualified cold outdoor regime or with unknown outdoor conditions, and emergency cooling at 80°F. The HA form validates and saves these settings atomically; generic configurations without the new protection object retain their previous limits.
- Accept optional explicitly mapped outdoor sensor or current weather temperatures with unit, age, and independent-source checks. Default outdoor qualification uses a 32°F reference, a ±2°F transition deadband, and five minutes of sustained valid evidence. Qualified regimes persist through the deadband; unknown conditions are identified separately and use the conservative higher floor.
- Rank simultaneous measured heating and cooling emergencies by the larger temperature violation. Exact ties favor cooling in a qualified warm outdoor regime and heating in a cold, initially unqualified, or unknown regime by default. Changed winners between two active emergencies retain the existing 120-second direction confirmation while bypassing ordinary comfort dwell.
- Release actual emergency priority one degree inside its protection bound by default, independently of comfort-target completion. Preserve inherited probe corroboration, immediate raw opposing stops, forecast reserves, shared-head feasibility, and mechanical acknowledgement/minimum-Off timing.
- Fence queued integration repairs when window confirmation, sustained comfort effects, outdoor qualification/expiry, or existing probe-confirmation deadlines change the decision context, even without a new entity report.
- Preserve valid dashboard holds and existing private bindings when optional contacts or outdoor sources are added. Expose the effective protection, outdoor regime, and window-effect evidence in controller diagnostics. Electrical, auxiliary-heater, ownership, and manual-control safeguards are unchanged; no auxiliary-heater mode fix is included.

## 0.7.3

- Stop transfer fans when planned, reported, or residual source conditioning would move their destination away from its target. Preserve useful idle transfer, compatible conditioning assistance, and independent fan schedules.
- Use separate post-stop temperature evidence for new forecast requests instead of projecting completed conditioning as continued drift. Preserve raw temperature-stop forecasts, genuine new trends, and actual house-limit protection.
- Recheck unstarted forecast-only requests during minimum-Off waits and cancel them when their evidence clears. Track started protection origins per room so a new forecast does not inherit another room's recovery hold.
- Learn stable, device-identity and operating-context capability baselines locally. Treat missing modes as recovery evidence only when those modes were previously established; do not assume every head supports Heat.
- Confirm persistent capability loss with at least three explicit failed refresh/rechecks, spaced 60 seconds apart, over at least 120 seconds before repair. Reset consecutive failures when capabilities return; retain bounded reload attempts and 15/30/60-minute backoff until stable recovery.
- Coordinate owning-integration repair with current-input revalidation, queued-command fencing, actual service-job draining, healthy-head Off confirmation, and fresh post-reload Off feedback. A reload is not an Off acknowledgement, and incomplete mode restoration does not by itself keep otherwise valid calls blocked.
- Retain ownership across delayed or cancelled service callers until their actual actuator jobs finish. Require superseding Off evidence after late positive completions, and reject stale stops from a previous ownership or repair generation.
- Add a bounded per-head transition history that distinguishes command requests, service returns, acknowledgements, and reported states. An unexpected Off report alone does not prove a controller-issued stop or physical compressor cycle.

## 0.7.2

- Prioritize house-temperature protection over another attached room's soft comfort preference, while preserving immediate raw/forecast opposing house cutoffs, missing-probe inhibition, and hardware acknowledgement/off timers.
- Carry protective calls until the originating room returns to its target or safe interior, avoiding repeated starts and stops at the protection threshold.
- Require 0.5°F of room headroom before comfort-only restarts and retain per-head comfort cutoffs across decision intervals. This is a restart deadband, not a guarantee of five-minute operation.
- Explain the blocking room, measured temperature, comfort boundary, and protection override in plan/recovery states and structured diagnostics.

- Rename the integration, add-on, device labels, logs, generated dashboards, release bundles, and documentation to Home Climate.
- Refresh the old default integration title while preserving custom names, registered entities, settings, storage, and add-on identity.
- Prefer private `home_climate_bindings.json` wiring and continue reading `house_climate_bindings.json` when the new file is absent. Both filenames stay excluded from public archives.
- Add the `home-climate` command while retaining the previous command as a compatibility alias.

## 0.7.0

- Replace the native room/schedule editor with a fixed operating policy and private device-role bindings. Keep one live-command opt-in and the existing dashboard temperature/range cards.
- Keep actual entity IDs in a private Home Assistant JSON file; publish only synthetic bindings. Migrate existing room identities, area assignments, and valid temporary holds using that private map. Holds cannot extend past the next fixed schedule boundary.
- Use ambient probes for room control; exclude heat-pump and living-room heater thermometers. Preserve valid cold-corner ambient readings alongside other independent probes without invented offsets.
- Stop heating/cooling with a 1°F house-limit reserve and a short worsening-trend forecast, including fresh maintenance and minimum-run retention.
- Add a native living-room heater power guard: conservative 1350 W circuit/input budget, separate 2000 W battery-output spike threshold, and 40% battery reserve. Apply the 12 A ceiling to circuit/input only; use downstream heater current for recovery confirmation rather than an output limit.
- Request Dyson Off, then cut only its dedicated outlet if acknowledgement fails. Restore guard-owned power only after battery exceeds 40%, safe load/cooldown checks, and fresh physical power/thermostat feedback; never force heat.
- Persist power-cut ownership and protect unload/restart from delayed outlet restoration. Keep the Elite and network outlets outside the actuator set.
- Explain that Emporia real watts are a conservative input budget, not a full-circuit RMS-current measurement. The independent native guard is not installed by the legacy standalone/add-on path.

## 0.6.9

- Deliver native worker heartbeats through a coalesced Home Assistant loop callback instead of waiting on a synthetic helper-service roundtrip. Native status-helper writes no longer queue no-op services.
- Defer new positive commands when heartbeat confirmation is overdue while retaining ownership, active-call safety checks, pending acknowledgements, and their original deadlines. Only a timely HA-loop confirmation can renew the lease; expired or previous-session callbacks cannot revive control.
- Abandon positive commands that time out before service dispatch without stopping an existing head call. Once dispatch begins, uncertain commands retain strict timeout and Off recovery.
- Show delayed heartbeat confirmation ahead of ordinary running/ready plans and warnings, with stable heartbeat state, block reason, and confirmation status in entity attributes.
- Configure native controller and auxiliary heartbeat leases separately, defaulting to five and two minutes. Auxiliary expiry stops heating locally and requires confirmed Off plus a later heartbeat before rearming; healthy heads retain the main lease.
- Preserve explicit disable, shutdown, auxiliary probe cutoffs, command acknowledgements, and standalone/legacy helper behavior.

## 0.6.8

- Add an explicit Back (keep draft) action to every configuration and options form. Preserve staged edits and unfinished form values while navigating, including incomplete room, schedule, and fan forms.
- Keep validation on Continue and apply configuration only at the final review/save step. Back clears destructive confirmation choices, and closing the dialog still cancels the unsaved draft.
- Explain comfort priority beside the room and schedule fields: higher numbers mean higher priority, with 1 as a typical value.
- Drain the previous controller before starting Home Assistant’s configuration reload. Keep saved changes pending and retry shutdown through device disconnects instead of entering HA’s permanent failed-unload state after an ordinary delayed Off report.
- Allow an additional 120-second Off-acknowledgement wait for direct unloads, preserving the worker-exit limit, critical ownership, in-flight activation barriers, and persisted fan cleanup.

## 0.6.7

- Log controller-wide Off requests with their cause and affected devices before dispatch, including global worker faults, startup reconciliation, disable, and shutdown.
- Report heartbeat-watchdog expiry with heartbeat age, the 120-second limit, and the full owned-device stop scope; distinguish worker pauses, saved ownership recovery, and shutdown-storage failures.
- Avoid repeating identical stop and watchdog messages during retries. Routine lifecycle stops use INFO, watchdog pauses use WARNING, and controller faults use ERROR; local device failures remain local.
- Explain deferred unloads with unconfirmed climate devices, in-flight activations, retained ownership, and worker/reload blockers without classifying an ordinary off-confirmation wait as a worker fault.
- Keep command acknowledgement, ownership, safety checks, recovery, and equipment timing unchanged.

## 0.6.6

- Add optional minimum cooling setpoints per physical head, editable in Home Assistant with an explicit apply-to-all option.
- Honor configured minima in planned and dispatched calls, combining them with the device's advertised range and original temperature grid. Room targets, heating, auxiliary control, and exact command acknowledgements remain unchanged.
- Exclude infeasible cooling calls from shared-mode arbitration and stop/report only the affected head, without requesting an integration reload.

## 0.6.5

- Keep transfer fans running at their configured speed when their shared head is selected to condition the destination room, even before a useful room-temperature gradient develops. Fast safety checks remove head-dependent assistance when the head is inhibited.
- Give heat-pump mode/setpoint acknowledgements a separate 120-second wait, configurable from System settings. Preserve a single bounded deadline across unconfirmed same-direction target changes; exact acknowledgement, safety stops, and Off/auxiliary/fan/transport timing remain unchanged.
- Show pending head commands, requested and reported values, and confirmation countdowns in room plans and Recovery status.

## 0.6.4

- Defer automatic reloads of a shared device integration while another healthy head on that integration is heating or cooling. Keep local recovery retries running without consuming a reload attempt.
- Power circulation fans on explicitly when requesting a positive percentage, fixing Dyson devices that stored a speed while remaining off.
- Show per-head run/off holds and countdowns in Recovery status, Plan summary, and room plans instead of reporting Ready during a blocked activation.
- Retain each head's latest stop cause. Climate acknowledgement errors now identify the exact requested service, mode, and setpoint plus reported state, target, and report age; recovery details preserve the original uncertain command.
- Preserve strict climate acknowledgements, off confirmation, shared changeover barriers, and independent safety checks.

## 0.6.3

- Add a per-probe “reports only on change” setting so an unchanged but valid reading is not rejected for its age. Periodic probes retain normal age checks.
- Expose the setting in each room's Home Assistant editor, preserving source identifiers, calibration, and existing selections.
- Continue rejecting unknown, unavailable, invalid, implausible, missing-timestamp, and future-dated readings. Auxiliary watchdogs honor the same per-probe policy and retain their independent temperature cutoff.
- Preserve existing dashboard comfort holds and generated watchdog signatures when the new setting is unused. Regenerate standalone/legacy HA packages after opting in a probe.

## 0.6.2

- Separate desired room temperatures from the setpoints sent to heat-pump heads. External room probes continue deciding demand and shutdown.
- Add independent heating and cooling call offsets, defaulting to 5°F, to keep a head's onboard thermostat from ending a room call prematurely. Active calls account for the head's reported temperature and respect device limits and steps; zero restores direct room targets.
- Keep call setpoints stable between ordinary 90-second decisions and retain the last acknowledged setpoint during minimum-run holds.
- Stop biased calls promptly at the opposite comfort boundary, missing readings, or system limits. Safety stops remain latched until the next comfort decision.
- Use fused room temperatures for ordinary comfort demand and run holds, preventing a persistently biased probe from creating competing comfort calls. Confirmed extremes still drive temperature-limit protection.
- Show room goals and head call setpoints separately in room plans. Configure call offsets in System settings using Home Assistant's temperature units.

## 0.6.1

- Make routine comfort decisions every 90 seconds by default, including head setpoints and circulation fan speeds. Ordinary state updates and recovery wakeups no longer trigger extra comfort decisions.
- Keep safety, command acknowledgements, heartbeat renewal, and disconnect recovery responsive between decisions. Dashboard edits, schedule changes, and newly confirmed temperature-limit violations can still trigger an immediate decision.
- Add per-head minimum run and off intervals, defaulting to five and three minutes. Holds use acknowledged HA states and yield to room comfort boundaries, missing probes, unsafe conditions, profile changes, and shutdown.
- Expose decision timing and head cycle holds in controller diagnostics. Existing configurations receive these defaults automatically; timing can be adjusted in System settings.

## 0.6.0

- Add a native room thermostat for Home Assistant's standard Thermostat card, exposing the comfort low/high range and fused room temperature.
- Add an ordinary Desired temperature control per room. Moving it shifts the current comfort band without editing integration configuration.
- Apply dashboard edits immediately through the running policy, preserving controller enablement, ownership, shared-mode arbitration, fan schedules, and safety limits.
- Hold edits until the next room schedule transition; rooms without schedules hold until Schedule is selected. Temporary enables an explicit comfort hold during limits-only periods.
- Persist active holds across HA restarts until their original expiry. Resume the saved schedule from the thermostat's Schedule preset.
- Convert temperatures through HA's standard climate/number APIs and reject invalid or out-of-bounds service inputs.

## 0.5.3

- Accept each head's advertised capabilities: an Off/Cool head can stay idle or cool without faulting the whole controller. Unsupported demand no longer wins shared-system arbitration.
- Preserve fresh off evidence across retries so a recovered head can resume even when a later, already-off command produces no new report.
- Wake native control promptly when availability, modes, off confirmation, or shutdown readiness changes.
- Add per-head Reload integration buttons for explicitly refreshing the owning HA integration, including stale capabilities.
- Add a readable Recovery status with affected rooms, reported modes, and the remaining settling time.
- In the native integration, automatically reload the owning Home Assistant integration after a heat-pump recovery issue persists for two minutes. Reloads share a per-integration cooldown, back off after failures, and stop after three attempts during one continuous incident. Shadow and disabled control never initiate reloads.
- Preserve ownership, genuine off confirmation, and the full configured all-head changeover delay throughout recovery.

## 0.5.2

- Tolerate temporary climate-device disconnections, API failures, and missing acknowledgements without restarting healthy rooms or dropping controller ownership.
- Keep reachable heads in the established shared direction; wait and retry at startup or changeover until every shared head confirms off before settling and activation.
- Reconcile an affected device through a fresh off report before resuming commands, including when an earlier activation may have completed late.
- Recover the auxiliary heater independently while retaining external-probe cutoffs, off retries, and strict heat-to-off-to-fan sequencing.
- Show waiting climate devices and off requests in room/heater plans and Current issues. Clear resolved auxiliary connectivity warnings.
- Update all GitHub Actions to stable Node.js 24 releases with immutable commit pins.

## 0.5.1

- Recover shutdown ownership once climate devices confirm off; persist unfinished circulation-fan stops separately so an offline fan cannot prevent climate control from resuming.
- Keep heat-pump and auxiliary climate control running when a separate circulation fan times out, becomes unavailable, or rejects a service call.
- Show persistent fan warnings in Current issues, retry each fan with capped backoff, and clear its warning after HA acknowledges the requested state.
- Prioritize climate commands over circulation fans and let a new fan-off request supersede an unsuccessful speed command immediately.
- Use integer fan percentages compatible with HA speed converters and accept one percentage point of reporting rounding for stepped fans.
- Preserve strict climate acknowledgement checks, shared-head all-off barriers, auxiliary heat/fan-only transitions, and owned-device shutdown recovery.

## 0.5.0

- Add a Controller enabled switch directly to the native HA device. Room-based installations no longer need a separate YAML enable/watchdog package.
- Show the reason live control is paused in the plan summary and current issues.
- Run a native HA-loop watchdog independently of the worker, checking external heater probes and a 120-second heartbeat.
- Persist ownership before issuing actuator commands, recover previously owned devices after restart, and retain shutdown retries until off is acknowledged.
- Reject queued activations after disabling; keep ownership while actuator calls are still in flight. Enabling control always starts through the all-off barrier.
- Preserve the generated package for standalone/add-on use and legacy version-1 file installations. Native enable controls reset off on restart or configuration reload.

## 0.4.3

- Show a readable plan summary, per-room head actions and active comfort ranges, planned fan percentages, and auxiliary-heater plans directly on the Home Assistant device page.
- Mark shadow decisions as previews and distinguish live plans from equipment feedback. Hide action plans when the controller faults, stops, or has not produced a decision.
- Expose current issues and the latest controller fault as readable entities. Preserve existing status and room-temperature identities.
- Display temperatures in HA's unit system and report the exact scheduled profile used by the decision engine. Room and fan configuration changes automatically update the available entities.

## 0.4.2

- Publish numbered GitHub Releases after CI passes, with changelog notes and installation bundles, so HACS can offer versioned updates.
- Read only configured entities through HA's direct cached-state API, avoiding per-poll event-loop waits and whole-house state copies.
- Preserve probe report timestamps and include all controller interlocks and legacy helpers in selective reads.
- Reject queued native operations after their deadline so a delayed command cannot activate equipment after timing out.
- Keep the latest fault reason, time, and count visible in controller status after recovery; include the operation in timeout messages.

## 0.4.1

- Fix missing room-name translation errors when opening room menus during setup or configuration. Room names now appear in the menu description, where Home Assistant supports placeholders.

## 0.4.0

- Replace fixed household roles with arbitrary rooms, editable names, shared-head assignments, profiles, schedules, and fan rules.
- Add native HA configuration and options editors with entity/area selectors and local temperature units.
- Add fused per-room temperature sensors with stable IDs across renames.
- Make auxiliary heating optional and assignable to any room.
- Preserve version-1 file/helper operation and provide import into the HA editor.
- Replace personal mappings with synthetic examples; add migration and room-configuration coverage.
- Include public distribution source, tests, CI, documentation, and MIT license.

## 0.3.0

- Add native Home Assistant lifecycle, status diagnostics, and HACS packaging.
- Share the controller engine between native integration, CLI, Docker, and add-on.
