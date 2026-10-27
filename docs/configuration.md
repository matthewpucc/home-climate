# Wiring and everyday controls

Native Home Climate uses a fixed house policy. Setup and Configure expose only **Allow live commands**. The **Controller enabled** device switch starts or pauses comfort control; a restart leaves it disabled. There is no room/schedule editor to work through.

## Room cards

Use each room's climate card to change its desired temperature or comfort range. The change holds until the next scheduled profile change. Safety limits continue to apply. Existing climate and number entities are retained when upgrading.

The fixed primary schedule is 10 p.m.–5 a.m. at 68°F (67–69°F range), 5–9 a.m. at 69°F (67–70°F), and limits-only outside those hours. Its purifier runs at 60% during the sleep period and is off during the day. Other occupied rooms normally target 72°F; living targets 68°F and has the lowest shared-head priority.

## Source mapping

`custom_components/house_climate/controller/house_policy.py` contains schedules, priorities, and temperatures. Actual room/head/fan IDs are loaded from `/config/home_climate_bindings.json`; the repository contains only a synthetic example.

Assign the actual ambient probe IDs to each logical area in the private file. Prefer at least two independent sources where available; the shared-head areas still require their own measurements.

Ambient probes in cold exterior corners remain valid readings with no invented offset. A probe near the heater may warm sooner; its reading still participates in the cutoff. Multiple probes contribute to the room median; the valid coldest/hottest readings also protect room limits. Heat-pump thermometers, the auxiliary heater's internal readings, obsolete probes, and equipment temperature gauges are excluded from ambient bindings.

Both bonus-floor fans circulate conditioned air toward the office. The bonus head serves office and entertainment together; neither room substitutes for a completely unmeasured other area.

## Electrical wiring

The independent power guard receives private bindings for complete-circuit power, battery input/output, battery charge, both wall phases, heater-plug current, and the dedicated heater-only cutoff switch.

The heater is downstream of the battery. Circuit power and battery input are overlapping views and are never summed. The guard never switches the battery AC output or network equipment.

Electrical thresholds live in `custom_components/house_climate/power_guard.py`. Device IDs stay in the private JSON file. These are operational rules, not user-facing tuning options. See [power and recovery rules](control.md).

## Private bindings

Copy the prepared private file to `/config/home_climate_bindings.json` **before** upgrading. New installations can use `bindings.example.json` as a schema example; every example ID is synthetic.

The file has exactly five top-level sections: `heads`, `rooms`, `fans`, `heater`, and `power`. It accepts entity IDs only. Unknown fields, rule overrides, missing roles, and invalid domains are rejected. Missing private bindings fail closed rather than selecting guessed equipment.

Existing `house_climate_bindings.json` files are accepted when the new filename is absent. When both exist, `home_climate_bindings.json` is authoritative; invalid or unreadable new wiring does not fall back to different equipment.

Keep this file in HA's config directory, outside `custom_components`. Git ignores its standard filename, and installation/source packages explicitly exclude it. It survives HACS updates.

## Upgrade behavior

Configuration entries migrate in place. Existing room identities and area assignments are matched to the private wiring so dashboards retain their entities. Valid temporary holds are carried over with updated fingerprints and cannot extend beyond the new next schedule boundary. Legacy tuning is no longer authoritative.

Shadow mode performs no device writes. After opting into live commands, the electrical guard protects the heater independently of the comfort switch. It can therefore shed a manually operated heater while comfort control is paused.
