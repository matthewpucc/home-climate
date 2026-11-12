import unittest
from unittest.mock import Mock
from house_climate.executor import Executor, ClimateRecovery
from tests.test_controller import config

class ActionableRecoveryTests(unittest.TestCase):
    def test_retry_window_does_not_renew_and_clears(self):
        cfg = config()
        ex = Executor(cfg, Mock(), '°F')
        entity = next(iter(cfg.heads.values()))
        ex.recoveries[entity] = ClimateRecovery((), 'first failure')
        ex._sync_issue(entity)
        def disposition(now):
            return ex.recovery_status(now)['devices'][entity]['notification_disposition']
        self.assertEqual(disposition(10), 'recovering')
        ex.recoveries[entity].reason = 'second failure'
        self.assertEqual(disposition(10 + cfg.head_activation_timeout_seconds), 'human_required')
        ex._clear_recovery(entity)
        self.assertNotIn(entity, ex._attention_started)

    def test_heater_uncertainty_is_not_suppressed(self):
        cfg = config()
        ex = Executor(cfg, Mock(), '°F')
        ex.recoveries[cfg.heater_entity] = ClimateRecovery((), 'Off uncertain')
        ex._sync_issue(cfg.heater_entity)
        self.assertEqual(ex.recovery_status(10)['devices'][cfg.heater_entity]['notification_disposition'], 'human_required')
