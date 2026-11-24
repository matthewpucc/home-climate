"""Evidence-gated native ownership transfer. Neither transaction writes equipment."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import time

from .controller.ha import HAError
from .controller.manual_head_direction import trusted_mode
from .manual_heads import ManualHead

OPT_IN_CONFIG = "home_climate_manual_head_handoff.json"
_LOGGER = logging.getLogger(__name__)


def read_opt_in(path, roles):
    try:
        with open(path, "rb") as source:
            raw = source.read(2049)
        values = json.loads(raw)
        if (len(raw) > 2048 or not isinstance(values, list) or len(values) > len(roles)
                or any(not isinstance(role, str) or role not in roles for role in values)
                or len(set(values)) != len(values)):
            raise ValueError
        return frozenset(values)
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError):
        _LOGGER.warning("Automatic manual head handoff disabled: invalid opt-in configuration")
        return frozenset()


def _fresh_off(controls, entity, boundary, *, exact=None):
    state = controls.hass.states.get(entity)
    if state is None:
        return None
    stamp = state.last_reported.timestamp()
    if exact is not None and stamp != exact:
        return None
    attributes = state.attributes
    if exact is not None and (state.context.user_id is not None or state.context.parent_id is not None):
        return None
    now = datetime.now(timezone.utc).timestamp()
    if exact is not None and not 0 <= now - stamp <= 15:
        return None
    return stamp if trusted_mode({
        "state": state.state, "attributes": attributes, "last_reported": state.last_reported.isoformat(),
        "integration_repair": controls.repair_gate.overlay(entity) if controls.repair_gate else {},
    }, now, boundary) == "off" else None


def _outstanding(controls, entity):
    trial_manager = getattr(controls, "head_fan_trials", None)
    trial = trial_manager.trial if trial_manager is not None else None
    job = controls._handoff_jobs.get(entity)
    return (controls._head_inflight.get(entity, 0) or entity in controls._head_stop_pending
            or entity in controls._uncertain_device_jobs or controls.pending_device_jobs({entity})
            or controls.stop_jobs({entity}) or (job is not None and not job.done())
            or entity in (controls.handoff or {}).get("blower_recovery", {})
            or (trial_manager is not None and trial_manager._lock.locked())
            or (trial is not None and trial.entity_id == entity and trial.stage not in {"complete", "cancelled"}))


def _can_drop_terminal_presets(controls):
    plan = controls.handoff
    manager = getattr(controls, "head_fan_trials", None)
    trial = manager.trial if manager is not None else None
    return ((plan is None or (plan["stage"] == "armed" and not plan["requests"]
                               and not plan.get("blower_recovery")))
            and (manager is None or not manager._lock.locked())
            and (trial is None or trial.stage in {"complete", "cancelled"}))


def _lease_fresh(controls):
    return 0 <= time.monotonic() - controls.heartbeat_monotonic <= controls.config.heartbeat_timeout_seconds


def request_release(controls, entity, marker, generation):
    roles = [role for role, candidate in controls.config.heads.items() if candidate == entity]
    if (len(roles) != 1 or roles[0] not in controls.manual_handoff_opt_in
            or entity in controls.manual_head_entities or controls.manual_handoff_pending):
        return
    role = roles[0]
    identity = controls._replacement_registry_identity(entity)
    if (identity is None or not _lease_fresh(controls)
            or controls._off_observation_eligible(entity) != generation
            or not _can_drop_terminal_presets(controls)
            or _outstanding(controls, entity) or not _fresh_off(controls, entity,
                         controls._manual_head_boundary, exact=marker)):
        return
    # Fence synchronously in the reporting callback, before any worker request
    # can use the newly confirmed Off. In-flight/uncertain jobs are never
    # cancelled or assumed stopped: such a head is ineligible for release.
    controls.head_write_fence.rotate(entity, blocked=True)
    controls.manual_handoff_pending = entity
    controls._publish()
    task = controls.hass.async_create_task(
        _release(controls, role, entity, identity, marker, generation), "Home Climate manual head handoff")
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def _release(controls, role, entity, identity, marker, generation):
    keep_fence = False
    try:
        async with controls._check_lock:
            async with controls._lock:
                if (not _lease_fresh(controls) or controls._off_observation_eligible(entity) != generation
                        or controls.hass.is_stopping or not _can_drop_terminal_presets(controls)
                        or _outstanding(controls, entity)
                        or controls._replacement_registry_identity(entity) != identity
                        or not _fresh_off(controls, entity, controls._manual_head_boundary, exact=marker)):
                    return
                owned = tuple(candidate for candidate in controls.owned if candidate != entity)
                records = {**controls.manual_heads, role: ManualHead(role, entity, identity, marker)}
                # With a manual participant there is no safe terminal
                # Auto direction. Persist an Off-only managed cleanup plan.
                candidate_plan = None
                try:
                    await controls._save(owned, controls.pending_fans, handoff=candidate_plan,
                                         manual_heads=records, verify_write=True)
                except Exception:
                    # A logged write failure and a failure reading its result
                    # are not equivalent. Reopen only after prior ownership is
                    # verified back on disk; if this can't be verified, the
                    # pending fence remains visible and prohibits new starts.
                    try:
                        await controls._save(controls.owned, controls.pending_fans, verify_write=True)
                    except Exception:
                        keep_fence = True
                        controls.manual_handoff_issue = "Handoff storage uncertain; inspect before restarting"
                    else:
                        controls.manual_handoff_issue = "Handoff not saved; Home Climate retained ownership"
                    _LOGGER.warning("Home Climate could not verify manual head ownership for %s", entity)
                    return
                controls.owned, controls.handoff = owned, candidate_plan
                controls.manual_heads = records
                controls.manual_head_entities = controls.manual_head_entities | {entity}
                controls._head_possible.discard(entity)
                controls._head_off_receipts.pop(entity, None)
                controls._head_off_baselines.pop(entity, None)
                controls._replacement_startup_off.pop(entity, None)
                controls.manual_handoff_issue = None
                keep_fence = True
                _LOGGER.warning("Home Climate released %s after unexplained Off; manual ownership is active", entity)
    except asyncio.CancelledError:
        # Lifecycle shutdown must not turn uncertain persistence into a write.
        keep_fence = True
        controls.manual_handoff_issue = "Handoff interrupted; inspect saved ownership"
        raise
    finally:
        if not keep_fence:
            controls.head_write_fence.rotate(entity, blocked=False)
        if not keep_fence or entity in controls.manual_head_entities:
            controls.manual_handoff_pending = None
        controls._publish()
        controls._schedule_check()


async def resume(controls, entity):
    """Clear one disposition only with Controller Off; normal On owns reacquisition."""
    async with controls._check_lock:
        async with controls._lock:
            selected = [(role, record) for role, record in controls.manual_heads.items()
                        if entity == record.entity and controls.config.heads.get(role) == entity]
            if len(selected) != 1:
                raise HAError("Select the exact configured manually controlled heat pump")
            role, record = selected[0]
            if (not controls.live or controls.enabled or controls.enabled_intent or controls.owned
                    or controls.recovering or controls._closing or controls.hass.is_stopping
                    or controls.takeover_pending or controls.manual_handoff_pending
                    or _outstanding(controls, entity)):
                raise HAError("Switch Controller Off and wait for ownership cleanup before resuming one head")
            if (controls._replacement_registry_identity(entity) != record.registry_id
                    or not _fresh_off(controls, entity, max(controls._manual_head_boundary, record.reported_off_at))):
                raise HAError("Turn the manual head Off and wait for a fresh native report before resuming")
            records = {key: row for key, row in controls.manual_heads.items() if key != role}
            try:
                await controls._save(controls.owned, controls.pending_fans, manual_heads=records,
                                     verify_write=True)
            except Exception:
                try:
                    await controls._save(controls.owned, controls.pending_fans, verify_write=True)
                except Exception:
                    controls.manual_handoff_issue = "Resume storage uncertain; inspect before restarting"
                    controls._publish()
                raise HAError("Resume was not verified; the heat pump remains fenced") from None
            # Exact wiring and registry identity were checked, so the selected
            # stored, configured and registry endpoints are one entity.
            controls.manual_heads = records
            controls.manual_head_entities = controls.manual_head_entities - {entity}
            controls.head_write_fence.rotate(entity, blocked=False)
            controls.manual_handoff_issue = None
            controls._publish()
