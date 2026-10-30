"""Auxiliary activation cannot assume a target command supplies fan power."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.executor import Executor
from house_climate.ha import HAActivationDeferred, HAError
from house_climate.policy import Plan
from tests.test_controller import FakeHA, NIGHT, config, fixture


class AuxiliaryProviderHA(FakeHA):
    """Target programming selects heating but does not power an idle fan on."""

    def __init__(self, cfg, mode="off", target=72):
        super().__init__(fixture(cfg))
        self.heater = cfg.heater_entity
        self.data[self.heater]["state"] = mode
        self.data[self.heater]["attributes"]["temperature"] = target
        self.reports = 0
        self.positive_allowed = True
        self.defer_positive = False
        self.hold_target = False
        self.hold_mode = False
        self.fail_target = False
        self.jobs_on_target_failure = False
        self.fail_mode = False
        self.hold_off = False
        self.attempts = []
        self.deadlines = []
        self.jobs = ()
        self.uncertain = ()
        self.now = 0

    def service_before(self, domain, service, *, deadline, **data):
        self.deadlines.append(deadline)
        if self.now >= deadline:
            raise HAActivationDeferred("Original activation budget expired")
        return self.service(domain, service, **data)

    def service_jobs(self, entities):
        return self.jobs

    def uncertain_service_jobs(self, entities):
        return self.uncertain

    def report(self, *, mode=None, target=None):
        self.reports += 1
        observed = self.data[self.heater]
        if mode is not None:
            observed["state"] = mode
        if target is not None:
            observed["attributes"]["temperature"] = target
        observed["last_reported"] = (NIGHT + timedelta(seconds=self.reports)).isoformat()

    def service(self, domain, service, **data):
        if data["entity_id"] != self.heater:
            return super().service(domain, service, **data)
        self.attempts.append((domain, service, dict(data)))
        positive = service == "set_temperature" or data.get("hvac_mode") not in (None, "off")
        if positive:
            if self.defer_positive:
                raise HAActivationDeferred("Lease awaits confirmation")
            if not self.positive_allowed:
                raise HAError("Ownership or power guard rejects activation")
        self.calls.append((domain, service, dict(data)))
        if service == "set_temperature":
            if self.fail_target:
                if self.jobs_on_target_failure:
                    self.jobs = (object(),)
                raise HAError("Target service completion is uncertain")
            if not self.hold_target:
                # The mode field is deliberately ignored, like a provider
                # whose temperature implementation does not enable fan power.
                mode = "heat" if self.data[self.heater]["state"] != "off" else "off"
                self.report(mode=mode, target=data["temperature"])
        elif service == "set_hvac_mode":
            if data["hvac_mode"] == "heat" and self.fail_mode:
                raise HAError("Heat service completion is uncertain")
            if data["hvac_mode"] == "off" and self.hold_off:
                return
            if data["hvac_mode"] != "heat" or not self.hold_mode:
                self.report(mode=data["hvac_mode"])


class AuxiliaryStartupTests(unittest.TestCase):
    def make(self, *, mode="off", target=72, timeout=45):
        self.cfg = replace(config(), command_timeout_seconds=timeout)
        self.ha = AuxiliaryProviderHA(self.cfg, mode, target)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.heater = self.cfg.heater_entity
        return self.ha

    def tick(self, now, heating=True):
        self.ha.now = now
        self.executor.auxiliary(Plan(heater_on=heating), self.ha.states(), now)

    def commands(self):
        return [(service, data) for _, service, data in self.ha.calls]

    def test_initially_off_programs_target_then_explicit_heat_once(self):
        self.make()
        self.tick(0)
        self.tick(1)
        self.tick(2)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])
        self.assertEqual(self.commands()[0][1]["hvac_mode"], "heat")
        self.assertEqual(self.commands()[0][1]["temperature"], 86)
        self.assertEqual(self.commands()[1][1]["hvac_mode"], "heat")
        for now in (3, 15, 60):
            self.tick(now)
        self.assertEqual(len(self.ha.calls), 2)

    def test_initially_off_with_unchanged_target_still_requests_heat(self):
        self.make(target=86)
        self.tick(0)
        self.tick(1)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")
        self.tick(2)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])

    def test_already_heat_with_correct_target_does_not_cycle(self):
        self.make(mode="heat", target=86)
        for now in (0, 15, 60):
            self.tick(now)
        self.assertEqual(self.ha.calls, [])

    def test_already_heat_updates_target_without_repeated_power_commands(self):
        self.make(mode="heat")
        self.tick(0)
        self.tick(1)
        self.tick(2)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")
        self.assertEqual([service for service, _ in self.commands()], ["set_temperature"])

    def test_fan_only_programming_is_positive_then_explicit_heat_once(self):
        self.make(mode="fan_only")
        self.tick(0)
        self.tick(1)
        self.tick(2)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])
        self.assertTrue(all(data.get("hvac_mode") == "heat" for _, data in self.commands()))

    def test_unchanged_cached_target_does_not_skip_fresh_programming_ack(self):
        self.make(target=86)
        self.ha.hold_target = True
        self.tick(0)
        self.tick(1)
        self.assertEqual([service for service, _ in self.commands()], ["set_temperature"])
        self.ha.report(target=86)
        self.tick(2)
        self.tick(3)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])

    def test_both_stages_share_original_deadline_and_failure_does_not_repeat_on(self):
        self.make()
        self.ha.hold_target = self.ha.hold_mode = True
        self.tick(0)
        self.tick(20)
        self.ha.report(target=86)
        self.tick(30)
        self.assertEqual(self.ha.deadlines, [45, 45])
        self.tick(44)
        self.tick(45)
        self.assertEqual(self.ha.data[self.heater]["state"], "off")
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.assertEqual([data["hvac_mode"] for service, data in self.commands()
                          if service == "set_hvac_mode"], ["heat", "off"])
        for now in (46, 60, 90, 180):
            self.tick(now)
        self.assertEqual([data["hvac_mode"] for service, data in self.commands()
                          if service == "set_hvac_mode"], ["heat", "off"])
        self.assertIn(self.heater, self.executor.device_issues)

    def test_target_ack_at_deadline_cannot_start_heat(self):
        self.make()
        self.ha.hold_target = True
        self.tick(0)
        self.ha.report(target=86)
        self.tick(45)
        self.assertFalse(any(service == "set_hvac_mode" and data["hvac_mode"] == "heat"
                             for service, data in self.commands()))
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")

    def test_replacement_target_cannot_renew_a_startup(self):
        self.make()
        self.ha.hold_target = True
        self.tick(0)
        self.executor.config = replace(self.cfg, heater_high_setpoint_f=85)
        self.tick(15)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.assertIsNotNone(self.executor._auxiliary_failure)

    def test_target_service_failure_reconciles_off_and_holds_same_intent(self):
        self.make()
        self.ha.fail_target = True
        self.tick(0)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        for now in (15, 30, 60):
            self.tick(now)
        self.assertEqual(len(self.ha.calls), 2)

    def test_mode_failure_reconciles_off_and_holds_same_intent(self):
        self.make()
        self.tick(0)
        self.ha.fail_mode = True
        self.tick(1)
        self.assertEqual([data["hvac_mode"] for service, data in self.commands()
                          if service == "set_hvac_mode"], ["heat", "off"])
        for now in (15, 30, 60):
            self.tick(now)
        self.assertEqual(len(self.ha.calls), 3)

    def test_guard_or_ownership_loss_before_heat_never_dispatches_heat(self):
        self.make()
        self.tick(0)
        self.ha.positive_allowed = False
        self.tick(1)
        self.assertFalse(any(service == "set_hvac_mode" and data["hvac_mode"] == "heat"
                             for service, data in self.commands()))
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.assertIsNotNone(self.executor._auxiliary_failure)

    def test_deferred_heat_then_off_still_sends_a_superseding_off(self):
        self.make()
        self.tick(0)
        self.ha.defer_positive = True
        self.tick(1)
        self.assertNotIn(self.heater, self.executor.pending)
        self.tick(2, heating=False)
        self.assertEqual([service for service, _ in self.commands()],
                         ["set_temperature", "set_hvac_mode"])
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.tick(3, heating=False)
        self.assertIsNone(self.executor._auxiliary_failure)

    def test_late_or_uncertain_jobs_prevent_rearm_even_after_nonheat_and_off(self):
        self.make()
        self.ha.fail_target = True
        self.tick(0)
        self.ha.jobs = (object(),)
        self.tick(1, heating=False)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.ha.jobs, self.ha.uncertain = (), (self.heater,)
        self.tick(2, heating=False)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.ha.uncertain = ()
        self.tick(3, heating=False)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.tick(4, heating=False)
        self.assertIsNone(self.executor._auxiliary_failure)
        self.ha.fail_target = False
        self.tick(5)
        self.tick(6)
        self.tick(7)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")

    def test_same_adapter_executor_recreation_cannot_reset_failed_intent(self):
        self.make()
        self.ha.fail_target = True
        self.tick(0)
        self.tick(1)
        count = len(self.ha.calls)
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.ha.fail_target = False
        self.tick(2)
        self.assertEqual(len(self.ha.calls), count)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.tick(3, heating=False)
        self.assertIsNone(self.executor._auxiliary_failure)
        self.tick(4)
        self.tick(5)
        self.tick(6)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")

    def test_internal_shutdown_does_not_clear_failure_hold(self):
        self.make()
        self.ha.fail_target = True
        self.tick(0)
        self.executor.shutdown(self.ha.states(), reason="Internal fault cleanup")
        self.ha.fail_target = False
        count = len(self.ha.calls)
        self.tick(1)
        self.assertEqual(len(self.ha.calls), count)
        self.assertIsNotNone(self.executor._auxiliary_failure)

    def test_restored_or_assumed_target_feedback_never_authorizes_heat(self):
        for flag in ("restored", "assumed_state"):
            with self.subTest(flag=flag):
                self.make()
                self.tick(0)
                self.ha.data[self.heater]["attributes"][flag] = True
                self.ha.report(target=86)
                self.tick(1)
                self.assertFalse(any(service == "set_hvac_mode" and data["hvac_mode"] == "heat"
                                     for service, data in self.commands()))
                self.tick(45)
                self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")

    def test_untrusted_already_heat_cannot_use_the_ordinary_shortcut(self):
        for flag in ("restored", "assumed_state"):
            for target in (72, 86):
                with self.subTest(flag=flag, target=target):
                    self.make(mode="heat", target=target)
                    self.ha.data[self.heater]["attributes"][flag] = True
                    self.tick(0)
                    self.assertEqual(self.ha.calls, [])
                    self.assertIn("trusted", self.executor.device_issues[self.heater])

    def test_duplicate_or_backwards_target_report_is_not_new_acknowledgement(self):
        self.make()
        self.ha.report(target=72)
        old = self.ha.data[self.heater]["last_reported"]
        self.ha.hold_target = True
        self.tick(0)
        self.ha.data[self.heater]["attributes"]["temperature"] = 86
        self.ha.data[self.heater]["last_reported"] = old
        self.tick(1)
        self.assertEqual(len(self.ha.calls), 1)
        self.ha.data[self.heater]["last_reported"] = NIGHT.isoformat()
        self.tick(2)
        self.assertEqual(len(self.ha.calls), 1)
        self.ha.report(target=86)
        self.tick(3)
        self.tick(4)
        self.assertEqual(self.ha.data[self.heater]["state"], "heat")

    def test_adapter_without_bounded_dispatch_defers_new_start_but_allows_off(self):
        self.make()
        self.ha.service_before = None
        self.tick(0)
        self.assertEqual(self.ha.calls, [])
        self.assertIn("bounded", self.executor.device_issues[self.heater])
        self.tick(1, heating=False)
        self.assertEqual(self.ha.calls, [])

    def test_cached_off_after_cancel_cannot_enable_fan_without_new_off_report(self):
        self.make()
        self.tick(0)
        self.ha.defer_positive = True
        self.tick(1)
        self.ha.hold_off = True
        self.executor.auxiliary(Plan(auxiliary_fan_only=True), self.ha.states(), 2)
        self.executor.auxiliary(Plan(auxiliary_fan_only=True), self.ha.states(), 3)
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.assertFalse(any(data.get("hvac_mode") == "fan_only" for _, data in self.commands()))
        self.assertIsNotNone(self.executor._auxiliary_failure)

    def test_off_is_requested_again_after_late_job_cleanup_before_rearm(self):
        self.make()
        self.ha.fail_target = True
        self.tick(0)
        self.ha.jobs = (object(),)
        self.tick(1, heating=False)
        count = len(self.ha.calls)
        self.ha.jobs = ()
        self.ha.hold_off = True
        self.tick(2, heating=False)
        self.assertEqual(len(self.ha.calls), count + 1)
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.tick(3, heating=False)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.ha.report(mode="off")
        self.tick(4, heating=False)
        self.assertIsNone(self.executor._auxiliary_failure)

    def test_old_recovery_receipt_cannot_replace_post_cleanup_off_request(self):
        self.make()
        self.ha.fail_target = self.ha.jobs_on_target_failure = True
        self.tick(0)
        self.assertTrue(self.executor.recoveries[self.heater].off_receipt)
        count = len(self.ha.calls)
        self.ha.jobs = ()
        self.ha.hold_off = True
        self.tick(1, heating=False)
        self.assertEqual(len(self.ha.calls), count + 1)
        self.assertEqual(self.commands()[-1][1]["hvac_mode"], "off")
        self.assertIn(self.heater, self.executor.recoveries)
        self.assertIsNotNone(self.executor._auxiliary_failure)
        self.ha.report(mode="off")
        self.tick(2, heating=False)
        self.assertIsNone(self.executor._auxiliary_failure)


if __name__ == "__main__":
    unittest.main()
