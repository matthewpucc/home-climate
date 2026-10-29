# Home Climate

A Home Assistant integration with a fixed operating policy for four heat-pump heads, room fans, and a battery-backed auxiliary heater. Operating rules live in source; actual device bindings stay in a private Home Assistant file. Setup has an atomic form for live-command permission and independent protection settings. Room climate cards still provide temporary comfort-temperature and range adjustments.

## Install

Use the [installation guide](docs/hacs.md) to add this repository to HACS as an **Integration**, or install the release ZIP.

1. Place your private device map at `/config/home_climate_bindings.json` before installing or upgrading. [bindings.example.json](bindings.example.json) shows the schema with synthetic IDs.
2. Install the integration and restart Home Assistant.
3. Add **Home Climate** under Settings → Devices & services, or let the existing entry migrate.
4. Leave live commands disabled to inspect plans, or opt in when ready; then turn on **Controller enabled**.

Existing installs update in place. The old private filename `house_climate_bindings.json` still works; no file rename or entity reconfiguration is required.

The controller starts disabled after an HA restart. When live commands are permitted, the living-room heater power guard remains active even while comfort control is disabled.

## Fixed house policy

| Area | Normal target / range | Shared head |
| --- | --- | --- |
| Office | 72°F / 71–73°F | Bonus |
| Entertainment | 72°F / 71–73°F | Bonus |
| Guest bedroom | 72°F / 71–73°F | Guest |
| Primary, 10 p.m.–5 a.m. | 68°F / 67–69°F, highest comfort priority | Primary |
| Primary, 5–9 a.m. | 69°F / 67–70°F | Primary |
| Primary, daytime | Comfort off; house limits remain active | Primary |
| Living room | 68°F / 67–69°F, lowest priority | Living plus auxiliary heater |

Schedules use America/Los_Angeles. Comfort cards retain their 66–75°F editing bounds. Native protection is configured separately: the 0.8.0 defaults are a 60°F heating floor, a 65°F floor for qualified cold or untrusted outdoor conditions, and cooling emergency at 80°F. Qualified outdoor state uses a configurable reference/deadband and sustained-valid interval. Raw opposing guards still stop one degree inside the active protection bound, with a short worsening-only forecast. Generic configurations without explicit protection retain legacy 66–75°F behavior. See [protection settings and conflict rules](docs/protection-temperatures.md).

Room control uses external ambient probes. Heat-pump readings remain available for equipment setpoint compensation, but cannot make a warm room appear cold. Living uses its assigned ambient probes rather than the heater's thermometer. A real cold-corner measurement remains valid without an invented calibration offset.

Version 0.8.1 adds [explicit local-primary/weather-fallback selection](docs/outdoor-sources.md), source-specific age limits, observation-time checks, and qualification recovery after reporting gaps. Existing unmarked source lists retain coldest-valid selection; mark a primary explicitly in the private bindings file to opt in.

The system makes comfort decisions every 90 seconds. Fifteen-second maintenance checks handle temperature cutoffs and acknowledgements. Five-minute acknowledged head runs and three-minute off periods reduce unnecessary cycling; protection and equipment recovery can override a run.

A shared head checks every attached area before starting. Comfort-only restarts need 0.5°F of headroom from the opposing comfort edges. Confirmed/forecast house protection can override those soft preferences until the affected area recovers, while the opposing house cutoffs and hardware timing stay enforced. Plan and recovery entities identify the room and threshold responsible for a wait.

Optional private window contacts can lower only an affected direction's comfort priority to 25% after a sustained measured deficit, and allow its opposing soft shared-head boundary to yield. They never discard room measurements or hard protection. With no competing eligible demand, that room can still receive conditioning. See [window evidence and recovery](docs/window-contacts.md), [operating rules](docs/control.md), and [wiring and dashboard controls](docs/configuration.md).

## Living-room heater power guard

The Elite battery supplies the heater and network equipment. By default only the heater's dedicated outlet is a cutoff actuator. The [opt-in emergency cutoff](docs/heater-emergency-cutoff.md) adds a separately bound shared AC last resort, optional individual outlet/meter stages, calibrated aggregate-output corroboration, and an optional existing HA notify group for emergency alerts. Shared AC recovery is always manual; these additions require 0.8.3 or later.

