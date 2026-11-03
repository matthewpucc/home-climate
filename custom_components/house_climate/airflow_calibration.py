"""Private durable physical identity pins for explicitly accepted fixed outlets."""
from __future__ import annotations

from homeassistant.helpers.storage import Store

from .const import DOMAIN


async def async_load_calibrations(api, entry_id):
    """Bind an explicit verified calibration once; never silently move it.

    Calibration keys are operator declarations, not inferred hardware abilities.
    Stored physical identifiers remain in HA's private storage, outside exports
    and public examples. A new physical calibration needs a new explicit key.
    """
    from .airflow_adapter import identity
    selected = [fan for fan in api.config.fan_controls.values()
                if fan.airflow is not None and fan.airflow.outlet == "calibrated_fixed"]
    api._airflow_calibration_error = None
    if not selected:
        return
    store = Store(api.hass, 1, f"{DOMAIN}.airflow_calibration.{entry_id}")
    ledger = Store(api.hass, 1, f"{DOMAIN}.airflow_commissioned.{entry_id}")
    try:
        saved = await store.async_load()
        commissioned = await ledger.async_load()
        if saved is None:
            saved = {"bindings": {}}
        if commissioned is None:
            commissioned = {"keys": []}
        if not isinstance(saved, dict) or not isinstance(saved.get("bindings"), dict):
            raise ValueError("invalid calibration storage")
        if (not isinstance(commissioned, dict) or not isinstance(commissioned.get("keys"), list)
                or not all(isinstance(key, str) and key for key in commissioned["keys"])):
            raise ValueError("invalid commissioning ledger")
        keys = set(commissioned["keys"])
        pins = dict(saved["bindings"])
        if any(not isinstance(key, str) or not isinstance(value, list) or len(value) != 4
               or not all(isinstance(part, str) and part for part in value)
               for key, value in pins.items()):
            raise ValueError("invalid calibration identity")
        if set(pins) != keys:
            raise ValueError("missing or inconsistent calibration records")
        changed = False
        for fan in selected:
            key = fan.airflow.calibration_key
            if key in pins:
                continue
            bound = identity(api.hass, fan.entity_id)
            if len(bound) >= 4 and bound[3] == "hass_dyson" and all(bound[:4]):
                pins[key] = list(bound[:4])
                changed = True
        if changed:
            # Write the independent marker first. If the pin save fails or a
            # single storage file is lost later, this key cannot be reused as
            # a first-time declaration for a different physical device.
            await ledger.async_save({"keys": sorted(pins)})
            await store.async_save({"bindings": pins})
        api._fixed_airflow_bindings = {key: tuple(value) for key, value in pins.items()}
    except Exception:
        # Missing/corrupt/unwritable storage cannot silently recommission a
        # replacement. Other equipment and ordinary purification remain usable.
        api._fixed_airflow_bindings = {}
        api._airflow_calibration_error = "Fixed-outlet calibration storage unavailable; restore it before directional control"
