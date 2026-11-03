"""Read-only provider evidence for recovery; never refresh or repair a device."""
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES


def mode_issue(hass, entity_id):
    """Supplement advertised HA modes when a provider has a separate mode gate.

    Kumo caches hvac_modes at construction, while pykumo.set_mode consults its
    current profile and silently rejects Auto when hasModeAuto is absent/false.
    Read that same cached gate, without invoking provider methods or interpreting
    an unattributed log warning as a rejection for a particular entity.
    """
    registry = hass.data.get(er.DATA_REGISTRY)
    entry = registry.async_get(entity_id) if registry is not None else None
    component = hass.data.get(DATA_INSTANCES, {}).get("climate")
    entity = component.get_entity(entity_id) if component else None
    kumo_entity = entity is not None and type(entity).__module__ == "custom_components.kumo.climate"
    if not kumo_entity and (entry is None or entry.platform != "kumo"):
        return None
    profile = getattr(getattr(entity, "_pykumo", None), "_profile", None) if kumo_entity else None
    if not isinstance(profile, dict) or not getattr(entity, "available", False):
        return "Recovery Auto capability is unverified in the current Kumo provider; inspect the integration"
    supported = profile.get("hasModeAuto", False)
    if supported is False:
        return "Recovery Auto is rejected by the current Kumo profile; inspect the integration before retrying"
    if supported is not True:
        return "Recovery Auto capability is unverified in the current Kumo profile; inspect the integration"
    return None
