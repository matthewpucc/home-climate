from datetime import datetime, timezone
import unittest
from house_climate.head_fan_command import FanCommand, FanContext

NOW = datetime(2026, 11, 12, 20, tzinfo=timezone.utc)
CTX = FanContext('plan1', True, True, True, False)

def state(mode='auto', **kwargs):
    return {'state': 'cool', 'attributes': {'fan_mode': mode, 'fan_modes': ['auto', 'low', 'quiet']}, **kwargs}


class FanCommandTests(unittest.TestCase):
    def request(self, c, **kwargs):
        args = dict(elapsed=0, wall_now=NOW, timezone='America/Los_Angeles', sample_id='before')
        args.update(kwargs)
        return c.request('low', state(), CTX, **args)

    def observe(self, c, t, mode='low', **kwargs):
        args = dict(elapsed=t, wall_now=NOW, timezone='America/Los_Angeles', sample_id=str(t), event_at=t)
        args.update(kwargs)
        return c.observe(state(mode), CTX, **args)

    def test_request_is_single_use_and_reports_never_physical_confirmation(self):
        c = FanCommand()
        self.assertEqual(self.request(c), {'fan_mode': 'low'})
        self.assertIsNone(self.request(c))
        self.assertEqual(self.observe(c, 1).status, 'dispatch_pending')
        c.dispatched(outcome='accepted', elapsed=1)
        self.assertEqual(self.observe(c, 2).status, 'settling')
        result = self.observe(c, 12)
        self.assertEqual(result.status, 'reported_consistent')
        self.assertEqual(result.physical_confirmation, 'unknown')

    def test_cached_reads_do_not_count(self):
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='accepted', elapsed=0)
        self.observe(c, 1, sample_id='before')
        self.observe(c, 20, sample_id='before')
        self.assertEqual(c.result.status, 'settling')
        self.assertEqual(self.observe(c, 60).status, 'unconfirmed')
        self.assertIsNone(self.request(c))

    def test_matching_reports_must_remain_consistent(self):
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='uncertain', elapsed=0)
        self.observe(c, 1)
        self.observe(c, 10, 'auto')
        self.observe(c, 12)
        self.assertEqual(self.observe(c, 21).status, 'settling')
        self.assertEqual(self.observe(c, 22).status, 'reported_consistent')

    def test_unknown_command_outcome_does_not_resend(self):
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='uncertain', elapsed=0)
        self.assertIsNone(self.request(c))
        self.assertEqual(self.observe(c, 60, 'auto').status, 'unconfirmed')

    def test_context_cancellation_and_no_restore(self):
        for ctx in (FanContext('plan2', True, True, True, False),
                    FanContext('plan1', False, True, True, False),
                    FanContext('plan1', True, True, True, True)):
            c = FanCommand()
            self.request(c)
            c.dispatched(outcome='accepted', elapsed=0)
            result = c.observe(state(), ctx, elapsed=1, wall_now=NOW, timezone='UTC', sample_id='after', event_at=1)
            self.assertEqual(result.status, 'cancelled')
            self.assertIsNone(self.request(c))

    def test_external_mode_change_and_off_cancel(self):
        for s in (state('quiet'), state(state='heat'), state(state='off'), state(state='unavailable')):
            c = FanCommand()
            self.request(c)
            c.dispatched(outcome='accepted', elapsed=0)
            result = c.observe(s, CTX, elapsed=1, wall_now=NOW, timezone='UTC', sample_id='after', event_at=1)
            self.assertEqual(result.status, 'cancelled')

    def test_sleep_cutoff_and_margin(self):
        for wall in (datetime(2026, 11, 13, 6, tzinfo=timezone.utc),
                     datetime(2026, 11, 13, 5, 54, tzinfo=timezone.utc)):
            c = FanCommand()
            self.assertIsNone(self.request(c, wall_now=wall))
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='accepted', elapsed=0)
        self.assertEqual(self.observe(c, 10, wall_now=datetime(2026,11,13,6,tzinfo=timezone.utc)).status, 'cancelled')

    def test_rejected_unsupported_and_bad_clock(self):
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='rejected', elapsed=0)
        self.assertEqual(c.result.status, 'failed')
        for value in (True, float('nan'), float('inf'), 'bad'):
            self.assertIsNone(self.request(FanCommand(), elapsed=value))
        c = FanCommand()
        self.request(c)
        c.dispatched(outcome='accepted', elapsed=0)
        self.observe(c, 4)
        self.assertEqual(self.observe(c, 3).status, 'cancelled')
        for options in ({'settle_seconds': 60}, {'deadline_seconds': 0}, {'settle_seconds': True}):
            with self.assertRaises(ValueError):
                FanCommand(**options)

    def test_delayed_predispatch_reports_do_not_count(self):
        c = FanCommand()
        self.request(c)
        self.observe(c, 1, sample_id='early')
        c.dispatched(outcome='accepted', elapsed=2)
        self.observe(c, 20, sample_id='early', event_at=1)
        self.observe(c, 21, sample_id='delayed', event_at=1.5)
        self.assertEqual(c.result.status, 'settling')
        self.observe(c, 22, event_at=22)
        self.assertEqual(self.observe(c, 31).status, 'settling')
        self.assertEqual(self.observe(c, 32).status, 'reported_consistent')


