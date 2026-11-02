# Notification routing and evidence

Private bindings may contain a `notifications` object. Keep real destinations
out of committed examples. A normal notify-entity route looks like this:

```json
{
  "notifications": {
    "normal": {
      "action": "notify.send_message",
      "target": {"entity_id": "notify.fixture_group"}
    }
  }
}
```

The entity ID selects the recipient; it is not a service name. Home Climate
supplies the event-specific message and title. A route cannot replace them with
an unrelated automation template. Missing or unavailable targets remain a
visible routing failure rather than being labelled successfully delivered.

Home Assistant 2026.10.4's `notify.send_message` accepts message and title, not a
provider-specific critical payload. This action cannot be marked
`critical_capable`. A critical route requires an independently verified
provider-specific notify action, its explicit payload, and
`critical_capable: true`. For example, the following is a configuration pattern,
not a claim that any particular installation has this service or phone setup:

```json
{
  "critical": {
    "action": "notify.mobile_app_fixture_phone",
    "data": {"data": {"push": {"sound": {"name": "default", "critical": 1}}}},
    "critical_capable": true
  }
}
```

Validate the provider action and device notification permissions before using
that declaration. Do not derive an action name from a notify entity ID. Other
providers and mobile platforms have different payload requirements.

Without a configured capable critical route, an urgent condition still creates
a persistent HA notice and sends an urgent-labelled message to the normal
route, if available. Diagnostics state that critical route capability is
unverified. This fallback does not claim to override silent mode or Do Not
Disturb. Even with a configured critical route, `service_accepted` means only
that HA accepted the service call; endpoint delivery remains `unknown`.

Alerts send promptly once per incident. Repeated polls update the local notice
without repeated external messages. A severity escalation or a resolved
condition that later recurs may send a new message. Only a known-unavailable
service or entity target is retried automatically, at most three checks five
minutes apart. A timeout or service exception can follow actual dispatch, so
unknown delivery is not automatically retried. Notification calls have a
five-second service deadline and do not delay actuator shutdown work.

Critical conditions include low battery even during auxiliary manual takeover,
unconfirmed failed shutdown, exhausted shared-head Off confirmation and failed
recovery handoff. Actionable device, controller and source problems use normal
notifications. Ordinary minimum-cycle, changeover and averaging waits remain
quiet. Required physical fault obligations are independent of notification
delivery success.

The controller status exposes active alerts, configured route capability,
service outcome and local-notice errors. Equipment diagnostics separately show
requested settings, HA-reported mode/action and electrical corroboration.
Neither a Heat-mode echo nor a successful notification service call proves
the corresponding physical outcome.
