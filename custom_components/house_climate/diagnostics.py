"""Download bounded observations without exporting configuration or live inputs."""


async def async_get_config_entry_diagnostics(hass, entry):
    controls = getattr(entry.runtime_data, "controls", None)
    return {"auxiliary_observations": controls.observations.download() if controls else None}
