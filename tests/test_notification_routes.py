"""Configuration keeps notify entities, actions and priority evidence distinct."""
import unittest

from house_climate.notifications import NotificationRoute, NotificationSettings


class NotificationRouteTests(unittest.TestCase):
    def test_entity_route_round_trip_and_runtime_message(self):
        raw = {"normal": {"action": "notify.send_message",
                          "target": {"entity_id": "notify.fixture_group"}}}
        settings = NotificationSettings.from_dict(raw)
        self.assertEqual(NotificationSettings.from_dict(settings.to_dict()), settings)
        self.assertEqual(settings.normal.service_data("Battery reserve reached", "Home Climate"), {
            "entity_id": ["notify.fixture_group"], "message": "Battery reserve reached", "title": "Home Climate"})

    def test_critical_payload_is_only_used_by_explicit_capable_provider_route(self):
        normal = NotificationRoute("notify.send_message", ("notify.fixture_group",))
        critical = NotificationRoute("notify.mobile_app_fixture", data={"data": {
            "push": {"sound": {"name": "default", "critical": 1}}}}, critical_capable=True)
        settings = NotificationSettings(normal, critical)
        self.assertEqual(settings.route_for("critical"), (critical, True))
        self.assertEqual(settings.route_for("normal"), (normal, False))
        unverified = NotificationSettings(normal, NotificationRoute("notify.fixture_urgent"))
        self.assertEqual(unverified.route_for("critical"), (normal, False))

    def test_send_message_cannot_masquerade_as_legacy_notify_payload(self):
        for raw in (
            {"action": "notify.send_message"},
            {"action": "notify.send_message", "target": {"entity_id": "notify.fixture"},
             "data": {"data": {"push": {"interruption-level": "critical"}}}},
            {"action": "notify.send_message", "target": {"entity_id": "notify.fixture"},
             "critical_capable": True},
            {"action": "notify.fixture", "critical_capable": True},
            {"action": "notify.fixture", "target": {"entity_id": "notify.fixture"}},
            {"action": "switch.turn_on", "target": {"entity_id": "switch.fixture"}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                NotificationRoute.from_dict(raw)

    def test_route_cannot_replace_event_text_or_target(self):
        for key in ("message", "title", "entity_id"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                NotificationRoute("notify.fixture", data={key: "unrelated"})

    def test_mutating_input_or_service_payload_cannot_modify_configuration(self):
        raw = {"action": "notify.fixture", "data": {"data": {"priority": "high"}}}
        route = NotificationRoute.from_dict(raw)
        raw["data"]["data"]["priority"] = "low"
        payload = route.service_data("message", "title")
        payload["data"]["priority"] = "changed"
        self.assertEqual(route.data["data"]["priority"], "high")

    def test_invalid_configuration_does_not_silently_drop_unknown_keys(self):
        invalid = [
            {"emergency": {}}, {"normal": {"action": "notify.fixture", "critical_capable": 1}},
            {"normal": {"action": "notify.fixture", "data": {"data": float("nan")}}},
            {"normal": {"action": "notify.send_message", "target": {"area_id": "fixture"}}},
            {"normal": {"action": "notify.send_message", "target": {
                "entity_id": ["notify.fixture", "notify.fixture"]}}},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                NotificationSettings.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
