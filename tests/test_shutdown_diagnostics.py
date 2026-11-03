"""Pure observations preserve unknowns and bounded history without actuator I/O."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

from house_climate.shutdown_diagnostics import ShutdownDiagnostics, HISTORY_LIMIT


class ShutdownDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.d = ShutdownDiagnostics()
        self.now = datetime.now(timezone.utc)
        self.b = SimpleNamespace(heater='climate.synthetic', heater_switch='switch.synthetic',
                                 emergency_ac_switch='switch.synthetic_ac')
        self.g = SimpleNamespace(_wall_clock=lambda: self.now.timestamp(),
            _last_observation_wall=self.now.timestamp(), _off_request_wall=None,
            _cut_request_wall=None, _emergency_ac_request_wall=None)

    def event(self, entities, *, user=None):
        return SimpleNamespace(data={'domain': 'switch', 'service': 'turn_off',
            'service_data': {'entity_id': entities}}, time_fired=self.now - timedelta(seconds=1),
            context=SimpleNamespace(user_id=user, id='private-context'))

    def test_unknown_external_actor_is_not_inferred_from_user_context(self):
        self.d.ha_request(self.event(['switch.synthetic', 'switch.unrelated'], user='shared-token-user'), self.b)
        row = self.d.history[-1]
        self.assertEqual(row['initiator'], 'unknown')
        self.assertIsNone(row['reason'])
        self.assertEqual(row['outcome'], 'unknown')
        self.assertEqual(row['emergency_intent'], 'unknown')
        self.assertTrue(row['user_context_present'])
        text = json.dumps(self.d.download())
        self.assertNotIn('shared-token-user', text)
        self.assertNotIn('private-context', text)
        self.assertNotIn('switch.synthetic', text)
        self.assertEqual(self.d.counts['ha_off_requested'], 1)

    def test_external_report_is_fresh_temporal_evidence_not_service_completion(self):
        self.d.ha_request(self.event('switch.synthetic'), self.b)
        state = SimpleNamespace(state='off', attributes={}, last_reported=self.now,
                                context=SimpleNamespace(id='different-context'))
        event = SimpleNamespace(data={'entity_id': 'switch.synthetic', 'new_state': state,
                                      'old_state': SimpleNamespace(state='off')})
        state.attributes = {'restored': True}
        self.d.ha_state(event, self.b)
        self.assertEqual(self.d.counts['ha_off_reported'], 0)
        state.attributes = {}
        self.d.ha_state(event, self.b)
        self.d.ha_state(event, self.b)
        self.assertEqual(self.d.counts['ha_off_reported'], 1)
        self.assertFalse(self.d.history[-1]['context_matches_request'])
        self.assertEqual(self.d.history[-1]['service_completion'], 'unknown')
        self.assertEqual(self.d.history[-1]['causality'], 'not_established')
        self.assertEqual(self.d.counts['guard_completed'], 0)

    def test_unmatched_off_and_expired_requests_never_invent_command_attribution(self):
        request = self.event('switch.synthetic')
        request.time_fired = self.now - timedelta(days=1)
        self.d.ha_request(request, self.b)
        state = SimpleNamespace(state='off', attributes={}, last_reported=self.now,
                                context=SimpleNamespace(id='private-context'))
        event = SimpleNamespace(data={'entity_id': 'switch.synthetic', 'new_state': state,
                                      'old_state': SimpleNamespace(state='on')})
        self.d.ha_state(event, self.b)
        row = self.d.history[-1]
        self.assertIsNone(row['request'])
        self.assertIsNone(row['request_at'])
        self.assertIsNone(row['context_matches_request'])
        self.assertEqual(row['initiator'], 'unknown')
        self.assertEqual(self.d._ha_requests, {})

    def test_qualified_aggregate_drop_is_separate_from_missing_dedicated_meter(self):
        current = {'power_cut_confirmed': True, 'electrical_stop_confirmed': False,
                   'shutdown_evidence': 'corroborated_off', 'shutdown_confirmation': 'emergency_ac_off',
                   'aggregate_stop': {'status': 'corroborated', 'classification': 'drop',
                                      'baseline_w': 1400, 'latest_w': 100}}
        self.d.capture(self.g, current)
        values, _ = self.d.values(self.now.timestamp())
        self.assertIsNone(values['shutdown_dedicated_load_w'])
        self.assertIsNone(values['shutdown_electrical_stop_confirmed'])
        self.assertEqual(values['shutdown_aggregate_drop_w'], 1300)
        self.assertEqual(values['shutdown_isolation_confirmed'], 1)
        current['aggregate_stop']['status'] = 'inconclusive'
        self.d.capture(self.g, current)
        self.assertIsNone(self.d.values(self.now.timestamp())[0]['shutdown_aggregate_drop_w'])

    def test_stale_snapshot_and_stale_reading_become_unknown(self):
        current = {'heater_power_w': 4, 'electrical_stop_confirmed': True, 'power_cut_confirmed': True}
        self.d.capture(self.g, current)
        self.assertEqual(self.d.values(self.now.timestamp())[0]['shutdown_dedicated_load_w'], 4)
        self.assertIsNone(self.d.values(self.now.timestamp() + 31)[0]['shutdown_dedicated_load_w'])
        self.g._last_observation_wall -= 31
        self.d.capture(self.g, current)
        self.assertIsNone(self.d.values(self.now.timestamp())[0]['shutdown_dedicated_load_w'])

    def test_aggregate_classification_selects_actual_drop_baseline(self):
        for outcome, expected in (
            ({'classification': 'already_low', 'baseline_w': 10, 'latest_w': 20}, None),
            ({'classification': 'relative_drop', 'baseline_w': 1400,
              'qualified_baseline_w': 1000, 'latest_w': 100}, 900),
            ({'classification': 'relative_drop', 'baseline_w': 1400, 'latest_w': 100}, None),
            ({'classification': 'drop', 'baseline_w': 1400, 'latest_w': 100}, 1300),
        ):
            with self.subTest(outcome=outcome):
                self.d.capture(self.g, {'aggregate_stop': {'status': 'corroborated', **outcome}})
                self.assertEqual(self.d.values(self.now.timestamp())[0]['shutdown_aggregate_drop_w'], expected)

    def test_history_reason_and_request_tracking_are_bounded(self):
        for _ in range(100):
            self.d.record('guard_trip', reason='x' * 1000)
            self.d.ha_request(self.event(['switch.synthetic', 'switch.synthetic_ac']), self.b)
        self.assertEqual(len(self.d.history), HISTORY_LIMIT)
        self.assertEqual(len(self.d._ha_requests), 2)
        self.assertLessEqual(max(len(row.get('reason') or '') for row in self.d.history), 256)
        self.assertEqual(self.d.counts['guard_trip'], 100)
        restarted = ShutdownDiagnostics()
        self.assertEqual(restarted.counts['guard_trip'], 0)
        self.assertIsNone(restarted.times['guard_trip'])
        self.assertIsNone(restarted.values(self.now.timestamp())[0]['shutdown_isolation_confirmed'])
