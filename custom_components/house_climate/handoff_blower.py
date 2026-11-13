"""Bounded blower-Auto cleanup attached to the native recovery handoff.

Reported Auto is cached-state evidence, never physical fan confirmation. A
reserved write without its finished receipt is reconciled, not replayed.
"""
from __future__ import annotations

import asyncio
import math
import time

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

MAX_ATTEMPTS = 3
RETRY_SECONDS = 15
REPORT_MAX_AGE = 180
DONE = {"complete", "superseded"}


def validate(plan):
    """Reject malformed recovery intent rather than interpreting it as authority."""
    records = plan.get("blower_recovery", {})
    if not isinstance(records, dict) or len(records) > 64:
        raise ValueError("Invalid blower recovery mapping")
    for entity, record in records.items():
        if (entity not in plan.get("heads", ()) or not isinstance(record, dict)
                or not {"trial_id", "mode", "baseline", "armed_at", "stage", "attempts"} <= record.keys()
                or not isinstance(record["stage"], str) or record["stage"] not in {"pending", "reserved", "blocked", *DONE}
                or type(record["attempts"]) is not int or not 0 <= record["attempts"] <= MAX_ATTEMPTS
                or (record["attempts"] > 0 and record.get("first_at") is None)):
            raise ValueError("Invalid blower recovery record")
        for key in ("trial_id", "mode", "baseline"):
            if not isinstance(record[key], str) or not record[key].strip() or len(record[key]) > 256:
                raise ValueError("Invalid blower recovery identity or mode")
        for key in ("armed_at", "first_at", "reserved_at", "dispatched_at", "finished_at"):
            value = record.get(key)
            if value is None and key != "armed_at":
                continue
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid blower recovery timestamp")
        for key in ("operator_changed", "superseded", "uncertain", "rejected", "manual_hvac"):
            if key in record and type(record[key]) is not bool:
                raise ValueError("Invalid blower recovery flag")
    return plan


def pending(plan):
    return bool(plan and any(record.get("stage") not in DONE or record.get("manual_hvac")
                            for record in plan.get("blower_recovery", {}).values()))


def notification_disposition(controls, plan, entity):
    """Keep bounded automatic recovery quiet; never hide an unsafe blocker."""
    record = plan.get("blower_recovery", {}).get(entity)
    if not record or plan.get("stage") != "applying" or plan.get("interrupted"):
        return "human_required"
    state = controls.hass.states.get(entity)
    current = time.time()
    attempts = record.get("attempts", 0)
    first = record.get("first_at")
    if (record.get("stage") != "reserved" or type(attempts) is not int
            or not 1 <= attempts <= MAX_ATTEMPTS or type(first) not in (int, float)
            or not 0 <= current - first < controls.config.head_activation_timeout_seconds
            or any(record.get(flag) for flag in ("rejected", "uncertain", "manual_hvac", "operator_changed", "superseded"))
            or _changed(state, record) or controls.handoff is not plan or controls.enabled
            or controls.takeover_pending or not plan.get("authorized") or entity not in controls.owned
            or entity not in controls.config.heads.values() or entity in controls._uncertain_device_jobs
            or controls.stop_jobs({entity}) or controls.pending_device_jobs({entity})
            or not _fresh(state) or state.state not in ("off", "heat_cool")
            or "auto" not in state.attributes.get("fan_modes", ())
            or state.attributes.get("fan_mode") not in (record["mode"], record["baseline"], "auto")
            or (controls.repair_gate and controls.repair_gate.held(entity))):
        return "human_required"
    job = controls._handoff_jobs.get(entity)
    finished = record.get("finished_at")
    if finished is None:
        return "recovering" if record.get("dispatched_at") and job is not None and not job.done() else "human_required"
    if type(finished) not in (int, float) or not first <= finished <= current:
        return "human_required"
    # All attempts, including the last, get a bounded feedback interval. Beyond
    # it only fresh mismatch evidence can authorize another automatic attempt.
    if current < finished + RETRY_SECONDS:
        return "recovering"
    if attempts < MAX_ATTEMPTS and _fresh(state, finished):
        return "recovering"
    return "human_required"


def _fresh(state, boundary=0):
    return (state is not None and state.state not in ("unknown", "unavailable")
            and not state.attributes.get("restored") and not state.attributes.get("assumed_state")
            and boundary < state.last_reported.timestamp() <= time.time()
            and time.time() - state.last_reported.timestamp() <= REPORT_MAX_AGE)


def _changed(state, record):
    return (record.get("stage") == "superseded" or record.get("operator_changed") or record.get("superseded")
            or (state is not None and state.context.user_id is not None
                and state.last_updated.timestamp() > record["armed_at"]))


async def _resolved(controls, record):
    manager = getattr(controls, "head_fan_trials", None)
    if manager is not None:
        await manager.async_fallback_resolved(record)


