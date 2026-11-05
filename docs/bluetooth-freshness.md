# Govee H5100 packet freshness

Home Climate can use newly received, valid H5100 temperature packets to keep a
stable room temperature eligible for control. Home Assistant can suppress
unchanged Govee entity writes, so the entity's `last_reported` can age even while
the sensor communicates. This native-only adapter leaves HA state and timestamps
unchanged. It does not add any source to a room or enable control.

## Evidence and supported path

The implementation was checked against Home Assistant 2026.11.0, its pinned
`habluetooth==7.1.2`, `bleak-esphome==4.0.0`, and `govee-ble==1.4.0`:

- HA's [`async_register_advertisement_callback`](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/bluetooth/api.py)
  delivers unchanged advertisements for one address. Registration does not replay history.
- [`habluetooth`](https://github.com/Bluetooth-Devices/habluetooth/blob/v7.1.2/src/habluetooth/manager.py)
  delivers these callbacks before source selection and history updates. Only `raw`
  describes the current packet. Manufacturer data, service data, UUIDs and names
  can be merged with older packets. A regression test uses the real remote
  scanner to demonstrate that a flags-only packet still has old manufacturer
  data; that packet must not renew temperature freshness.
- [`bleak-esphome`](https://github.com/Bluetooth-Devices/bleak-esphome/blob/v4.0.0/src/bleak_esphome/connect.py)
  selects raw delivery only when the proxy advertises `RAW_ADVERTISEMENTS`.
  The raw path preserves packet bytes and timestamps them at host receipt.
  The legacy proxy path and ordinary Bleak scanners may provide no raw bytes.
- The known H5100 manufacturer frame is company `0x0001`, a six-byte payload
  beginning `01 01`, packed signed temperature/humidity, and battery/error byte.
  Its decoding follows [`govee-ble`](https://github.com/Bluetooth-Devices/govee-ble/blob/v1.4.0/src/govee_ble/parser.py).
  Other formats are deliberately unsupported.

This proves the software path can supply packet evidence. It does **not** establish
that a particular installed proxy supplies raw packets. No live Home Assistant
or proxy configuration was changed or inspected with this change. After an
approved installation, download Home Climate diagnostics: the bounded
`bluetooth_packet_freshness` counters distinguish `accepted`, `raw_unavailable`,
`no_temperature_in_packet`, malformed/unsupported data and replay rejection.
An empty counter map means no supported subscription was started. A configured
source with no accepted raw packets continues to use ordinary HA freshness.

## Acceptance rules

Only configured room probes owned by the loaded `govee_ble` integration qualify.
The device registry must identify an H5100, and the temperature entity unique ID,
config-entry address and Bluetooth device connection must agree. Names and RSSI
are not identity or freshness evidence. Identity is checked again at receipt and
when a worker snapshot is built.

The adapter parses the complete raw AD structures and accepts only an
unambiguous supported temperature frame with no error flag, a valid battery
range and a temperature within −40–60°C. No merged field is used to fill missing
packet data. The decoded temperature must match the current, available HA entity
value within display rounding (0.11°F), with Celsius/Fahrenheit conversion.
Restored, assumed, missing and mismatching values cannot use the receipt.

The scanner timestamp must be after subscription, strictly newer than the last
accepted receipt, finite, not in the future and at most five seconds old at the
callback. The stored receipt expires at the configured probe stale interval,
checked against both monotonic time in the native bridge and wall time in the
controller. Invalid packets do not renew the last valid receipt. There is no
persisted receipt or startup history replay; restarting/unloading Home Climate
clears the evidence and unregisters callbacks. Existing acquisition-time and
required-circulation conditions still apply.

## What receipt does not prove

`packet_received_at` is host packet receipt time, not physical measurement time.
It is also exposed as a separate numeric [source metric](metrics.md); the
qualification metric expires when both report and packet evidence are old.
The normal `reported_at` stays the original HA report time. When only packet
receipt keeps an old value fresh, `timestamp_kind` is
`bluetooth_packet_receipt`, `measurement_at` and `observed_at` remain unknown,
and it cannot establish independent temperature progress or authorize a
progress-dependent auxiliary decision. Repeated identical advertisements never
become a series of physical measurements.

The H5100 frame has no trusted measurement timestamp or sequence counter. A
remote firmware replay given a new host receipt time, or an over-the-air replay,
cannot be distinguished from a newly received identical reading. This feature
rejects known cached/history replay and merged-field reuse; it does not claim
sensor acquisition freshness or authenticated radio evidence.

The independent auxiliary watchdog and electrical power guards keep their
existing inputs and freshness requirements. Their HA timestamp checks are not
relaxed by this adapter. CLI/add-on REST clients receive no native packet
evidence and keep their existing behavior. A newly added or rebound source needs
the normal Home Climate configuration reload before it is subscribed.
