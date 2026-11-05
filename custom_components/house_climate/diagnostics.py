"""Download bounded observations without exporting configuration or live inputs."""


async def async_get_config_entry_diagnostics(hass, entry):
    controls = getattr(entry.runtime_data, "controls", None)
    guard = getattr(entry.runtime_data, "power_guard", None)
    bluetooth = getattr(getattr(entry.runtime_data, "api", None), "bluetooth_freshness", None)
    return {"bluetooth_packet_freshness": dict(bluetooth.counts) if bluetooth else {},
            "auxiliary_observations": controls.observations.download() if controls else None,
            "shutdown_observations": (guard.shutdown_observations.download()
                if guard is not None and hasattr(guard, "shutdown_observations") else None)}