async def step(controls, plan):
    """Advance each blower obligation; return whether every one is resolved.

    Caller still advances HVAC fallback on failure, but must not release final
    ownership while this returns False, or overlap the per-entity actual job.
    """
    records = plan.get("blower_recovery", {})
    for entity, record in records.items():
        if record.get("stage") in DONE:
            await _resolved(controls, record)
            continue
        problem = f"{entity}:blower"
        state = controls.hass.states.get(entity)
        job = controls._handoff_jobs.get(entity)
        if job is not None and not job.done():
            continue
        if _changed(state, record):
            record["stage"] = "superseded"
            record["reason"] = "operator_change_preserved"
            plan["problems"].pop(problem, None)
            await controls._save(controls.owned, controls.pending_fans)
            await _resolved(controls, record)
            continue
        boundary = max(record["armed_at"], record.get("dispatched_at", 0), plan.get("resume_after", 0))
        if (entity in controls._uncertain_device_jobs or controls.pending_device_jobs({entity})
                or controls.stop_jobs({entity})):
            plan["problems"][problem] = "Blower recovery is waiting for unresolved device commands"
            continue
        if _fresh(state, boundary) and state.attributes.get("fan_mode") == "auto":
            record["stage"] = "complete"
            record["reason"] = "auto_reported_physical_unverified"
            plan["problems"].pop(problem, None)
            await controls._save(controls.owned, controls.pending_fans)
            await _resolved(controls, record)
            continue
        if (record.get("stage") == "reserved" and not record.get("finished_at")) or record.get("uncertain"):
            plan["problems"][problem] = "Interrupted blower recovery outcome is unknown; no command replayed"
            continue
        if (controls.handoff is not plan or controls.enabled or controls.takeover_pending
                or not plan.get("authorized") or entity not in controls.owned
                or entity not in controls.config.heads.values()):
            plan["problems"][problem] = "Blower recovery lacks current handoff authority"
            continue
        if not _fresh(state) or state.state not in ("off", "heat_cool"):
            plan["problems"][problem] = "Blower recovery needs an available stopped or Auto heat pump"
            continue
        if "auto" not in state.attributes.get("fan_modes", ()):
            plan["problems"][problem] = "Heat pump does not advertise blower Auto"
            continue
        reported = state.attributes.get("fan_mode")
        if not isinstance(reported, str) or not reported:
            plan["problems"][problem] = "Blower recovery needs an available fan-mode report"
            continue
        if reported not in (record["mode"], record["baseline"], "auto"):
            record["stage"] = "superseded"
            record["reason"] = "new_fan_setting_preserved"
            await controls._save(controls.owned, controls.pending_fans)
            await _resolved(controls, record)
            continue
        attempts = record.get("attempts", 0)
        if record.get("rejected") or attempts >= MAX_ATTEMPTS:
            plan["problems"][problem] = "Blower Auto recovery requires attention; bounded attempts exhausted or rejected"
            continue
        if attempts:
            finished = record.get("finished_at") or 0
            if (time.time() < finished + RETRY_SECONDS or not _fresh(state, finished)):
                continue
            if time.time() >= record["first_at"] + controls.config.head_activation_timeout_seconds:
                plan["problems"][problem] = "Blower Auto recovery feedback deadline expired"
                continue
        try:
            epoch = controls.repair_gate.capture(entity, activation=True) if controls.repair_gate else None
            if controls.repair_gate:
                controls.repair_gate.check(entity, epoch, activation=True)
        except (ValueError, RuntimeError) as exc:
            plan["problems"][problem] = str(exc)
            continue
        context = Context()
        record.update(stage="reserved", attempts=attempts + 1, context_id=context.id,
                      reserved_at=time.time(), finished_at=None)
        record.setdefault("first_at", time.time())
        generation = controls.ownership_generation
        await controls._save(controls.owned, controls.pending_fans)

        async def dispatch(entity=entity, record=record, context=context, epoch=epoch,
                           generation=generation, problem=problem):
            started = False
            try:
                current = controls.hass.states.get(entity)
                if _changed(current, record):
                    record.update(stage="superseded", reason="operator_change_preserved")
                    return
                if (controls.handoff is not plan or controls.enabled or controls.takeover_pending
                        or not plan.get("authorized") or generation != controls.ownership_generation
                        or entity not in controls.owned or controls.pending_device_jobs({entity})
                        or controls.stop_jobs({entity}) or entity in controls._uncertain_device_jobs
                        or not _fresh(current) or current.state not in ("off", "heat_cool")
                        or "auto" not in current.attributes.get("fan_modes", ())
                        or current.attributes.get("fan_mode") not in (record["mode"], record["baseline"], "auto")
                        or time.time() >= record["first_at"] + controls.config.head_activation_timeout_seconds):
                    record.update(stage="blocked", reason="authority_or_capability_changed_before_dispatch")
                    plan["problems"][problem] = "Blower recovery authority or capability changed before dispatch"
                    return
                if controls.repair_gate:
                    controls.repair_gate.check(entity, epoch, activation=True)
                record["dispatched_at"] = time.time()
                controls.head_activation_started(entity)
                started = True
                await controls.hass.services.async_call("climate", "set_fan_mode",
                    {"entity_id": entity, "fan_mode": "auto"}, blocking=True, context=context)
                record["reason"] = "awaiting_auto_report_physical_unverified"
            except Exception as exc:
                record["rejected"] = isinstance(exc, ServiceValidationError)
                plan["problems"][problem] = f"Blower recovery command failed: {exc}"
            finally:
                if started:
                    record["finished_at"] = time.time()
                    controls.head_activation_finished(entity)
                await controls._save(controls.owned, controls.pending_fans)
                if record.get("stage") in DONE:
                    await _resolved(controls, record)

        controls.service_inflight += 1
        task = controls.hass.async_create_background_task(dispatch(), "Home Climate blower recovery", eager_start=False)
        controls._handoff_jobs[entity] = task

        def finished(job, record=record, entity=entity):
            controls.service_inflight -= 1
            if job.cancelled():
                record["uncertain"] = True
                if record.get("dispatched_at"):
                    controls.activation_job_uncertain(entity)
            controls._schedule_check()

        task.add_done_callback(finished)
        await asyncio.wait({task}, timeout=5)
    return all(record.get("stage") in DONE for record in records.values())
