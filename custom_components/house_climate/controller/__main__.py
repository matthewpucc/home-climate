from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from .config import Config, Probe, Settings, Zone, helper
from .executor import ControlFault, Executor
from .ha import HomeAssistant
from .ha_package import watchdog_signature, write_dashboard, write_package
from .locking import live_lock
from .policy import Policy


def emit(event: str, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def preflight(config: Config, states: dict, executor: Executor):
    for entity in (config.entity("heartbeat", "input_datetime"), config.entity("heater_owned")):
        if states.get(entity, {}).get("state") in (None, "unknown", "unavailable"):
            raise ControlFault(f"Missing/unavailable helper: {entity}")
    if config.heater_entity:
        watchdog = config.entity("auxiliary_watchdog", "automation")
        probe_safe = config.entity("auxiliary_probe_safe", "binary_sensor")
        if states.get(watchdog, {}).get("state") != "on":
            raise ControlFault(f"Install/enable {watchdog} before live control")
        signature = states.get(probe_safe, {}).get("attributes", {}).get("configuration_signature")
        if signature != watchdog_signature(config):
            raise ControlFault("HA watchdog mapping differs from this config; regenerate and reload the HA package")
    for entity in config.climate_entities:
        state = states.get(entity, {})
        modes = set(state.get("attributes", {}).get("hvac_modes", []))
        required = {"off", "heat"} if entity == config.heater_entity else {"off", "heat", "cool"}
        if state.get("state") in (None, "unknown", "unavailable") or not required <= modes:
            raise ControlFault(f"Climate entity unavailable or missing required modes: {entity}")
    if config.heater_entity:
        executor.target(config.heater_entity, config.heater_high_setpoint_f, states, heater=True)


def release(ha, executor, event_callback=None):
    failures = executor.shutdown()
    # Leave ownership armed if the off command was not acknowledged. HA keeps retrying.
    try:
        if not executor.config.heater_entity or ha.states().get(executor.config.heater_entity, {}).get("state") == "off":
            ha.service("input_boolean", "turn_off", entity_id=executor.config.entity("heater_owned"))
    except Exception:
        failures.append("heater ownership release")
    (event_callback or emit)("stopped", failures=failures)


def run(args, config: Config):
    if args.live:
        with live_lock():
            return run_loop(args, config)
    return run_loop(args, config)


def run_loop(args, config: Config, *, ha_client=None, stop_event=None, wake_event=None, event_callback=None):
    # Native HA supplies its own API bridge and lifecycle event. CLI behavior stays unchanged.
    report = event_callback or emit
    if args.live and (args.snapshot or args.at or args.once):
        raise ValueError("--live requires continuous real-time operation (no snapshot/at/once)")
    if args.snapshot:
        with open(args.snapshot) as snapshot:
            states = json.load(snapshot)
        if isinstance(states, list):
            states = {s["entity_id"]: s for s in states}
        now = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("--at must include a UTC offset")
        plan = Policy(config).decide(states, now, Settings.read(states, config))
        report("shadow", timestamp=now.isoformat(), **plan.as_dict())
        return
    if args.at:
        raise ValueError("--at is only valid with --snapshot")
    config.validate(live=args.live)
    ha = ha_client if ha_client is not None else HomeAssistant(os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", ""), live=args.live)
    ha_config = ha.request("config")
    config = replace(config, timezone=ha_config["time_zone"])
    unit = ha_config["unit_system"]["temperature"]
    config.validate(live=args.live)
    policy, executor = Policy(config), Executor(config, ha, unit)
    quit_event = stop_event if stop_event is not None else threading.Event()
    if stop_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: quit_event.set())
    active = False
    faults = 0
    report("started", live=args.live, timezone=config.timezone, climate_unit=unit)
    try:
        while not quit_event.is_set():
            started = time.monotonic()
            try:
                states = ha.states()
                enabled = states.get(config.entity("enabled"), {}).get("state") == "on"
                settings = Settings.read(states, config, strict=args.live and enabled)
                now = datetime.now(timezone.utc)
                plan = policy.decide(states, now, settings, unit)
                if args.live and enabled:
                    preflight(config, states, executor)
                    if plan.heater_on and states.get(config.entity("auxiliary_probe_safe", "binary_sensor"), {}).get("state") != "on":
                        plan.heater_on = False
                        plan.warnings.append("HA independent probe check blocks auxiliary heat")
                    if not active:
                        # Claim before any actuator writes so a crash leaves HA responsible.
                        ha.service("input_boolean", "turn_on", entity_id=config.entity("heater_owned"))
                        active = True
                        # Reconcile startup/reenable through a full off barrier, including heater.
                        failures = executor.shutdown()
                        critical = [entity for entity in failures if entity not in config.fans.values()]
                        if critical:
                            raise ControlFault("Startup off commands failed: " + ", ".join(critical))
                        for entity in failures:
                            plan.warnings.append(f"Circulation fan {entity}: startup off command failed; retrying independently")
                        policy = Policy(config)
                        # The old snapshot predates shutdown. Read again next cycle.
                        report("arming", message="All-off commands sent; awaiting fresh state")
                    else:
                        if states[config.entity("heater_owned")]["state"] != "on":
                            raise ControlFault("Heater ownership was removed; refusing further activation")
                        ha.service("input_datetime", "set_datetime", entity_id=config.entity("heartbeat", "input_datetime"), timestamp=now.timestamp())
                        executor.step(plan, states, time.monotonic())
                        status = (f"Requested {plan.mode}; {executor.phase}. " + "; ".join(plan.reasons + plan.warnings))[:255]
                        if states.get(config.entity("status", "input_text"), {}).get("state") != status:
                            ha.service("input_text", "set_value", entity_id=config.entity("status", "input_text"), value=status)
                elif active:
                    release(ha, executor, event_callback=report)
                    active = False
                    policy = Policy(config)
                elif args.live and states.get(config.entity("heater_owned"), {}).get("state") == "on":
                    # Recover ownership left behind by a previous process, even when disabled.
                    release(ha, executor, event_callback=report)
                report("live" if args.live and enabled else "shadow", timestamp=now.isoformat(), phase=executor.phase, **plan.as_dict())
                faults = 0
            except Exception as exc:
                report("fault", error=str(exc))
                if active:
                    release(ha, executor, event_callback=report)
                    active = False
                policy = Policy(config)
                faults += 1
                if args.once:
                    raise
            if args.once:
                break
            # Interruptible wait; no heartbeat is renewed during a communication failure.
            delay = max(0, min(60, config.poll_seconds * max(1, min(faults, 4))) - (time.monotonic() - started))
            if wake_event is not None:
                wake_event.wait(delay)
                wake_event.clear()
            else:
                quit_event.wait(delay)
    finally:
        if active:
            release(ha, executor, event_callback=report)


def demo(config: Config):
    now = datetime.fromisoformat("2026-10-16T05:30:00+00:00")
    config = replace(config, zones={name: replace(zone, probes=tuple(
        Probe(f"sensor.demo_{name}_{i}", f"demo_{name}_{i}") for i in (1, 2))) for name, zone in config.zones.items()})
    settings = Settings.read({}, config)
    values = {key: settings.value(room.profile.target_f) + (2 if key != config.auxiliary_room else -1)
              for key, room in config.zones.items()}
    states = {config.heater_entity: {"state": "off"}} if config.heater_entity else {}
    for name, zone in config.zones.items():
        for probe in zone.probes:
            states[probe.entity_id] = {"state": str(values[name]), "attributes": {"unit_of_measurement": "°F"}, "last_updated": now.isoformat()}
    emit("synthetic_demo", **Policy(config).decide(states, now, Settings.read({}, config)).as_dict())


def main():
    parser = argparse.ArgumentParser(description="Shadow-first Home Assistant climate controller")
    parser.add_argument("--config", default="config.local.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--live", action="store_true")
    run_parser.add_argument("--once", action="store_true")
    run_parser.add_argument("--snapshot")
    run_parser.add_argument("--at")
    sub.add_parser("inventory")
    sub.add_parser("check-config")
    sub.add_parser("demo")
    generate = sub.add_parser("generate-ha")
    generate.add_argument("--output", default="home_assistant/house_climate.yaml")
    generate.add_argument("--dashboard", default="home_assistant/dashboard.json")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.command == "run":
            run(args, config)
        elif args.command == "generate-ha":
            write_package(config, args.output)
            write_dashboard(args.dashboard, config)
            emit("generated", package=args.output, dashboard=args.dashboard,
                 room_count=len(config.zones))
        elif args.command == "inventory":
            ha = HomeAssistant(os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", ""))
            for entity, state in sorted(ha.states().items()):
                attrs = state.get("attributes", {})
                if entity.startswith(("climate.", "fan.")) or attrs.get("device_class") == "temperature" or attrs.get("unit_of_measurement") in ("°F", "°C"):
                    emit("entity", entity_id=entity, state=state["state"], attributes=attrs)
        elif args.command == "demo":
            demo(config)
        else:
            emit("valid", missing_probe_zones=[name for name, zone in config.zones.items() if not zone.probes])
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        emit("error", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
