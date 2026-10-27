# Home Climate

A Home Assistant integration with a fixed operating policy for four heat-pump heads, room fans, and a battery-backed auxiliary heater. Operating rules live in source; actual device bindings stay in a private Home Assistant file. Setup has one live-command opt-in. Room climate cards still provide temporary temperature and range adjustments.

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

Schedules use America/Los_Angeles. All rooms retain the 66–75°F house bounds. Heating stops with a 1°F reserve below the upper bound; cooling stops with the same reserve above the lower bound. A short worsening-trend forecast can stop earlier.

Room control uses external ambient probes. Heat-pump readings remain available for equipment setpoint compensation, but cannot make a warm room appear cold. Living uses its assigned ambient probes rather than the heater's thermometer. A real cold-corner measurement remains valid without an invented calibration offset.

The system makes comfort decisions every 90 seconds. Fifteen-second maintenance checks handle temperature cutoffs and acknowledgements. Five-minute acknowledged head runs and three-minute off periods reduce unnecessary cycling; protection and equipment recovery can override a run.

A shared head checks every attached area before starting. Comfort-only restarts need 0.5°F of headroom from the opposing comfort edges. Confirmed/forecast house protection can override those soft preferences until the affected area recovers, while the opposing house cutoffs and hardware timing stay enforced. Plan and recovery entities identify the room and threshold responsible for a wait.

See [operating rules](docs/control.md) and [wiring and dashboard controls](docs/configuration.md).

## Living-room heater power guard

The Elite battery supplies the heater and network equipment. Only the heater's dedicated outlet is a cutoff actuator; the Elite output and network are never switched.

- Compare the complete Emporia circuit and Elite input separately against a **1350W** shedding budget, reduced at low wall voltage.
- Shed for a **2000W** battery output spike or battery at/below **40%**. The 12A ceiling applies to circuit/input only, so battery-assisted output may legitimately be higher.
- Request Dyson Off first. If Off is not confirmed within five seconds, cut the dedicated heater switch.
- Recover only above 40% battery, after safe load remains stable and the five-minute cooldown completes. Restore only an outlet the guard cut, confirm thermostat Off, then allow normal room demand.

Emporia exposes real power rather than full-circuit RMS current. The 1350W budget conservatively supports the requested 12A input ceiling; the downstream heater amp reading is used for recovery confirmation, not as a wall-current measurement or a 12A output ceiling. Software, sensor cadence, and device feedback cannot establish a physical current guarantee.

## Device recovery and diagnostics

The native integration learns stable, device-specific supported modes and can repair a persistent loss of a previously available mode. It first waits through explicit refresh/rechecks: at least three failures, 60 seconds apart, over at least two minutes. A successful recheck resets consecutive failures. Reloads have a separate attempt limit and backoff; unused-mode loss can wait while healthy heads are conditioning rooms.

A first-seen mode list cannot establish what is missing. If a head is already degraded when capability learning begins, use its **Reload integration** button once to obtain healthy reports. The controller does not invent Heat support for a Cool-only device. Recovery status explains retries, deferral, coordinated repair, and any unresolved block; [operating rules](docs/control.md#integration-capability-recovery) describe the boundaries.

The controller's transition history distinguishes its commands from device reports. A reported mode and accepted setpoint do not prove compressor output, and an unexpected Off report does not prove that Home Climate issued Off. Reloading an integration can restore its feedback, but cannot repair a failing Wi-Fi adapter.

## Development

The central house rules are in [house_policy.py](custom_components/house_climate/controller/house_policy.py); electrical thresholds and fallback logic are in [power_guard.py](custom_components/house_climate/power_guard.py). Temperature stopping margins are in [temperature_guard.py](custom_components/house_climate/controller/temperature_guard.py).

Existing installations migrate in place, preserving registered room identities and valid temporary holds. Daily adjustments belong on the room cards; schedules and engineering timers are fixed in source. The private bindings file contains only entity-role mappings, never rules or thresholds.

Run core tests with `python -m unittest discover -s tests -v`. Native integration tests require Python 3.14 and Home Assistant 2026.10.1: `python -m unittest discover -s tests_homeassistant -v`. GitHub Actions runs both suites and validates the install archives.

Core replays use synthetic probes and devices. Native tests exercise Home Assistant's real state and service machinery with simulated equipment; they do not operate physical heat pumps. These checks establish software behavior, not field cooling capacity, airflow, Wi-Fi reliability, or physical compressor cycling. Installation and a live comparison are separate validation steps.

The standalone CLI and container retain their legacy file configuration for compatibility. They do not install the native house-specific electrical guard.

MIT licensed.