- Compare Elite grid input against a real-power budget of **12 × the lower fresh wall-phase voltage**, in watts. Shed only after input remains at or above its budget for at least **30 seconds and three distinct fresh reports**. Pending input qualification defers new starts without stopping an existing call solely for that pending condition. Whole-circuit power is advisory; it never triggers shedding or blocks activation/recovery.
- Shed immediately for a **2000 W** AC-output spike or battery at/below **40%**. Temperature protections remain independent. Battery-assisted output may legitimately exceed grid input.
- Request Dyson Off first. If Off is not confirmed within five seconds, use the configured optional cutoff stages; shared AC requires explicit opt-in and manual recovery.
- Rearm only above 40% battery, with input below 85% of the current budget, 60 seconds of safe load and the five-minute cooldown. Restore only an eligible dedicated outlet the guard cut; never restore shared AC automatically.

The input budget is unchanged by the advisory circuit policy. Fresh measured voltage is required; missing or invalid voltage cannot fall back to a nominal value or circuit telemetry. Re-reading cached reports does not advance qualification. This reported-watt policy does **not** enforce the whole-circuit load or establish a physical 12 A guarantee. The downstream heater amp reading remains recovery evidence, not a wall-current measurement or output ceiling.

Grid charging can remain high after confirmed heater Off. That remains a wall-load diagnostic and heating inhibition, not proof that the heater failed to stop or a new reason to cut shared AC output. A heater already reporting fresh, trusted Off can use state-only evidence without another no-op stop when no tracked job or meter evidence conflicts. No JSON threshold or mapping changes are required, and upgrading does not clear existing recovery latches. See the [wall-budget evidence rules](docs/control.md#heater-load-shedding).

From 0.8.7, the dedicated-watts feature adds `power.heater_power` as an alternative to `power.heater_current`. Select one meter: W/kW power or A/mA current, with the unused binding `null`. Both retain the 180-second freshness check. Watts below 100 W can corroborate a fresh native Off report; thermostat idle alone does not qualify. Stale low readings cannot authorize restoration, and a new watt-based restoration requires post-command low-load feedback. This is operational load evidence, not RMS-overcurrent enforcement or physical isolation proof. See [dedicated meter configuration](docs/heater-emergency-cutoff.md#dedicated-load-meter). Do not apply the new field to 0.8.6.

An [unreleased staged commissioning path](docs/heater-emergency-cutoff.md#staged-watt-meter-commissioning-unreleased) separates relay-On, continuous low dedicated watts, native reconnection and post-Off verification during an explicitly supervised watt-meter test. Its stage deadlines share the existing ten-minute authorization limit, including cooldown; pending commands and cleanup remain tracked afterward. Normal unattended recovery and the amp path retain their ten-second verification window; this change does not run a test, permit comfort control or switch shared AC automatically. The proposal still needs supervised hardware validation.

## Device recovery and diagnostics

The native integration learns stable, device-specific supported modes and can repair a persistent loss of a previously available mode. It first waits through explicit refresh/rechecks: at least three failures, 60 seconds apart, over at least two minutes. A successful recheck resets consecutive failures. Reloads have a separate attempt limit and backoff; unused-mode loss can wait while healthy heads are conditioning rooms.

A first-seen mode list cannot establish what is missing. If a head is already degraded when capability learning begins, use its **Reload integration** button once to obtain healthy reports. The controller does not invent Heat support for a Cool-only device. Recovery status explains retries, deferral, coordinated repair, and any unresolved block; [operating rules](docs/control.md#integration-capability-recovery) describe the boundaries.

The controller's transition history distinguishes its commands from device reports. A reported mode and accepted setpoint do not prove compressor output, and an unexpected Off report does not prove that Home Climate issued Off. Reloading an integration can restore its feedback, but cannot repair a failing Wi-Fi adapter.

## Development

The central house rules are in [house_policy.py](custom_components/house_climate/controller/house_policy.py); electrical thresholds and fallback logic are in [power_guard.py](custom_components/house_climate/power_guard.py). Temperature stopping margins are in [temperature_guard.py](custom_components/house_climate/controller/temperature_guard.py).

Existing installations migrate in place, preserving registered room identities and valid temporary holds. Daily comfort adjustments belong on the room cards; the protection form is separate. House schedules remain fixed in source. Private bindings contain entity-role mappings; the optional emergency cutoff additionally permits explicit aggregate-output calibration.

Run core tests with `python -m unittest discover -s tests -v`. Native integration tests require Python 3.14 and Home Assistant 2026.10.4: `python -m unittest discover -s tests_homeassistant -v`. GitHub Actions runs both suites and validates the install archives.

Core replays use synthetic probes and devices. Native tests exercise Home Assistant's real state and service machinery with simulated equipment; they do not operate physical heat pumps. These checks establish software behavior, not field cooling capacity, airflow, Wi-Fi reliability, or physical compressor cycling. Installation and a live comparison are separate validation steps.

The standalone CLI and container retain their legacy file configuration for compatibility. They do not install the native house-specific electrical guard.

MIT licensed.
