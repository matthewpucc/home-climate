"""Wi-Fi failures reconcile one device without cycling healthy equipment."""
import unittest

from house_climate.__main__ import preflight
from house_climate.executor import ControlFault, Executor
from house_climate.ha import HAError
from house_climate.policy import Plan
from tests.test_controller import FakeHA, add_helpers, config, fixture


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.ha = FakeHA(fixture(self.cfg))
        self.executor = Executor(self.cfg, self.ha, "°F")
        self.bonus = self.cfg.heads["bonus"]
        self.guest = self.cfg.heads["guest"]
        self.living = self.cfg.heads["living"]

    def cooling(self, living=False):
        targets = {"bonus": 72, "guest": 72}
        if living:
            targets["living"] = 68
        return Plan(mode="cool", head_targets_f=targets)

    def step(self, now, plan=None):
        plan = plan if plan is not None else self.cooling()
        self.executor.step(plan, self.ha.states(), now)
        return plan

    def start(self):
        for now in (0, 15, 195, 210):
            self.step(now)
        self.assertEqual(self.executor.phase, "running")

    def calls(self, entity, service=None):
        return [call for call in self.ha.calls if call[2]["entity_id"] == entity
                and (service is None or call[1] == service)]

    def disconnect(self, entity):
        self.ha.data[entity]["state"] = "unavailable"
        self.ha.ignore.add(entity)

    def test_offline_head_preserves_established_direction_and_healthy_heads(self):
        self.start()
        self.ha.calls.clear()
        self.disconnect(self.living)
        plan = self.step(240)
        self.step(255)
        self.assertEqual(self.executor.phase, "running")
        self.assertEqual(self.executor.direction, "cool")
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.assertIn(self.living, self.executor.waiting_devices)
        self.assertTrue(any(self.living in warning for warning in plan.warnings))
        self.assertFalse(self.calls(self.guest))
        self.assertFalse(self.calls(self.bonus))
        self.assertTrue(all(call[2]["hvac_mode"] == "off"
                            for call in self.calls(self.living)))

    def test_reconnect_acknowledges_off_before_local_resume_without_global_cycle(self):
        self.start()
        self.ha.calls.clear()
        self.disconnect(self.living)
        self.step(240, self.cooling(living=True))
        self.ha.ignore.remove(self.living)
        self.step(255, self.cooling(living=True))
        self.assertEqual(self.ha.data[self.living]["state"], "cool")
        self.step(270, self.cooling(living=True))
        self.assertNotIn(self.living, self.executor.waiting_devices)
        self.assertEqual(len(self.calls(self.living, "set_temperature")), 1)
        self.step(285, self.cooling(living=True))
        self.assertEqual(self.ha.data[self.living]["state"], "cool")
        self.assertFalse(self.calls(self.guest))
        self.assertFalse(self.calls(self.bonus))
        self.assertEqual(self.executor.phase, "running")

    def test_changeover_waits_indefinitely_for_every_head_and_full_settling(self):
        self.start()
        self.disconnect(self.living)
        self.step(240)
        self.ha.calls.clear()
        heat = lambda: Plan(mode="heat", head_targets_f={"primary": 68})
        self.step(255, heat())
        started = self.executor.stop_started
        for now in (300, 600):
            plan = self.step(now, heat())
            self.assertEqual(self.executor.phase, "stopping")
            self.assertEqual(self.executor.stop_started, started)
            self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))
            self.assertTrue(plan.warnings)
        self.ha.ignore.remove(self.living)
        self.step(615, heat())
        self.step(630, heat())
        self.step(645, heat())
        self.assertEqual(self.executor.phase, "settling")
        self.assertEqual(self.executor.all_off_since, 615)
        self.step(794, heat())
        self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))
        self.step(795, heat())
        self.assertEqual(self.ha.data[self.cfg.heads["primary"]]["state"], "heat")

    def test_reported_opposite_mode_still_forces_shared_off_barrier(self):
        self.start()
        self.disconnect(self.living)
        self.step(240)
        self.ha.ignore.remove(self.living)
        self.ha.data[self.living]["state"] = "heat"
        self.ha.calls.clear()
        self.step(255)
        self.assertIn(self.executor.phase, ("stopping", "settling"))
        self.assertEqual(self.ha.data[self.guest]["state"], "off")
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))

    def test_activation_timeout_recovers_only_failed_head(self):
        self.ha.ignore.add(self.bonus)
        self.start()
        self.step(240)
        self.assertIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertEqual(self.executor.phase, "running")
        self.ha.calls.clear()
        self.ha.ignore.remove(self.bonus)
        self.step(255)
        self.assertEqual(self.calls(self.bonus)[0][2]["hvac_mode"], "off")
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.step(270)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.step(285)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.assertFalse(self.calls(self.guest))

    def test_transport_error_retains_ambiguous_activation_until_fresh_off(self):
        original = self.ha.service
        failed = False

        def flaky(domain, service, **data):
            nonlocal failed
            if service == "set_temperature" and data["entity_id"] == self.bonus and not failed:
                failed = True
                self.ha.calls.append((domain, service, data))
                raise HAError("Wi-Fi timed out after dispatch")
            original(domain, service, **data)

        self.ha.service = flaky
        self.step(0)
        self.step(15)
        self.step(195)
        self.assertIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.executor.pending[self.bonus].command[0], "set_temperature")
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.step(210)
        self.assertEqual(self.calls(self.bonus)[1][2]["hvac_mode"], "off")
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.step(225)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.step(240)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")

    def test_cached_off_does_not_acknowledge_superseding_off(self):
        self.ha.ignore.add(self.bonus)
        self.start()
        self.step(240)
        self.step(255)  # Superseding off is also lost; snapshot still says off.
        self.assertEqual(self.ha.data[self.bonus]["state"], "off")
        self.step(270)
        self.assertIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.executor.pending[self.bonus].command,
                         ("set_hvac_mode", (("hvac_mode", "off"),)))
        self.ha.data[self.bonus]["last_reported"] = "new-device-report"
        self.step(285)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)

    def test_late_heat_is_stopped_before_recovery_is_cleared(self):
        heater = self.cfg.heater_entity
        self.ha.ignore.add(heater)
        self.step(0, Plan(heater_on=True))
        self.step(45, Plan(heater_on=True))
        self.step(60, Plan())  # Lost off; old snapshot remains off.
        self.ha.data[heater]["state"] = "heat"
        self.ha.ignore.remove(heater)
        self.step(75, Plan())
        self.assertEqual(self.ha.data[heater]["state"], "off")
        self.assertNotIn(heater, self.executor.waiting_devices)
        self.step(90, Plan())
        self.assertNotIn(heater, self.executor.waiting_devices)
        self.assertEqual(len(self.calls(heater, "set_temperature")), 1)

    def test_auxiliary_disconnect_does_not_interrupt_shared_cooling(self):
        self.start()
        heater = self.cfg.heater_entity
        self.disconnect(heater)
        self.ha.calls.clear()
        for now in (240, 255, 270):
            self.step(now, Plan(mode="cool", head_targets_f={"bonus": 72, "guest": 72},
                                heater_on=True))
        self.assertIn(heater, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertFalse(self.calls(self.guest))
        self.assertFalse(self.calls(heater, "set_temperature"))
        self.ha.ignore.remove(heater)
        self.step(285, Plan(mode="cool", head_targets_f={"bonus": 72, "guest": 72}, heater_on=True))
        self.step(300, Plan(mode="cool", head_targets_f={"bonus": 72, "guest": 72}, heater_on=True))
        self.assertNotIn(heater, self.executor.waiting_devices)
        self.assertEqual(len(self.calls(heater, "set_temperature")), 1)
        self.step(315, Plan(mode="cool", head_targets_f={"bonus": 72, "guest": 72}, heater_on=True))
        self.assertEqual(self.ha.data[heater]["state"], "heat")
        self.assertFalse(self.calls(self.guest))

    def test_retries_are_bounded_without_forgetting_shutdown_obligation(self):
        self.disconnect(self.living)
        for now in range(0, 46):
            self.step(now)
        off_calls = self.calls(self.living)
        self.assertLessEqual(len(off_calls), 4)
        self.assertGreaterEqual(len(off_calls), 3)
        self.assertIn(self.living, self.executor.waiting_devices)
        self.assertEqual(self.executor.phase, "stopping")

    def test_startup_shutdown_failure_latches_recovery_despite_cached_off(self):
        original = self.ha.service

        def failed_off(domain, service, **data):
            if data["entity_id"] == self.living:
                self.ha.calls.append((domain, service, data))
                raise HAError("head offline")
            original(domain, service, **data)

        self.ha.service = failed_off
        failures = self.executor.shutdown(self.ha.states())
        self.assertIn(self.living, failures)
        self.assertIn(self.living, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.living]["state"], "off")
        self.step(0)
        self.step(45)
        self.assertEqual(self.executor.phase, "stopping")
        self.assertIn(self.living, self.executor.waiting_devices)
        self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))
        self.ha.service = original

    def test_preflight_tolerates_disconnect_incomplete_metadata_and_mode_subsets(self):
        for unavailable in ("unavailable", "unknown", None):
            with self.subTest(state=unavailable):
                states = fixture(self.cfg)
                add_helpers(states)
                if unavailable is None:
                    states.pop(self.living)
                else:
                    states[self.living]["state"] = unavailable
                preflight(self.cfg, states, self.executor)
        states = fixture(self.cfg)
        add_helpers(states)
        states[self.living]["attributes"]["hvac_modes"] = []
        preflight(self.cfg, states, self.executor)
        states[self.living]["attributes"]["hvac_modes"] = ["off", "heat"]
        preflight(self.cfg, states, self.executor)
        states = fixture(self.cfg)
        add_helpers(states)
        states[self.cfg.heater_entity]["attributes"]["max_temp"] = 80
        with self.assertRaises(ControlFault):
            preflight(self.cfg, states, self.executor)

    def test_incomplete_metadata_prevents_initial_activation_until_complete(self):
        self.ha.data[self.living]["attributes"]["hvac_modes"] = []
        for now in (0, 15, 195):
            self.step(now)
        self.assertEqual(self.executor.phase, "stopping")
        self.assertIn(self.living, self.executor.waiting_devices)
        self.assertFalse(any(call[1] == "set_temperature" for call in self.ha.calls))
        self.ha.data[self.living]["attributes"]["hvac_modes"] = ["off", "heat", "cool"]
        self.step(210)
        self.step(225)
        self.step(405)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")

    def test_cool_only_unused_head_does_not_fault_or_cycle_healthy_cooling(self):
        self.ha.data[self.living]["attributes"]["hvac_modes"] = ["off", "cool"]
        states = self.ha.states()
        add_helpers(states)
        preflight(self.cfg, states, self.executor)
        self.start()
        self.ha.calls.clear()
        for now in (240, 255, 270):
            self.step(now)
        self.assertEqual(self.ha.data[self.guest]["state"], "cool")
        self.assertFalse(self.calls(self.guest))
        self.assertFalse(self.calls(self.living))
        self.assertNotIn(self.living, self.executor.waiting_devices)

    def test_unsupported_requested_heat_is_local_and_resumes_when_capability_returns(self):
        self.ha.data[self.living]["attributes"]["hvac_modes"] = ["off", "cool"]
        heat = lambda: Plan(mode="heat", head_targets_f={"primary": 68, "living": 68})
        for now in (0, 15, 195, 210):
            self.step(now, heat())
        primary = self.cfg.heads["primary"]
        self.assertEqual(self.ha.data[primary]["state"], "heat")
        self.assertEqual(self.ha.data[self.living]["state"], "off")
        self.assertNotIn(self.living, self.executor.recoveries)
        self.assertNotIn(self.living, self.executor.pending)
        details = self.executor.recovery_status(210)["devices"][self.living]
        self.assertEqual(details["kind"], "capability")
        self.assertEqual(details["required_mode"], "heat")
        self.assertEqual(set(details["available_modes"]), {"off", "cool"})
        self.assertIn("heat", details["reason"])
        self.assertFalse(self.calls(self.living))  # Already-off must be a no-op.
        count = len(self.calls(primary))
        self.ha.data[self.living]["attributes"]["hvac_modes"].append("heat")
        self.step(225, heat())
        self.assertEqual(self.ha.data[self.living]["state"], "heat")
        self.assertNotIn(self.living, self.executor.waiting_devices)
        self.assertEqual(len(self.calls(primary)), count)

    def test_capability_only_block_needs_no_new_off_report(self):
        self.ha.data[self.living]["attributes"]["hvac_modes"] = ["off", "cool"]
        original = self.ha.service

        def no_off_needed(domain, service, **data):
            if data["entity_id"] == self.living:
                raise AssertionError("Available already-off head must not receive an off command")
            original(domain, service, **data)

        self.ha.service = no_off_needed
        heat = lambda: Plan(mode="heat", head_targets_f={"primary": 68, "living": 68})
        for now in (0, 15, 195, 240, 600):
            self.step(now, heat())
        self.assertIn(self.living, self.executor.capability_blocks)
        self.assertNotIn(self.living, self.executor.recoveries)
        self.assertEqual(self.ha.data[self.cfg.heads["primary"]]["state"], "heat")

    def test_post_activation_off_report_survives_successful_noop_off(self):
        self.ha.data[self.bonus]["last_reported"] = "before-activation"
        self.ha.ignore.add(self.bonus)
        self.start()
        self.ha.data[self.bonus]["last_reported"] = "after-reconnect"
        self.step(240)
        earliest = self.executor.recoveries[self.bonus].baseline
        self.ha.ignore.remove(self.bonus)
        original = self.ha.service

        def noop_off(domain, service, **data):
            if data["entity_id"] == self.bonus and service == "set_hvac_mode":
                self.ha.calls.append((domain, service, data))
                return  # Real integrations may avoid publishing already-off.
            original(domain, service, **data)

        self.ha.service = noop_off
        self.ha.calls.clear()
        self.step(255)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")
        self.assertEqual([call[1] for call in self.calls(self.bonus)],
                         ["set_hvac_mode", "set_temperature"])
        self.assertEqual(earliest[-3], "before-activation")

    def test_failed_off_receipts_preserve_earliest_baseline_and_block_recovery(self):
        self.ha.data[self.bonus]["last_reported"] = "before-activation"
        self.ha.ignore.add(self.bonus)
        self.start()
        self.step(240)
        earliest = self.executor.recoveries[self.bonus].baseline
        original = self.ha.service

        def failed_off(domain, service, **data):
            if data["entity_id"] == self.bonus and service == "set_hvac_mode":
                self.ha.calls.append((domain, service, data))
                raise HAError("off delivery failed")
            original(domain, service, **data)

        self.ha.service = failed_off
        self.step(255)
        self.ha.data[self.bonus]["last_reported"] = "after-reconnect"
        self.step(270)
        recovery = self.executor.recoveries[self.bonus]
        self.assertEqual(recovery.baseline, earliest)
        self.assertFalse(recovery.off_receipt)
        self.assertIn(self.bonus, self.executor.waiting_devices)
        self.ha.service = original
        self.ha.ignore.remove(self.bonus)
        self.step(285)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")

    def test_stale_off_is_not_confirmed_by_successful_noop_retries_or_time(self):
        self.ha.data[self.bonus]["last_reported"] = "unchanged-before-activation"
        self.ha.ignore.add(self.bonus)
        self.start()
        self.step(240)
        baseline = self.executor.recoveries[self.bonus].baseline
        for now in range(255, 601, 15):
            self.step(now)
            self.assertIn(self.bonus, self.executor.waiting_devices)
            self.assertEqual(self.executor.recoveries[self.bonus].baseline, baseline)
        self.ha.ignore.remove(self.bonus)
        self.ha.data[self.bonus]["last_reported"] = "genuine-new-device-report"
        self.step(615)
        self.assertNotIn(self.bonus, self.executor.waiting_devices)
        self.assertEqual(self.ha.data[self.bonus]["state"], "cool")

    def test_recovery_refresh_is_bounded_to_one_cached_state_read(self):
        self.start()
        self.disconnect(self.living)
        self.step(240, self.cooling(living=True))
        self.ha.ignore.remove(self.living)
        snapshot = self.ha.states()
        original = self.ha.states
        reads = []

        def read():
            reads.append(True)
            return original()

        self.ha.states = read
        self.executor.step(self.cooling(living=True), snapshot, 255)
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.ha.data[self.living]["state"], "cool")

    def test_settling_status_exposes_remaining_configured_delay(self):
        self.start()
        self.step(300, Plan(mode="heat", head_targets_f={"primary": 68}))
        status = self.executor.recovery_status(300)
        self.assertEqual(status["phase"], "settling")
        self.assertEqual(status["settle_remaining_seconds"], self.cfg.off_settle_seconds)
        self.assertEqual(self.executor.recovery_status(315)["settle_remaining_seconds"],
                         self.cfg.off_settle_seconds - 15)
        self.assertFalse(any(call[1] == "set_temperature" and call[2]["hvac_mode"] == "heat"
                             for call in self.ha.calls))
