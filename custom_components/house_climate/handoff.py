"""Durable terminal settings for a controller-owned native installation.

Only an ownership claim made with live permission arms positive handoff writes.
Off cleanup remains separate. A partly dispatched handoff is never blindly
replayed after restart or over an observed operator change.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import math


def now():
    return datetime.now(timezone.utc).timestamp()


def arm(controls, devices):
    fallback = getattr(controls.config, "fallback", None)
    if fallback is None:
        return None
    plan = {
        "version": 1, "stage": "armed", "authorized": bool(controls.live),
        "heads": [entity for entity in controls.config.heads.values() if entity in devices],
        "fans": [entity for entity in dict.fromkeys(controls.config.fans.values()) if entity in devices],
        "heat_low_f": fallback.heat_low_f, "cool_high_f": fallback.cool_high_f,
        "fan_percentage": fallback.fan_percentage,
        "requests": {}, "completed": [], "problems": {}, "stop_started": None,
        "settle_until": None,
    }
    minimums = {fan.entity_id: fan.minimum_percentage for fan in controls.config.fan_controls.values()
                if fan.entity_id in plan["fans"] and fan.minimum_percentage > 0}
    if minimums:
        plan["fan_minimum_percentages"] = minimums
    return plan


def validate(plan):
    if (not isinstance(plan, dict) or plan.get("version") != 1
            or type(plan.get("authorized")) is not bool
            or plan.get("stage") not in ("armed", "stopping", "settling", "applying", "blocked", "superseded")
            or not isinstance(plan.get("heads"), list) or not isinstance(plan.get("fans"), list)
            or any(not isinstance(e, str) or not e.startswith("climate.") for e in plan["heads"])
            or any(not isinstance(e, str) or not e.startswith("fan.") for e in plan["fans"])
            or not isinstance(plan.get("requests"), dict) or not isinstance(plan.get("completed"), list)
            or not isinstance(plan.get("problems"), dict)):
        raise ValueError("Invalid saved recovery handoff")
    for key in ("heat_low_f", "cool_high_f", "fan_percentage"):
        value = plan.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Invalid saved recovery target")
    if not (40 <= plan["heat_low_f"] < plan["cool_high_f"] <= 100
            and 0 <= plan["fan_percentage"] <= 100):
        raise ValueError("Invalid saved recovery target range")
    minimums = plan.get("fan_minimum_percentages", {})
    if (not isinstance(minimums, dict) or set(minimums) - set(plan["fans"])
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or not 0 <= value <= 100 for value in minimums.values())):
        raise ValueError("Invalid saved recovery purifier minimums")
    return deepcopy(plan)


def restored(plan):
    plan = validate(plan)
    # Unknown completion of a dispatched command is not permission to repeat it.
    if plan["requests"]:
        plan["interrupted"] = True
        plan["resume_after"] = now()
    else:
        plan["stage"], plan["settle_until"] = "stopping", None
    return plan


def _desired(controls, plan, entity):
    if entity in plan["heads"]:
        state = controls.hass.states.get(entity)
        if (state is None or state.state in ("unknown", "unavailable")
                or state.attributes.get("restored") or state.attributes.get("assumed_state")
                or "heat_cool" not in state.attributes.get("hvac_modes", ())):
            raise ValueError("Available dual-setpoint Auto capability is required")
        celsius = controls.hass.config.units.temperature_unit in ("°C", "C")
        convert = (lambda f: (f - 32) * 5 / 9) if celsius else float
        return "climate", "set_temperature", {
            "hvac_mode": "heat_cool", "target_temp_low": convert(plan["heat_low_f"]),
            "target_temp_high": convert(plan["cool_high_f"])}
    if entity in plan["fans"]:
        minimum = plan.get("fan_minimum_percentages", {}).get(entity, 0)
        percentage = max(plan["fan_percentage"], minimum)
        from .configuration import purifier_identity_issue
        if issue := purifier_identity_issue(controls.hass, controls.config, entity,
                activation=bool(percentage), require_identity=minimum > 0, controls=controls):
            raise ValueError(issue)
        state = controls.hass.states.get(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            raise ValueError("Available circulation fan is required")
        if percentage and minimum:
            # Use the same integer report grid as ordinary fan control. Never
            # round a saved purification minimum down during a safe handoff.
            try:
                step = float(state.attributes.get("percentage_step", 1))
                if not math.isfinite(step) or not 0 < step <= 100:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Invalid recovery fan percentage step") from None
            rung = max(1, math.ceil((math.ceil(percentage) - 1e-7) / step))
            percentage = min(100, max(1, math.floor(rung * step + 1e-7)))
        return "fan", ("turn_on" if percentage else "turn_off"), (
            {"percentage": percentage} if percentage else {})
    raise ValueError("Entity is not part of the saved recovery handoff")


def _matches(state, data, *, fan=False):
    if (state is None or state.state in ("unknown", "unavailable")
            or state.attributes.get("restored") or state.attributes.get("assumed_state")):
        return False
    try:
        if fan:
            percentage = data.get("percentage", 0)
            return (state.state == ("on" if percentage else "off")
                    and (not percentage or abs(float(state.attributes.get("percentage")) - percentage) <= 1))
        return (state.state == "heat_cool"
                and all(abs(float(state.attributes.get(key)) - data[key]) <= .1
                        for key in ("target_temp_low", "target_temp_high")))
    except (TypeError, ValueError):
        return False


async def _save(controls):
    await controls._save(controls.owned, controls.pending_fans)
    controls._publish()


async def step(controls):
    """Run under the native ownership/check lock; retain all actual jobs."""
    plan = controls.handoff
    if plan is None or controls.takeover_pending:
        return
    current = set(controls.config.heads.values()) | set(controls.config.fans.values())
    expected = set(plan["heads"]) | set(plan["fans"])
    if not expected <= current:
        plan["problems"]["bindings"] = "Recovery bindings changed; positive handoff is inhibited"
        plan["stage"] = "blocked"
    if not plan["authorized"]:
        plan["problems"]["permission"] = "No saved live authorization for positive recovery settings"
        plan["stage"] = "blocked"
    if plan["stage"] in ("armed", "stopping", "blocked") and not plan["requests"]:
        if plan["stage"] != "blocked":
            plan["stage"] = "stopping"
        plan["stop_started"] = plan["stop_started"] or now()
        save_error = None
        try:
            await _save(controls)  # Positive presets need durable intent.
        except Exception as exc:
            save_error = exc
            plan["problems"]["persistence"] = f"Recovery persistence failed: {exc}"
        stop_entities = [e for e in controls.owned if e.startswith("climate.")
                         and not (controls.auxiliary_manual and e == controls.config.heater_entity)]
        await asyncio.gather(*(controls._off(e) for e in stop_entities
            if (not controls._is_off(e) or (e == controls.config.heater_entity
                and (controls._auxiliary_heat_possible or controls.auxiliary_heartbeat_blocked)
                and not controls._ack_auxiliary_heartbeat_off())) and not controls.stop_jobs({e})))
        if save_error is not None:
            controls._publish()
            raise save_error
        plan["problems"].pop("persistence", None)
        head_jobs = controls.stop_jobs(set(plan["heads"])) or controls.pending_device_jobs(set(plan["heads"]))
        head_safe = all(controls._is_off(e) for e in plan["heads"])
        if not head_safe or head_jobs or controls.inflight > controls.auxiliary_inflight:
            if now() - plan["stop_started"] >= controls.config.command_timeout_seconds:
                plan["problems"]["head_stop"] = "Recovery awaits confirmed shared-head Off; activation remains blocked"
            controls._publish()
            return
        plan["problems"].pop("head_stop", None)
        heater = controls.config.heater_entity
        if heater in controls.owned and (not controls._is_off(heater)
                or ((controls._auxiliary_heat_possible or controls.auxiliary_heartbeat_blocked)
                    and not controls._ack_auxiliary_heartbeat_off()) or controls.auxiliary_inflight
                or controls.stop_jobs({heater}) or controls.pending_device_jobs({heater})):
            # A genuine auxiliary stop obligation remains independent of HP recovery.
            controls.pending_auxiliaries = tuple(dict.fromkeys((*controls.pending_auxiliaries, heater)))
            controls.owned = tuple(e for e in controls.owned if e != heater)
            await _save(controls)
        if plan["stage"] == "blocked":
            return
        plan["stage"] = "settling"
        plan["settle_until"] = now() + max(controls.config.off_settle_seconds,
                                           controls.config.head_minimum_off_seconds)
        await _save(controls)
    if plan["stage"] == "settling":
        if not all(controls._is_off(e) for e in plan["heads"]):
            plan["stage"], plan["settle_until"] = "stopping", None
            await _save(controls)
            return
        if now() < plan["settle_until"]:
            controls._publish()
            return
        plan["stage"] = "applying"
        await _save(controls)
    if plan["stage"] != "applying":
        return
    for entity in (*plan["heads"], *plan["fans"]):
        if entity in plan["completed"]:
            continue
        request = plan["requests"].get(entity)
        state = controls.hass.states.get(entity)
        if request:
            job = controls._handoff_jobs.get(entity)
            if job is not None and not job.done():
                if now() - request["at"] >= controls.config.head_activation_timeout_seconds:
                    plan["problems"][entity] = "Recovery command is still running; completion is unknown"
                continue
            if request.get("uncertain") or request.get("cancelled_before_dispatch") or entity in controls._uncertain_device_jobs:
                plan["problems"][entity] = "Recovery command completion is uncertain; ownership is retained"
                continue
            minimum = plan.get("fan_minimum_percentages", {}).get(entity, 0)
            if minimum:
                minimum = max(minimum, plan["fan_percentage"])
            meets_minimum = (not minimum or (state is not None
                and isinstance(state.attributes.get("percentage"), (int, float))
                and not isinstance(state.attributes.get("percentage"), bool)
                and state.attributes["percentage"] >= minimum))
            if (state is not None and state.last_reported.timestamp() > max(request["at"], plan.get("resume_after", 0))
                    and meets_minimum and _matches(state, request["data"], fan=entity in plan["fans"])):
                plan["completed"].append(entity)
                plan["problems"].pop(entity, None)
                await _save(controls)
                continue
            if (state is not None and state.context.user_id is not None
                    and state.last_updated.timestamp() > request["at"]):
                plan["problems"][entity] = "Operator changed this device during recovery; no preset will be replayed"
                continue
            if plan.get("interrupted") or now() - request["at"] >= controls.config.head_activation_timeout_seconds:
                plan["problems"][entity] = "Recovery outcome is unconfirmed; inspect this device before retrying"
            continue
        if plan.get("interrupted"):
            plan["problems"][entity] = "Interrupted recovery needs review before another positive command"
            continue
        try:
            domain, service, data = _desired(controls, plan, entity)
            if controls.repair_gate:
                epoch = controls.repair_gate.capture(entity, activation=True)
                controls.repair_gate.check(entity, epoch, activation=True)
        except (ValueError, RuntimeError) as exc:
            plan["problems"][entity] = str(exc)
            continue
        plan["requests"][entity] = {"at": now(), "data": data}
        await _save(controls)
        controls.service_inflight += 1
        async def dispatch(entity=entity, domain=domain, service=service, data=data,
                           epoch=epoch if controls.repair_gate else None,
                           generation=controls.ownership_generation):
            started = False
            try:
                if (controls.handoff is not plan or controls.takeover_pending
                        or controls.enabled or generation != controls.ownership_generation):
                    return
                current_state = controls.hass.states.get(entity)
                if (current_state is not None and current_state.context.user_id is not None
                        and current_state.last_updated.timestamp() > plan["requests"][entity]["at"]):
                    plan["problems"][entity] = "Operator changed this device during recovery; no preset will be replayed"
                    return
                if controls.repair_gate:
                    controls.repair_gate.check(entity, epoch, activation=True)
                if controls.pending_device_jobs({entity}) or controls.stop_jobs({entity}):
                    plan["problems"][entity] = "Previous device command remains unresolved; recovery activation blocked"
                    return
                if domain == "fan":
                    from .configuration import purifier_identity_issue
                    if issue := purifier_identity_issue(controls.hass, controls.config, entity,
                            activation=bool(data.get("percentage")),
                            require_identity=plan.get("fan_minimum_percentages", {}).get(entity, 0) > 0,
                            controls=controls):
                        raise ValueError(issue)
                plan["requests"][entity]["dispatched"] = True
                controls.head_activation_started(entity)
                started = True
                await controls.hass.services.async_call(domain, service, {"entity_id": entity, **data}, blocking=True)
            except Exception as exc:
                plan["problems"][entity] = f"Recovery command failed: {exc}"
            finally:
                if started:
                    controls.head_activation_finished(entity)
        task = controls.hass.async_create_background_task(dispatch(), "Home Climate recovery setting", eager_start=False)
        controls._handoff_jobs[entity] = task
        def finished(job, entity=entity):
            controls.service_inflight -= 1
            if job.cancelled():
                request = plan["requests"][entity]
                if request.get("dispatched"):
                    request["uncertain"] = True
                    controls.activation_job_uncertain(entity)
                    plan["problems"][entity] = "Recovery command was cancelled after dispatch; completion is uncertain"
                else:
                    request["cancelled_before_dispatch"] = True
                    plan["problems"][entity] = "Recovery command was cancelled before dispatch; explicit retry is required"
            controls._schedule_check()
        task.add_done_callback(finished)
        await asyncio.wait({task}, timeout=5)
    if (set(plan["completed"]) >= expected
            and not controls.stop_jobs(expected) and not controls.pending_device_jobs(expected)
            and not any(not job.done() for entity, job in controls._handoff_jobs.items() if entity in expected)):
        previous = (controls.handoff, controls.owned, controls.pending_fans)
        controls.handoff, controls.owned = None, ()
        try:
            await controls._save((), controls.pending_fans)
        except BaseException:
            controls.handoff, controls.owned, controls.pending_fans = previous
            raise
        controls.recovering = False
        controls.ownership_generation += 1
        if controls.config.heater_entity not in controls.pending_auxiliaries:
            controls._reset_auxiliary_heartbeat()
    else:
        await _save(controls)
    controls._publish()
