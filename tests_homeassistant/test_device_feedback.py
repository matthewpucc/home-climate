"""Matter recovery reads return physical replies without commands/cache writes."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.helpers.entity_component import DATA_INSTANCES
from custom_components.house_climate.device_feedback import async_read_matter_switch


class PhysicalFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        cls = type('FixtureMatterSwitch', (), {'__module__': 'homeassistant.components.matter.switch'})
        entity = cls()
        entity._entity_info = SimpleNamespace(primary_attribute=SimpleNamespace(cluster_id=6, attribute_id=0))
        entity._endpoint = SimpleNamespace(endpoint_id=2, node=SimpleNamespace(node_id=9))
        entity.available = True
        entity.matter_client = SimpleNamespace(read_attribute=AsyncMock(return_value={'2/6/0': True}))
        component = SimpleNamespace(get_entity=Mock(return_value=entity))
        hass = SimpleNamespace(data={DATA_INSTANCES: {'switch': component}},
                               services=Mock(), states=Mock())
        return hass, entity, component

    async def test_read_unchanged_on_uses_actual_attribute_response_only(self):
        hass, entity, _ = self.fixture()
        self.assertEqual(await async_read_matter_switch(hass, 'switch.synthetic'), 'on')
        entity.matter_client.read_attribute.assert_awaited_once_with(9, '2/6/0')
        hass.services.assert_not_called()
        hass.states.assert_not_called()
        self.assertEqual(hass.services.mock_calls, [])
        self.assertEqual(hass.states.mock_calls, [])

    async def test_off_is_valid_but_wrong_paths_and_non_boolean_replies_are_unknown(self):
        hass, entity, _ = self.fixture()
        for response, expected in (({'2/6/0': False}, 'off'), ({'1/6/0': True}, None),
                                   ({'2/6/0': 1}, None), ({'2/6/0': 'true'}, None), (None, None)):
            entity.matter_client.read_attribute.return_value = response
            self.assertEqual(await async_read_matter_switch(hass, 'switch.synthetic'), expected)

    async def test_unsupported_source_and_wrong_attribute_are_not_polled(self):
        hass, entity, component = self.fixture()
        component.get_entity.return_value = SimpleNamespace()
        self.assertIsNone(await async_read_matter_switch(hass, 'switch.synthetic'))
        component.get_entity.return_value = entity
        entity._entity_info.primary_attribute.cluster_id = 8
        self.assertIsNone(await async_read_matter_switch(hass, 'switch.synthetic'))
        entity.matter_client.read_attribute.assert_not_awaited()

    async def test_entity_replacement_or_disconnect_during_read_rejects_reply(self):
        hass, entity, component = self.fixture()
        async def replace(*args):
            component.get_entity.return_value = SimpleNamespace()
            return {'2/6/0': True}
        entity.matter_client.read_attribute.side_effect = replace
        self.assertIsNone(await async_read_matter_switch(hass, 'switch.synthetic'))
        component.get_entity.return_value = entity
        async def disconnect(*args):
            entity.available = False
            return {'2/6/0': True}
        entity.matter_client.read_attribute.side_effect = disconnect
        self.assertIsNone(await async_read_matter_switch(hass, 'switch.synthetic'))

    async def test_read_failure_does_not_fall_back_to_state_or_command(self):
        hass, entity, _ = self.fixture()
        entity.matter_client.read_attribute.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await async_read_matter_switch(hass, 'switch.synthetic')
        self.assertEqual(hass.services.mock_calls, [])
        self.assertEqual(hass.states.mock_calls, [])
