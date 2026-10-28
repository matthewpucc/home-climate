"""Native control interlocks and an HA-loop watchdog independent of the worker.

Only ownership is persisted. Enabling control is an explicit, per-load action.
The worker sees an internal adapter snapshot; no fake HA helper entities are made.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .controller.ha import HAActivationDeferred, HAError
from .controller.ha_package import watchdog_signature
from .controller.sensors import probe_value

_LOGGER = logging.getLogger(__name__)



class NativeControls:
    def __init__(self, hass, config, live, entry_id, notify):
        self.hass, self.config, self.live, self.notify = hass, config, live, notify
        self.store = Store(hass, 1, f"house_climate.{entry_id}.ownership")
        self.enabled = False
        self.active = False
        self.owned = ()
        self.pending_fans = ()
        self._fan_problems = {}
        self.recovering = False
        self.heartbeat = 0.0
        self.heartbeat_monotonic = 0.0
        self.inflight = 0
        self.service_inflight = 0
        self._stop_jobs = {}
        self.repair_gate = None
        self.ownership_generation = 0
        self.auxiliary_inflight = 0
        self._auxiliary_heat_possible = False
        self.auxiliary_heartbeat_blocked = False
        self._auxiliary_heartbeat_baseline = None
        self._auxiliary_heartbeat_off_receipt = False
        self._auxiliary_heartbeat_off_ack = False
        self._auxiliary_stop_pending = False
        self._auxiliary_stop_epoch = 0
        self._head_inflight = {entity: 0 for entity in config.heads.values()}
        self._head_possible = set()
        self._head_stop_epochs = {entity: 0 for entity in config.heads.values()}
        self._head_stop_pending = set()
        self._head_off_baselines = {}
        self._head_off_receipts = {}
        self._uncertain_device_jobs = set()
        self.problem = None
        self._auxiliary_off_error = None
        self._stop_log_reason = None
        self._shutdown_save_error = None
        self._lock = asyncio.Lock()
        # Serializes every watchdog service with ownership claims and close.
        self._check_lock = asyncio.Lock()
        self._claiming = False
        self._closing = False
        self._unsubscribers = []
        self._task = None
        self._snapshot = {}
        self.power_guard = None
        self._publish()

    async def async_load(self):
        data = await self.store.async_load()
        if data:
            devices = data.get("devices", [])
            if not isinstance(devices, list) or any(
                not isinstance(entity, str) or not entity.startswith(("climate.", "fan."))
                for entity in devices
            ):
                raise HAError("Invalid saved controller ownership")
            pending = data.get("pending_fans", [])
            if not isinstance(pending, list) or any(
                not isinstance(entity, str) or not entity.startswith("fan.")
                for entity in pending
            ):
                raise HAError("Invalid saved pending circulation fans")
            self.owned = tuple(dict.fromkeys(devices))
            self.pending_fans = tuple(entity for entity in dict.fromkeys(pending)
                                      if entity not in self.owned)
            self.recovering = bool(self.owned)
            # A prior process may have dispatched heat while HA still cached Off.
            # Restored auxiliary ownership needs a fresh superseding stop.
            self._auxiliary_heat_possible = bool(self.config.heater_entity in self.owned)
            if self.owned:
                self._log_stop(logging.INFO, "Recovering previously owned devices after integration load")
        self._publish()

    @callback
    def async_start(self):
        if self.active or self._closing:
            return
        self.active = True
        entities = {*self.config.climate_entities, *self.config.fans.values(), *self.owned, *self.pending_fans,
                    *(probe.entity_id for room in self.config.zones.values() for probe in room.probes)}
        self._unsubscribers = [
            async_track_time_interval(self.hass, self._schedule_check, timedelta(seconds=15)),
            async_track_state_change_event(self.hass, entities, self._schedule_check),
        ]
        self._publish()
        self._schedule_check()

    def _publish(self):
        # Assignment publishes a coherent snapshot for synchronous worker reads.
        self._snapshot = {
            "enabled": self.enabled and not self.recovering and not self._claiming,
            "owned": bool(self.owned) and not self.recovering, "active": self.active,
            "heartbeat": self.heartbeat,
            "heartbeat_monotonic": self.heartbeat_monotonic,
            "generation": self.ownership_generation,
            "auxiliary_heartbeat_blocked": self.auxiliary_heartbeat_blocked,
            "auxiliary_heartbeat_reason": self.auxiliary_heartbeat_reason,
        }
        self.notify()

    def overlay(self, states):
        snapshot = self._snapshot
        entity = self.config.entity
        result = dict(states)
        for key, state, attrs in (
            (entity("enabled"), "on" if snapshot["enabled"] else "off", {}),
            (entity("heater_owned"), "on" if snapshot["owned"] else "off", {}),
            (entity("heartbeat", "input_datetime"), str(snapshot["heartbeat"]),
             {"timestamp": snapshot["heartbeat"]}),
            (entity("status", "input_text"), "", {}),
            (entity("auxiliary_watchdog", "automation"), "on" if snapshot["active"] else "off", {}),
            (entity("auxiliary_probe_safe", "binary_sensor"), "on" if self.probes_safe(states) else "off",
             {"configuration_signature": watchdog_signature(self.config)}),
        ):
            result[key] = {"state": state, "attributes": attrs}
        return result

    def probes_safe(self, states):
        if not self.config.heater_entity:
            return True
        values = []
        now = datetime.now(timezone.utc)
        for probe in self.config.zones[self.config.auxiliary_room].probes:
            try:
                values.append(probe_value(probe, states, now, self.config.sensor_stale_seconds,
                                          self.hass.config.units.temperature_unit))
            except (ValueError, TypeError, OverflowError):
                continue
        return bool(values) and max(values) < self.config.heater_cutoff_f

    def _probe_states(self):
        if not self.config.heater_entity:
            return {}
        result = {}
        for probe in self.config.zones[self.config.auxiliary_room].probes:
            state = self.hass.states.get(probe.entity_id)
            if state:
                result[probe.entity_id] = {
                    "state": state.state, "attributes": dict(state.attributes),
                    "last_reported": state.last_reported.isoformat(),
                }
        return result

    @property
    def hint(self):
        if self.problem:
            return self.problem
        if self.recovering:
            return "Waiting for previously owned devices to stop"
        if not self.live:
            return "Enable live commands in Configure → System settings"
        if not self.enabled:
            return "Turn on Controller enabled on this device"
        return None

    def _log_stop(self, level, reason):
        """Keep the original global-stop cause visible without retry spam."""
        if self._stop_log_reason is not None:
            return
        self._stop_log_reason = reason
        try:
            _LOGGER.log(
                level, "Home Climate watchdog: %s; Off requested for all %d owned devices: %s",
                reason, len(self.owned), ", ".join(self.owned) or "none",
            )
        except Exception:
            # A failing custom log handler must never prevent safety commands.
            pass

    def _log_shutdown_save_error(self, message):
        if self._shutdown_save_error == message:
            return
        self._shutdown_save_error = message
        if self._stop_log_reason is None:
            self._stop_log_reason = message
        try:
            _LOGGER.error(
                "Home Climate watchdog: %s; Off requested for all %d owned devices: %s",
                message, len(self.owned), ", ".join(self.owned) or "none",
            )
        except Exception:
            # Reporting persistence failure must not interrupt its shutdown.
            pass

    @callback
    def pause(self, reason=None):
        self._log_stop(logging.WARNING, reason or "Controller pause requested")
        self.enabled = False
        self.ownership_generation += 1
        self.recovering = bool(self.owned)
        if reason:
            self.problem = reason
        self._publish()
        self._schedule_check()

    async def async_set_enabled(self, enabled):
        if enabled:
            if self._closing:
                raise HAError("Controller is stopping")
            if not self.active:
                raise HAError("Home Assistant is still starting")
            if not self.live:
                raise HAError("Enable live commands in Configure → System settings first")
            self.config.validate(live=True)
            if (self.recovering or (self.owned and not self.enabled)
                    or (not self.enabled and self.service_inflight)):
                raise HAError("Waiting for previously owned devices to acknowledge off")
            self.problem = None
            self._stop_log_reason = None
            self._shutdown_save_error = None
        elif self.owned:
            self._log_stop(logging.INFO, "Controller disabled")
        if enabled != self.enabled:
            self.ownership_generation += 1
        self.enabled = enabled
        if not enabled and self.owned:
            self.recovering = True
        self._publish()
        self._schedule_check()

    def accepts(self, domain, service, entity):
        return (domain, service, entity) in {
            ("input_boolean", "turn_on", self.config.entity("heater_owned")),
            ("input_boolean", "turn_off", self.config.entity("heater_owned")),
            ("input_datetime", "set_datetime", self.config.entity("heartbeat", "input_datetime")),
            ("input_text", "set_value", self.config.entity("status", "input_text")),
        }

    async def _save(self, devices, pending):
        data = {"devices": list(devices)}
        if pending:
            data["pending_fans"] = list(pending)
        await self.store.async_save(data)

    async def async_service(self, domain, service, data):
        if domain == "input_boolean":
            if service == "turn_on":
                # No cleanup off request may outlive the ownership claim. The
                # same lock covers both scheduled and directly awaited checks.
                if self._claiming:
                    raise HAError("Controller ownership is already being claimed")
                self._claiming = True
                self._publish()
                try:
                    async with self._check_lock:
                        async with self._lock:
                            if (not self.active or not self.enabled or not self.live
                                    or self.recovering or self._closing):
                                raise HAError("Control is disabled or still stopping")
                            if not self.owned:
                                if self.service_inflight:
                                    raise HAError("Previous device services are still running; ownership claim deferred")
                                devices = tuple([*self.config.climate_entities,
                                                 *self.config.fans.values()])
                                pending = tuple(entity for entity in self.pending_fans
                                                if entity not in devices)
                                try:
                                    # Reclaim current fans and preserve old mapped
                                    # fans atomically before permitting activation.
                                    await self._save(devices, pending)
                                except BaseException:
                                    self.enabled = False
                                    self._publish()
                                    raise
                                self.owned = devices
                                self.ownership_generation += 1
                                self.pending_fans = pending
                                self.recovering = not self.enabled
                            self.heartbeat = datetime.now(timezone.utc).timestamp()
                            self.heartbeat_monotonic = time.monotonic()
                finally:
                    self._claiming = False
                    self._publish()
                    self._schedule_check()
            else:
                self.recovering = bool(self.owned)
                self._publish()
                await self._clear_if_off()
                self._schedule_check()
        elif domain == "input_datetime":
            self.confirm_heartbeat()
        # The old input_text is only a CLI diagnostic sink; native sensors expose it.

    @property
    def auxiliary_heartbeat_reason(self):
        if not self.auxiliary_heartbeat_blocked:
            return None
        if self._auxiliary_heartbeat_off_ack:
            return "Auxiliary heater stopped after heartbeat expiry; waiting for a fresh heartbeat"
        return "Auxiliary heater heartbeat expired; waiting for fresh Off acknowledgement"

    def _auxiliary_marker(self):
        state = self.hass.states.get(self.config.heater_entity) if self.config.heater_entity else None
        if state is None:
            return None
        return (state.state, state.attributes.get("hvac_action"),
                state.attributes.get("temperature"), state.last_reported,
                state.last_updated, state.last_changed)

    def _auxiliary_could_heat(self):
        if (self.power_guard is not None
                and getattr(self.power_guard, "emergency_fallback_enabled", False) is True):
            return not self.power_guard.shutdown_confirmed
        if self.power_guard is not None and self.power_guard.power_cut_confirmed:
            return False
        state = self.hass.states.get(self.config.heater_entity) if self.config.heater_entity else None
        return (state is None or state.state not in ("off", "fan_only")
                or state.attributes.get("hvac_action") == "heating")

    def _reset_auxiliary_heartbeat(self):
        self._auxiliary_heat_possible = False
        self.auxiliary_heartbeat_blocked = False
        self._auxiliary_heartbeat_baseline = None
        self._auxiliary_heartbeat_off_receipt = False
        self._auxiliary_heartbeat_off_ack = False
        self._auxiliary_stop_pending = False

    def _head_marker(self, entity):
        state = self.hass.states.get(entity)
        return ((state.state, state.last_reported, state.last_updated,
                 state.attributes.get("hvac_action")) if state is not None else None)

    def _ack_head_off(self, entity):
        state = self.hass.states.get(entity)
        acknowledged = bool(
            not self._head_inflight.get(entity, 0)
            and self._head_off_receipts.get(entity)
                == (self.ownership_generation, self._head_stop_epochs.get(entity))
            and state is not None and state.state == "off"
            and state.attributes.get("hvac_action") not in ("heating", "cooling")
            and self._head_marker(entity) != self._head_off_baselines.get(entity))
        if acknowledged:
            self._head_possible.discard(entity)
            self._head_stop_pending.discard(entity)
        return acknowledged

    @callback
    def head_activation_started(self, entity):
        if entity not in self._head_inflight:
            return
        self._head_inflight[entity] += 1
        self._head_possible.add(entity)
        self._head_stop_epochs[entity] += 1
        self._head_stop_pending.discard(entity)
        self._head_off_receipts.pop(entity, None)

    @callback
    def head_activation_finished(self, entity):
        if entity not in self._head_inflight:
            return
        self._head_inflight[entity] -= 1
        if (entity in self._head_stop_pending or self.recovering
                or not self.enabled or self._closing):
            # A prior Off can precede the actual completion of a timed-out
            # positive service, even when HA still caches that same Off state.
            self._head_stop_epochs[entity] += 1
            self._head_stop_pending.add(entity)
            self._head_off_baselines[entity] = self._head_marker(entity)
            self._head_off_receipts.pop(entity, None)

    @callback
    def head_off_started(self, entity):
        if entity not in self._head_inflight or entity not in self._head_possible:
            return None
        if entity not in self._head_stop_pending:
            self._head_stop_epochs[entity] += 1
            self._head_stop_pending.add(entity)
            self._head_off_baselines[entity] = self._head_marker(entity)
            self._head_off_receipts.pop(entity, None)
        return (self.ownership_generation, self._head_stop_epochs[entity])

    @callback
    def head_off_finished(self, entity, receipt):
        if (entity in self._head_stop_pending
                and receipt == (self.ownership_generation, self._head_stop_epochs.get(entity))):
            self._head_off_receipts[entity] = receipt
            self._ack_head_off(entity)
        elif entity in self._head_stop_pending:
            # This report arrived from a stop predating a later positive
            # completion. It cannot be reused by a newer no-report receipt.
            self._head_off_baselines[entity] = self._head_marker(entity)
            self._head_off_receipts.pop(entity, None)

    @callback
    def activation_job_uncertain(self, entity):
        if entity not in self.config.climate_entities:
            return
        self._uncertain_device_jobs.add(entity)
        self.problem = f"Device service cancelled after dispatch; completion remains uncertain: {entity}"
        self._publish()
        self._schedule_check()

    def _check_auxiliary_heartbeat_deadline(self, instant):
        if (not self.config.heater_entity or self.config.heater_entity not in self.owned
                or not self.enabled or self.recovering or not self.live):
            return
        age = instant - self.heartbeat_monotonic
        if (not self.auxiliary_heartbeat_blocked
                and not 0 <= age <= self.config.auxiliary_heartbeat_timeout_seconds
                and (self.auxiliary_inflight or self._auxiliary_heat_possible
                     or self._auxiliary_could_heat())):
            self.auxiliary_heartbeat_blocked = True
            self._auxiliary_stop_epoch += 1
            self._auxiliary_heartbeat_baseline = self._auxiliary_marker()
            self._auxiliary_heartbeat_off_receipt = False
            self._auxiliary_heartbeat_off_ack = False
            self._auxiliary_stop_pending = True
            try:
                _LOGGER.warning(
                    "Auxiliary heater heartbeat expired (age %.1fs; lease limit %.0fs); "
                    "Off requested for %s; shared heat-pump heads retain their own lease",
                    age, self.config.auxiliary_heartbeat_timeout_seconds, self.config.heater_entity)
            except Exception:
                pass  # Diagnostic handlers must never prevent the safety Off.
            self._publish()
            self._schedule_check()

    def _ack_auxiliary_heartbeat_off(self):
        if not (self.auxiliary_heartbeat_blocked or self._auxiliary_heat_possible):
            return False
        isolated = bool(self.power_guard is not None and self.power_guard.power_cut_confirmed)
        acknowledged = bool(not self.auxiliary_inflight and (
            isolated or (self._auxiliary_heartbeat_off_receipt
                         and self._is_off(self.config.heater_entity)
                         and self._auxiliary_marker() != self._auxiliary_heartbeat_baseline)))
        if acknowledged and not self.auxiliary_heartbeat_blocked:
            self._auxiliary_heat_possible = False
        if acknowledged != self._auxiliary_heartbeat_off_ack:
            self._auxiliary_heartbeat_off_ack = acknowledged
            self._publish()
        return acknowledged

    @callback
    def auxiliary_activation_started(self, data):
        self.auxiliary_inflight += 1
        if data.get("hvac_mode") != "fan_only":
            # A completed service can still leave an old Off report cached.
            # Keep possible heat exposure until a confirmed superseding stop.
            self._auxiliary_heat_possible = True
        if self._auxiliary_heat_possible or self.auxiliary_heartbeat_blocked:
            self._auxiliary_stop_epoch += 1
            self._auxiliary_stop_pending = False
            self._auxiliary_heartbeat_off_receipt = False
            self._auxiliary_heartbeat_off_ack = False

    @callback
    def auxiliary_activation_finished(self):
        self.auxiliary_inflight -= 1
        if (self.auxiliary_heartbeat_blocked
                or (self._auxiliary_heat_possible and self._auxiliary_stop_pending)):
            # An earlier Off cannot supersede a positive service which only
            # completed later. Require another Off and a fresh report after it.
            self._auxiliary_stop_epoch += 1
            self._auxiliary_heartbeat_baseline = self._auxiliary_marker()
            self._auxiliary_heartbeat_off_receipt = False
            self._auxiliary_heartbeat_off_ack = False
            self._auxiliary_stop_pending = True
            self._publish()

    @callback
    def auxiliary_off_started(self):
        if (not self.config.heater_entity
                or not (self._auxiliary_heat_possible or self.auxiliary_heartbeat_blocked
                        or self.auxiliary_inflight or self._auxiliary_could_heat())):
            return
        self._auxiliary_heat_possible = True
        if not self._auxiliary_stop_pending:
            self._auxiliary_stop_epoch += 1
            self._auxiliary_heartbeat_baseline = self._auxiliary_marker()
            self._auxiliary_heartbeat_off_receipt = False
            self._auxiliary_heartbeat_off_ack = False
            self._auxiliary_stop_pending = True
        return (self.ownership_generation, self._auxiliary_stop_epoch)

    @callback
    def auxiliary_off_finished(self, receipt):
        # A stop dispatched before the last positive completion cannot prove
        # that the newly uncertain heater has been superseded.
        if (receipt is not None and self._auxiliary_stop_pending
                and receipt == (self.ownership_generation, self._auxiliary_stop_epoch)):
            self._auxiliary_heartbeat_off_receipt = True
            self._ack_auxiliary_heartbeat_off()
        elif receipt is not None and self._auxiliary_stop_pending:
            self._auxiliary_heartbeat_baseline = self._auxiliary_marker()
            self._auxiliary_heartbeat_off_receipt = False
            self._auxiliary_heartbeat_off_ack = False

    @callback
    def confirm_heartbeat(self, *, generation=None):
        """Renew only a still-valid ownership lease, on Home Assistant's loop."""
        if generation is not None and generation != self.ownership_generation:
            raise HAActivationDeferred("Heartbeat belongs to an earlier controller lease")
        if (not self.active or not self.live or not self.enabled or not self.owned
                or self.recovering or self._claiming or self._closing):
            raise HAActivationDeferred("Controller is disabled or stopping; heartbeat ignored")
        instant = time.monotonic()
        age = instant - self.heartbeat_monotonic
        if not 0 <= age <= self.config.heartbeat_timeout_seconds:
            self.pause(f"Control paused: controller heartbeat expired (age {age:.1f}s; lease limit {self.config.heartbeat_timeout_seconds:g}s)")
            raise HAActivationDeferred("Controller heartbeat lease expired")
        # Observe the shorter heater deadline before the shared lease is renewed.
        self._check_auxiliary_heartbeat_deadline(instant)
        if self._ack_auxiliary_heartbeat_off():
            self._reset_auxiliary_heartbeat()
        self.heartbeat = datetime.now(timezone.utc).timestamp()
        self.heartbeat_monotonic = instant
        self._publish()

    def check_activation(self, entity, data, *, generation=None):
        if generation is not None and generation != self.ownership_generation:
            raise HAActivationDeferred("Command belongs to an earlier controller lease")
        if (not self.active or not self.live or not self.enabled or self.recovering
                or self._claiming or self._closing
                or entity not in self.owned):
            raise HAError("Controller is disabled or stopping; activation blocked")
        if entity in self._uncertain_device_jobs:
            raise HAError("A prior device service has uncertain completion; activation blocked")
        instant = time.monotonic()
        age = instant - self.heartbeat_monotonic
        if not 0 <= age <= self.config.heartbeat_timeout_seconds:
            raise HAError("Controller heartbeat expired; activation blocked")
        self._check_auxiliary_heartbeat_deadline(instant)
        if entity == self.config.heater_entity and (
                self.auxiliary_heartbeat_blocked
                or not 0 <= age <= self.config.auxiliary_heartbeat_timeout_seconds):
            raise HAActivationDeferred(self.auxiliary_heartbeat_reason
                                      or "Waiting for a fresh auxiliary heater heartbeat")
        if (entity == self.config.heater_entity and data.get("hvac_mode") == "heat"
                and not self.probes_safe(self._probe_states())):
            raise HAError("External room probes block auxiliary heat")

    def check_stop(self, entity):
        if entity not in self.owned and entity not in self.pending_fans:
            raise HAActivationDeferred("Stop belongs to a device no longer owned by this controller")

    def _is_off(self, entity):
        if (entity == self.config.heater_entity and self.power_guard is not None
                and getattr(self.power_guard, "emergency_fallback_enabled", False) is True):
            return self.power_guard.shutdown_confirmed
        if (entity == self.config.heater_entity and self.power_guard is not None
                and self.power_guard.power_cut_confirmed):
            # The configured cutoff reports Off; the now-unpowered Dyson
            # integration cannot be required to produce another report.
            return True
        if entity in self._uncertain_device_jobs:
            return False
        if entity in self._head_possible:
            return self._ack_head_off(entity)
        state = self.hass.states.get(entity)
        return bool(state and state.state == "off"
                    and state.attributes.get("hvac_action") not in ("heating", "cooling"))

    async def _clear_if_off(self):
        async with self._lock:
            critical = tuple(entity for entity in self.owned if entity.startswith("climate."))
            auxiliary_safe = (not (self._auxiliary_heat_possible or self.auxiliary_heartbeat_blocked)
                              or self._ack_auxiliary_heartbeat_off())
            if (self.owned and not self.inflight and not self.service_inflight
                    and not self.auxiliary_inflight and auxiliary_safe
                    and all(self._is_off(entity) for entity in critical)):
                pending = tuple(dict.fromkeys([
                    *self.pending_fans,
                    *(entity for entity in self.owned
                      if entity.startswith("fan.") and not self._is_off(entity)),
                ]))
                # Persist the retry obligation before releasing critical ownership.
                await self._save((), pending)
                self.owned = ()
                self.ownership_generation += 1
                self._reset_auxiliary_heartbeat()
                self.pending_fans = pending
                self.recovering = False
                if self.problem and self.problem.startswith(
                    ("Could not stop climate.", "Could not save controller shutdown:")
                ):
                    self.problem = None
                self._publish()

    async def _clear_pending_fans(self):
        async with self._lock:
            pending = tuple(entity for entity in self.pending_fans if not self._is_off(entity))
            if pending != self.pending_fans:
                await self._save(self.owned, pending)
                for entity in self.pending_fans:
                    if entity not in pending:
                        self._fan_problems.pop(entity, None)
                self.pending_fans = pending
                self._publish()

    @callback
    def _schedule_check(self, _event=None):
        if (not self.active or self._claiming or self._closing
                or (self._task and not self._task.done())):
            return
        self._task = self.hass.async_create_task(self.async_check(), "Home Climate watchdog")

    async def _off(self, entity):
        # Watchdog stops have the same actual handler lifetime as worker stops.
        # A reported Off before the handler returns cannot release ownership.
        generation = self.ownership_generation
        repair_epoch = self.repair_gate.capture(entity, activation=False) if self.repair_gate else None
        self.service_inflight += 1
        task = self.hass.async_create_background_task(
            self._off_job(entity, generation, repair_epoch), "Home Climate watchdog device stop", eager_start=False)

        self._stop_jobs[task] = entity

        def finished(job):
            self._stop_jobs.pop(job, None)
            self.service_inflight -= 1
            if not job.cancelled():
                job.exception()
            else:
                self.activation_job_uncertain(entity)
            self._schedule_check()

        task.add_done_callback(finished)
        done, _pending = await asyncio.wait({task}, timeout=5)
        return bool(task.result()) if done else False

    def stop_jobs(self, entities):
        return tuple(task for task, entity in self._stop_jobs.items()
                     if entity in entities and not task.done())

    async def _off_job(self, entity, generation, repair_epoch):
        if generation != self.ownership_generation:
            return False
        try:
            self.check_stop(entity)
            if self.repair_gate:
                self.repair_gate.check(entity, repair_epoch, activation=False)
        except HAActivationDeferred:
            return False
        receipt = self.auxiliary_off_started() if entity == self.config.heater_entity else None
        head_receipt = self.head_off_started(entity)
        try:
            if (entity == self.config.heater_entity and self.power_guard is not None
                    and getattr(self.power_guard, "emergency_fallback_enabled", False) is True):
                if not await self.power_guard.async_request_heater_off():
                    raise HAError("Heater shutdown remains unconfirmed")
            else:
                await self.hass.services.async_call(
                    entity.split(".", 1)[0],
                    "set_hvac_mode" if entity.startswith("climate.") else "turn_off",
                    {"entity_id": entity, **({"hvac_mode": "off"} if entity.startswith("climate.") else {})},
                    blocking=True,
                )
            if entity == self.config.heater_entity:
                self.auxiliary_off_finished(receipt)
            if head_receipt is not None:
                self.head_off_finished(entity, head_receipt)
            return True
        except Exception as exc:
            message = f"Could not stop {entity}: {exc}"
            if entity.startswith("fan."):
                if self._fan_problems.get(entity) != message:
                    _LOGGER.warning("Home Climate watchdog: %s", message)
                self._fan_problems[entity] = message
            else:
                if self.problem != message:
                    log = _LOGGER.error if self.recovering or not self.enabled else _LOGGER.warning
                    log("Home Climate watchdog: %s", message)
                self.problem = message
                if entity == self.config.heater_entity:
                    self._auxiliary_off_error = message
            self._publish()
            return False

    async def async_check(self):
        """Run on HA's loop even when the control worker stalls or exits."""
        async with self._check_lock:
            if self._claiming:
                return
            heater = self.hass.states.get(self.config.heater_entity) if self.config.heater_entity else None
            safely_idle = bool(heater and heater.state in ("off", "fan_only")
                               and heater.attributes.get("hvac_action") != "heating")
            if self._auxiliary_off_error and (self._is_off(self.config.heater_entity)
                                              or (not self.recovering and safely_idle)):
                if self.problem == self._auxiliary_off_error:
                    self.problem = None
                    self._publish()
                self._auxiliary_off_error = None
            attempted = set()
            if self.owned:
                instant = time.monotonic()
                heartbeat_age = instant - self.heartbeat_monotonic
                expired = not 0 <= heartbeat_age <= self.config.heartbeat_timeout_seconds
                if not expired:
                    self._check_auxiliary_heartbeat_deadline(instant)
                if self.recovering or not self.enabled or expired:
                    if self.enabled and expired:
                        self._log_stop(
                            logging.WARNING,
                            f"controller heartbeat expired (age {heartbeat_age:.1f}s; "
                            f"lease limit {self.config.heartbeat_timeout_seconds:g}s); disabling control",
                        )
                        self.enabled = False
                        self.ownership_generation += 1
                        self.problem = "Control paused: controller heartbeat expired"
                    self.recovering = True
                    self._publish()
                    attempted = {entity for entity in self.owned
                                 if self.inflight or not self._is_off(entity)
                                 or (entity == self.config.heater_entity
                                     and (self._auxiliary_heat_possible
                                          or self.auxiliary_heartbeat_blocked))}
                    await asyncio.gather(*(self._off(entity) for entity in attempted))
                    try:
                        await self._clear_if_off()
                    except Exception as exc:
                        self.problem = f"Could not save controller shutdown: {exc}"
                        self._log_shutdown_save_error(self.problem)
                        self._publish()
                elif self.config.heater_entity in self.owned:
                    if self.auxiliary_heartbeat_blocked:
                        if not self._ack_auxiliary_heartbeat_off():
                            await self._off(self.config.heater_entity)
                            self._ack_auxiliary_heartbeat_off()
                    elif self._auxiliary_could_heat() and not self.probes_safe(self._probe_states()):
                        await self._off(self.config.heater_entity)
            # Cleanup from previous mappings is independent of the live lease.
            # Current fans are removed from this bucket by the saved claim above.
            if self.pending_fans:
                await asyncio.gather(*(self._off(entity) for entity in self.pending_fans
                                       if entity not in attempted and not self._is_off(entity)))
                try:
                    await self._clear_pending_fans()
                except Exception as exc:
                    self.enabled = False
                    self.recovering = bool(self.owned)
                    self.problem = f"Could not save controller shutdown: {exc}"
                    self._log_shutdown_save_error(self.problem)
                    self._publish()

    async def async_close(self):
        if (not self.active and self._closing and not self.owned
                and not self.inflight and not self.service_inflight):
            return True
        self._closing = True
        closed = False
        try:
            await self.async_set_enabled(False)
            if self._task:
                # Cancelling a pending configuration reload must not cancel the
                # independently scheduled watchdog it happens to be awaiting.
                await asyncio.shield(self._task)
            # The check lock also waits for a claim already saving ownership.
            await self.async_check()
            if self.owned or self.inflight or self.service_inflight:
                # Keep subscriptions alive for unresolved critical ownership.
                return False
            # Fan-only cleanup survives in storage and resumes on next load.
            self.active = False
            for unsubscribe in self._unsubscribers:
                unsubscribe()
            self._unsubscribers.clear()
            self._publish()
            closed = True
            return True
        finally:
            if not closed:
                self._closing = False
                self._schedule_check()
