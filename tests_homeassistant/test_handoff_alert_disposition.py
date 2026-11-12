import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
from custom_components.house_climate.handoff import notification_disposition

class HandoffAlertDispositionTests(unittest.TestCase):
    def fixture(self):
        return SimpleNamespace(handoff={
            'stage': 'applying', 'heads': ['climate.fixture'], 'fans': [],
            'problems': {'climate.fixture': 'first failure'},
            'requests': {'climate.fixture': {'data': {}, 'at': 100, 'first_at': 100,
                'dispatched': True, 'dispatched_at': 100, 'finished_at': 101, 'attempts': 1}}},
            _uncertain_device_jobs={}, _handoff_jobs={}, hass=Mock(), config=SimpleNamespace(head_activation_timeout_seconds=120))

    def test_retry_predicate_is_authoritative_and_targets_must_match(self):
        c = self.fixture()
        c.hass.states.get.return_value.last_reported.timestamp.return_value = 102
        with patch('custom_components.house_climate.handoff.now', return_value=105), \
             patch('custom_components.house_climate.handoff._desired', return_value=('climate', 'set_temperature', {})), \
             patch('custom_components.house_climate.handoff._retry_evidence', return_value=True) as retry:
            self.assertEqual(notification_disposition(c)['climate.fixture'], 'recovering')
            self.assertEqual(retry.call_args.kwargs['current'], 116)
            retry.return_value = False
            self.assertEqual(notification_disposition(c)['climate.fixture'], 'human_required')
        with patch('custom_components.house_climate.handoff._desired', return_value=('climate', 'set_temperature', {'changed': True})), \
             patch('custom_components.house_climate.handoff._retry_evidence', return_value=True):
            self.assertEqual(notification_disposition(c)['climate.fixture'], 'human_required')

    def test_interrupted_and_unfinished_do_not_claim_retryable(self):
        for case in ('interrupted', 'unfinished', 'invalid'):
            c = self.fixture()
            if case == 'interrupted':
                c.handoff['interrupted'] = True
            elif case == 'unfinished':
                c.handoff['requests']['climate.fixture']['finished_at'] = None
            else:
                c.handoff['requests']['climate.fixture']['at'] = 'invalid'
            with self.subTest(case=case):
                self.assertEqual(notification_disposition(c)['climate.fixture'], 'human_required')
