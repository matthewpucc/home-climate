"""Narrow hass-dyson adapter: one bound purifier, existing native dispatch fences."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
import time

from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntryState

from .configuration import purifier_identity_issue
from .controller.airflow_execution import at_minimum, matches_profile
from .controller.config import Settings
from .controller.ha import HAActivationDeferred, HAError
from .controller.policy import Policy


@dataclass(frozen=True)
class AirflowAuthorization:
    entity: str
    name: str
    mode: str
    identity: tuple
    calls: tuple
    targets: tuple
    percentage: int
    deadline: float


def identity(hass, entity):
    entry = er.async_get(hass).async_get(entity)
    if entry is None or not entry.device_id:
        return ()
    owner = hass.config_entries.async_get_entry(entry.config_entry_id)
    # An unloaded or reloaded owning integration invalidates prior evidence.
    component = hass.data.get("fan")
    native_entity = component.get_entity(entity) if hasattr(component, "get_entity") else None
    return (entry.device_id, entry.config_entry_id, entry.unique_id, entry.platform,
            id(owner), str(owner.state) if owner else None, id(native_entity))


def target_signature(plan):
    return tuple(sorted((name, tuple(target.get(key) for key in
        ("low_f", "high_f", "target_f", "comfort_enabled", "priority")))
        for name, target in plan.room_targets.items()))


def authorize(api, entity, plan, states, now):
    if api.controls is None:
        raise HAError("Directional control requires native ownership controls")
    fan = next((f for f in api.config.fan_controls.values() if f.entity_id == entity), None)
    intent = plan.fan_airflow.get(entity, {})
    if fan is None or fan.airflow is None or intent.get("profile") not in fan.airflow.profiles:
        raise HAActivationDeferred("No current configured airflow intent")
    state = states.get(entity, {})
    bound = tuple(state.get("airflow_identity") or ())
    if not bound or len(bound) < 4 or bound[3] != "hass_dyson":
        raise HAError("Bind an independently registered hass_dyson purifier with angle feedback")
    check_report(api, state)
    calls = tuple((name, mode) for name, target in plan.room_targets.items()
                  for mode in ("heat", "cool") if target.get(f"{mode}_requested"))
    ticket = AirflowAuthorization(entity, intent["profile"], intent["mode"], bound, calls,
                                 target_signature(plan), plan.fans[entity], now + min(30, api.config.poll_seconds))
    api._airflow_authorizations[entity] = ticket
    return ticket


def check_report(api, state):
    """Required even when already at speed and no new command will be sent."""
    from .controller.airflow import TransferAirflow
    now = datetime.now(timezone.utc)
    stamp = TransferAirflow._reported_at(state, now)
    attrs = state.get("attributes", {})
    if attrs.get("restored") or attrs.get("assumed_state") or state.get("restored"):
        raise HAError("Purifier feedback is restored/assumed; wait for an actual device report")
    if stamp is None or (now - stamp).total_seconds() > api.config.sensor_stale_seconds:
        raise HAError("Purifier settings report is stale; refresh device telemetry")
    features = state.get("attributes", {}).get("supported_features")
    if (type(features) is not int or features & 7 != 7
            or not api.hass.services.has_service("hass_dyson", "set_oscillation_angles")
            or not api.hass.services.has_service("fan", "set_direction")):
        raise HAError("Purifier needs speed, outlet direction and atomic hass_dyson angle services")


def validate_dispatch(api, ticket, service, body):
    """Run on HA's loop immediately before the actual device operation."""
    if (not isinstance(ticket, AirflowAuthorization)
            or api._airflow_authorizations.get(ticket.entity) is not ticket
            or time.monotonic() >= ticket.deadline or body.get("entity_id") != ticket.entity):
        raise HAActivationDeferred("Airflow intent expired or was superseded")
    fan = next((f for f in api.config.fan_controls.values() if f.entity_id == ticket.entity), None)
    if fan is None or fan.airflow is None or ticket.name not in fan.airflow.profiles:
        raise HAActivationDeferred("Airflow profile is no longer configured")
    if identity(api.hass, ticket.entity) != ticket.identity:
        raise HAActivationDeferred("Purifier physical identity or integration lifecycle changed")
    registry = er.async_get(api.hass)
    entry = registry.async_get(ticket.entity)
    owner = api.hass.config_entries.async_get_entry(entry.config_entry_id)
    if owner is None or owner.state != ConfigEntryState.LOADED or owner.domain != "hass_dyson":
        raise HAActivationDeferred("Purifier owning integration is not loaded")
    if any(other != ticket.entity and (alias := registry.async_get(other))
           and alias.device_id == ticket.identity[0] for other in api.config.fans.values()):
        raise HAError("Multiple purifier bindings identify the same device; keep one explicit fan binding")
    if issue := purifier_identity_issue(api.hass, api.config, ticket.entity,
                                        require_identity=True, controls=api.controls):
        raise HAError(issue)
    profile = fan.airflow.profiles[ticket.name]
    states = {state["entity_id"]: state for state in api._read_states()}
    state = states.get(ticket.entity, {})
    attrs = state.get("attributes", {})
    check_report(api, state)
    now = datetime.now(timezone.utc)
    config = replace(api.config, timezone=api.hass.config.time_zone)
    settings = (api.settings_provider(states, config, now) if api.settings_provider
                else Settings.read(states, config))
    policy = Policy(config)
    policy.calls.update({call: True for call in ticket.calls})
    policy.directed_airflow.selected[ticket.entity] = ticket.name
    if ticket.percentage > fan.minimum_percentage:
        policy.directed_airflow.boosting[ticket.entity] = ticket.name
    current = policy.decide(states, now, settings, api.hass.config.units.temperature_unit)
    intent = current.fan_airflow.get(ticket.entity, {})
    if (intent.get("profile") != ticket.name or intent.get("mode") != ticket.mode
            or target_signature(current) != ticket.targets):
        raise HAActivationDeferred("Fresh room demand/source output no longer supports this airflow intent")
    if service in ("set_direction", "set_airflow_profile"):
        if not at_minimum(state, fan.minimum_percentage):
            raise HAActivationDeferred("Reduce purifier to its minimum before changing aim")
        if service == "set_direction":
            if set(body) != {"entity_id", "direction"} or body["direction"] != profile.direction:
                raise HAError("Outlet request differs from the configured profile")
            return "fan", service, body
        if (set(body) != {"entity_id", "lower_angle", "upper_angle"}
                or body["lower_angle"] != profile.lower_angle or body["upper_angle"] != profile.upper_angle
                or attrs.get("direction") != profile.direction):
            raise HAError("Sweep request/outlet differs from the configured profile")
        return "hass_dyson", "set_oscillation_angles", {
            "device_id": ticket.identity[0], "lower_angle": profile.lower_angle,
            "upper_angle": profile.upper_angle}
    if (service not in ("turn_on", "set_percentage")
            or set(body) != {"entity_id", "percentage"}
            or not matches_profile(state, profile)):
        raise HAActivationDeferred("Directional boost requires matching reported sweep, oscillation and outlet")
    # A profile can remain useful for comfort while losing its transfer basis.
    # Recheck the speed entitlement too; matching angles alone is insufficient.
    step = float(attrs.get("percentage_step", 1))
    if not math.isfinite(step) or not 0 < step <= 100:
        raise HAError("Invalid fan percentage step")
    permitted = min(ticket.percentage, current.fans.get(ticket.entity, 0))
    maximum = min(100, math.floor(max(1, round(permitted / step),
        math.ceil((math.ceil(fan.minimum_percentage) - 1e-7) / step)) * step + 1e-7))
    if not intent.get("boost") or type(body["percentage"]) not in (int, float) or not 0 < body["percentage"] <= maximum:
        raise HAActivationDeferred("Fresh airflow intent no longer permits this boost")
    return "fan", service, body
