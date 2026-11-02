"""Read-only adapters for actual physical feedback; never rewrite HA state."""
import asyncio


async def async_read_matter_switch(hass, entity_id):
    """Read an actual Matter OnOff attribute, including an unchanged value.

    HA 2026.10's public Matter client read_attribute returns the response even
    when the server suppresses an unchanged attribute event. This deliberately
    does not use update_entity, switch.turn_on, a re-interview or cached data.
    Unsupported switch implementations remain unknown.
    """
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    component = hass.data.get(DATA_INSTANCES, {}).get("switch")
    entity = component.get_entity(entity_id) if component else None
    if entity is None or type(entity).__module__ != "homeassistant.components.matter.switch":
        return None
    info = getattr(entity, "_entity_info", None)
    attribute = getattr(info, "primary_attribute", None)
    if (getattr(attribute, "cluster_id", None), getattr(attribute, "attribute_id", None)) != (6, 0):
        return None
    endpoint = entity._endpoint
    path = f"{endpoint.endpoint_id}/6/0"
    async with asyncio.timeout(5):
        response = await entity.matter_client.read_attribute(endpoint.node.node_id, path)
    if component.get_entity(entity_id) is not entity or not entity.available:
        return None
    value = response.get(path) if isinstance(response, dict) else None
    return ("on" if value else "off") if type(value) is bool else None
