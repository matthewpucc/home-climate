"""Download bounded observations without exporting configuration or live inputs."""

from .controller.diagnostics import HEAD_TRANSITION_LIMIT, bounded_head_history


async def async_get_config_entry_diagnostics(hass, entry):
    controls = getattr(entry.runtime_data, "controls", None)
    guard = getattr(entry.runtime_data, "power_guard", None)
    bluetooth = getattr(getattr(entry.runtime_data, "api", None), "bluetooth_freshness", None)
    runtime = entry.runtime_data
    heads = getattr(getattr(runtime, "config", None), "heads", {})
    history = getattr(runtime, "thermal_storage", None)
    return {"head_fan_trial": getattr(runtime, "status", {}).get("head_fan_trial"),
            "thermal_storage": history.diagnostics if history else {"status": "unavailable"},
            "head_transitions": {
                "history_limit_per_head": HEAD_TRANSITION_LIMIT,
                "persistence": "process_local",
                "recent_events": bounded_head_history(getattr(runtime, "status", {}), heads.values())},
            "bluetooth_packet_freshness": dict(bluetooth.counts) if bluetooth else {},
            "auxiliary_observations": controls.observations.download() if controls else None,
            "shutdown_observations": (guard.shutdown_observations.download()
                if guard is not None and hasattr(guard, "shutdown_observations") else None)}
