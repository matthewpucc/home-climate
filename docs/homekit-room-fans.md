# Apple Home room fan controls

Home Climate provides two optional virtual fan controls for each room with a
configured circulation fan or purifier. They represent **policy requests**, not
the device's measured speed or an actuator. They don't control the heat-pump
blower or auxiliary heater. The existing minimum and speed numbers remain
available in Home Assistant and use the same saved settings.

| Apple Home control | Minimum fan speed | Exact fan speed |
| --- | --- | --- |
| Auto | Return to the configured baseline and normal schedule. | Clear the fixed speed and return to Home Climate's normal decisions, including the current minimum. |
| Manual / slider | Set the lowest speed automatic policy can request. Ordinary demand or a schedule can still request more. | Ask for a fixed speed, taking precedence over the minimum and ordinary demand, subject to existing device and safety checks. |
| Off | Set a manual 0% floor. It doesn't ask the physical fan to stop. | Set a manual 0% speed request. It isn't an emergency stop or a device power switch. |

Changing a slider selects manual control. Auto and Off are different: only
Auto hands the setting back. Switching one control to Auto doesn't reset the
other, and neither one changes temperature Hold. An Auto tile may be active
while the physical fan is idle. Use the underlying fan's status if you need
its actual operating state. Device quantization, direction rules, ownership
and ordinary recovery behavior still apply.

Switching back to Manual without a slider change uses the last nonzero
manual percentage when one is saved. With no saved value, it uses a positive
displayed percentage if available so the value need not jump.

## Add one room first

1. In Home Assistant, open **Settings → Devices & services → Entities**, find
   **<Room> purifier minimum** and **<Room> purifier exact speed** for one room,
   and enable the ones you want. They start disabled to avoid adding
   accessories to a bridge on upgrade.
2. In the existing HomeKit Bridge, include only those new fan entities for
   the pilot. Use HomeKit accessory type **Fan**, not **Air Purifier**; the
   native Fan service uses the single Auto preset for its Auto/Manual control.
   Give the minimum and exact speed distinct names, especially for Siri. Keep
   your existing bridge and the underlying device integrations in place.
3. Prefer a supervised idle or Preview window. Test separate Auto, Manual,
   slider and Off writes in Apple Home. Check that both settings agree in HA,
   then verify that Auto restores the intended policy and that the other
   rooms and temperature controls are unchanged. Preview saves policy intent
   without issuing normal equipment commands.
4. Expand only after the pilot. To roll back the Apple Home surface, return
   each pilot control to its intended policy in HA, then remove its exposure
   from the bridge and disable the optional entities if desired. Removing a
   tile alone does not clear a saved override.

Tests exercise the Home Assistant HomeKit bridge class and its Fan
characteristics. They do not pair a device or verify the current Apple Home
app, Siri, or physical fan response.