class OvernightScopeTests(unittest.TestCase):
    def test_non_sleeping_trial_needs_explicit_scope_and_end(self):
        now = datetime(2026, 11, 13, 7, tzinfo=timezone.utc)  # 23:00 Pacific
        end = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
        def request(context):
            c = FanCommand()
            payload = c.request('low', state(), context, elapsed=0, wall_now=now,
                                timezone='America/Los_Angeles', sample_id='before')
            return c, payload
        self.assertIsNone(request(CTX)[1])
        # Primary/sleep-sensitive protection wins over overnight permission.
        self.assertIsNone(request(FanContext('p', True, True, True, False,
                                             True, True, end))[1])
        self.assertIsNone(request(FanContext('p', True, True, True, False,
                                             False, True))[1])
        c, payload = request(FanContext('p', True, True, True, False, False, True, end))
        self.assertEqual(payload, {'fan_mode': 'low'})
        self.assertEqual(c.result.status, 'dispatch_pending')

    def test_trial_end_and_scope_withdrawal_cancel(self):
        now = datetime(2026, 11, 13, 7, tzinfo=timezone.utc)
        end = datetime(2026, 11, 13, 7, 7, tzinfo=timezone.utc)
        ctx = FanContext('p', True, True, True, False, False, True, end)
        c = FanCommand(deadline_seconds=180)
        # Must reserve cleanup time as well as the whole request deadline.
        self.assertIsNone(c.request('low', state(), ctx, elapsed=0, wall_now=now,
                                   timezone='America/Los_Angeles', sample_id='before'))
        c = FanCommand()
        self.assertIsNotNone(c.request('low', state(), ctx, elapsed=0, wall_now=now,
                                      timezone='America/Los_Angeles', sample_id='before'))
        c.dispatched(outcome='accepted', elapsed=1)
        withdrawn = FanContext('p', True, True, True, False, False, False, end)
        self.assertEqual(c.observe(state('low'), withdrawn, elapsed=2, wall_now=now,
                                   timezone='America/Los_Angeles', sample_id='after',
                                   event_at=2).status, 'cancelled')


    def test_explicit_end_applies_during_daytime_too(self):
        end = datetime(2026, 11, 12, 19, tzinfo=timezone.utc)
        ctx = FanContext('p', True, True, True, False, True, False, end)
        c = FanCommand()
        self.assertIsNone(c.request('low', state(), ctx, elapsed=0, wall_now=NOW,
                                   timezone='America/Los_Angeles', sample_id='before'))

    def test_late_service_completion_does_not_extend_deadline(self):
        c = FanCommand()
        c.request('low', state(), CTX, elapsed=0, wall_now=NOW,
                  timezone='America/Los_Angeles', sample_id='before')
        c.dispatched(outcome='accepted', elapsed=60)
        self.assertEqual(c.result.status, 'unconfirmed')
