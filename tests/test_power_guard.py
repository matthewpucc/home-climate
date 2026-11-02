"""Exercise electrical policy and async failover without importing HA itself."""
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from house_climate.bindings import example_bindings


# The integration package imports HA during __init__; load this deliberately
# standard-library-only module directly for the core Python 3.11/3.12 suite.
_path = Path(__file__).resolve().parents[1] / "custom_components/house_climate/power_guard.py"
_spec = importlib.util.spec_from_file_location("_tested_house_power_guard", _path)
power = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = power
_spec.loader.exec_module(power)

B = example_bindings().power

# Keep production's 5/10/5-second proportions, compressed by ten for core
# tests. Millisecond deadlines can expire during an unrelated runner/GC stall.
TEST_OFF_ACK_SECONDS = .5
TEST_RESTORE_ACK_SECONDS = 1.0
TEST_SERVICE_TIMEOUT_SECONDS = .5


def advisory_report(clock, case):
    """Circuit observations cover ordinary disagreement and unusable data."""
    if case == "missing":
        return None
    value, unit = {"high": (5000, "W"), "unavailable": ("unavailable", "W"),
                   "unknown": ("unknown", "W"), "nonfinite": ("NaN", "W"),
                   "wrong_unit": (6, "A")}.get(case, (140, "W"))
    stamp = clock.wall + 6 if case == "future" else clock.wall - (181 if case == "stale" else 0)
    attrs = {"unit_of_measurement": unit}
    if case in ("restored", "assumed_state"):
        attrs[case] = True
    return {"state": str(value), "attributes": attrs,
            "last_reported": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.wall = 1800000000.0

    def advance(self, seconds):
        self.now += seconds
        # HA timestamps have microsecond precision. Normalize the synthetic
        # wall clock to the same representation instead of creating a report
        # a fraction of a microsecond later than its observation clock.
        self.wall = datetime.fromtimestamp(self.wall + seconds, timezone.utc).timestamp()


def measurements(clock, **changes):
    defaults = {
        B.circuit_power: (140, "W"), B.input_power: (130, "W"),
        B.output_power: (150, "W"), B.battery: (43, "%"),
        B.heater_current: (.05, "A"),
        B.wall_phase_a_voltage: (124, "V"), B.wall_phase_b_voltage: (123, "V"),
    }
    defaults.update(changes)
    result = {entity: {"state": str(value), "attributes": {"unit_of_measurement": unit},
                       "last_changed": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
                       "last_reported": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}
              for entity, (value, unit) in defaults.items()}
    result[B.heater] = {"state": "off", "attributes": {"hvac_action": "off"},
                            "last_reported": datetime.fromtimestamp(clock.wall - 1, timezone.utc).isoformat()}
    result[B.heater_switch] = {"state": "on", "attributes": {},
                                 "last_changed": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
                                 "last_reported": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}
    for entity in (B.ac_output_switch, B.heater_switch):
        result[entity] = {"state": "on", "attributes": {"device_report": {
            "state": "on", "reported_at": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}},
            "last_changed": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
            "last_reported": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat()}
    return result


class PowerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.states = measurements(self.clock)

    def reading(self):
        return power.evaluate_power(self.states, B, self.clock.wall)

    def test_no_watt_ceilings_or_upstream_dependencies(self):
        for upstream in (B.circuit_power, B.input_power, B.wall_phase_a_voltage, B.wall_phase_b_voltage):
            for value in ("50000", "unavailable", "NaN", "0"):
                with self.subTest(entity=upstream, value=value):
                    self.states = measurements(self.clock)
                    self.states[upstream]["state"] = value
                    self.states[B.output_power]["state"] = "50000"
                    r = self.reading()
                    self.assertIsNone(r.issue)
                    self.assertTrue(r.safe_to_rearm)
                    self.assertIsNone(r.input_budget_w)

    def test_reserve_boundary_and_invalid_battery(self):
        for value, allowed in (("40.01", True), ("40", False), ("0", False),
                               ("101", False), ("NaN", False), ("unavailable", False)):
            with self.subTest(value=value):
                self.states[B.battery]["state"] = value
                self.assertEqual(self.reading().issue is None, allowed)

    def test_zero_output_is_inconclusive_not_a_fault(self):
        self.states[B.output_power]["state"] = "0"
        self.assertIsNone(self.reading().issue)
        self.assertFalse(self.reading().supplying_loads)
        self.states[B.output_power]["state"] = "1"
        self.assertTrue(self.reading().supplying_loads)

    def test_critical_measurement_units_freshness_and_validity(self):
        for entity in (B.battery, B.output_power):
            for bad in ("unavailable", "NaN", "inf", "-1"):
                with self.subTest(entity=entity, bad=bad):
                    self.states = measurements(self.clock)
                    self.states[entity]["state"] = bad
                    self.assertIsNotNone(self.reading().issue)
            self.states = measurements(self.clock)
            self.states[entity]["attributes"]["restored"] = True
            self.assertIsNotNone(self.reading().issue)
            self.states = measurements(self.clock)
            self.states[entity]["attributes"]["unit_of_measurement"] = "V"
            self.assertIsNotNone(self.reading().issue)
        self.states = measurements(self.clock)
        self.states[B.output_power]["state"] = ".15"
        self.states[B.output_power]["attributes"]["unit_of_measurement"] = "kW"
        self.assertEqual(self.reading().output_w, 150)

    def test_meter_loss_is_unknown_not_an_admission_failure(self):
        self.states[B.heater_current]["state"] = "unavailable"
        self.assertIsNone(self.reading().issue)
        self.assertIsNotNone(self.reading().meter_issue)
        self.assertFalse(self.reading().safe_to_rearm)

    def test_dedicated_current_is_not_an_output_ceiling(self):
        self.states[B.heater_current]["state"] = "15"
        self.assertIsNone(self.reading().issue)
        self.assertFalse(self.reading().safe_to_rearm)

    def test_unchanged_ac_report_can_renew_without_state_transition(self):
        original = self.states[B.ac_output_switch]["last_changed"]
        self.clock.advance(181)
        fresh = measurements(self.clock)
        fresh[B.ac_output_switch]["last_changed"] = original
        self.states = fresh
        self.assertIsNone(self.reading().issue)

    def test_corroborating_reports_do_not_claim_to_renew_device_report(self):
        old = self.states[B.ac_output_switch]["attributes"]["device_report"]
        self.clock.advance(181)
        self.states = measurements(self.clock)
        self.states[B.ac_output_switch]["attributes"]["device_report"] = old
        self.assertIsNone(self.reading().issue)
        self.assertTrue(self.reading().ac_enabled)
        self.assertEqual(self.reading().supply_evidence, "corroborating_reports")
        self.assertEqual(self.states[B.ac_output_switch]["attributes"]["device_report"], old)
        self.states[B.output_power]["last_reported"] = old["reported_at"]
        self.assertIsNotNone(self.reading().issue)

    def test_ac_accepts_corroborating_signals_without_physical_snapshot(self):
        for report in (None, {}, {"state": "on", "reported_at": "2026-01-01T00:00:00"}):
            with self.subTest(report=report):
                self.states[B.ac_output_switch]["attributes"]["device_report"] = report
                self.assertIsNone(self.reading().issue)
                self.assertEqual(self.reading().supply_evidence, "corroborating_reports")
        self.states[B.ac_output_switch]["attributes"]["device_report"] = {
            "state": "off", "reported_at": datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()}
        self.assertIsNotNone(self.reading().issue)
        self.assertEqual(self.reading().supply_evidence, "contradictory")
        self.states = measurements(self.clock)
        self.states[B.ac_output_switch]["state"] = "off"
        self.states[B.ac_output_switch]["attributes"]["device_report"]["state"] = "off"
        self.assertIn("disabled", self.reading().issue)

    def test_missing_binding_inhibits_auxiliary_without_invalidating_old_files(self):
        from dataclasses import replace
        binding = replace(B, ac_output_switch=None)
        self.assertIn("AC output", power.evaluate_power(self.states, binding, self.clock.wall).issue)

    def test_guard_has_no_implicit_device_wiring(self):
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), live=True, entry_id="test", notify=lambda: None)
        with self.assertRaises(TypeError):
            power.HeaterPowerGuard(SimpleNamespace(), None, live=True, entry_id="test", notify=lambda: None)


class FakeHass:
    def __init__(self, clock):
        self.clock = clock
        self.data = measurements(clock)
        self.states = SimpleNamespace(get=self.data.get)
        self.calls = []
        self.ignore_climate = False
        self.hang_climate = False
        self.ignore_cancellation = False
        self.release = asyncio.Event()
        self.restore_to_heat = False
        self.restore_overload = False
        self.ignore_switch_on_feedback = False
        self.hang_switch_on = False
        self.release_switch_on = asyncio.Event()
        self.services = SimpleNamespace(async_call=self.service)
        self.tasks = set()
        self.waiting_for_release = {}
        self.tasks_changed = asyncio.Event()

    def async_create_task(self, coroutine, *, eager_start=False):
        # Python 3.12 already schedules asyncio tasks non-eagerly. Accept HA's
        # keyword without passing an unsupported option to asyncio.create_task.
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        self.tasks_changed.set()

        def finished(done):
            self.tasks.discard(done)
            self.tasks_changed.set()

        task.add_done_callback(finished)
        return task

    async def wait_for_release(self, event, *, ignore_cancellation=False):
        """Identify deliberately hung handlers without losing their real task."""
        task = asyncio.current_task()
        self.waiting_for_release[task] = event
        self.tasks_changed.set()
        try:
            try:
                await event.wait()
            except asyncio.CancelledError:
                if not ignore_cancellation:
                    raise
                await event.wait()
        finally:
            self.waiting_for_release.pop(task, None)
            self.tasks_changed.set()

    async def settle(self):
        """Wait for actual guard work and callbacks, not a guessed sleep.

        Deliberately unreleased integration handlers may remain pending after
        the guard's bounded action finishes. They are still tracked and become
        waitable again as soon as the test releases them.
        """
        def runnable():
            return {task for task in self.tasks if not task.done()
                    and not (task in self.waiting_for_release
                             and not self.waiting_for_release[task].is_set())}

        async with asyncio.timeout(5):
            while True:
                self.tasks_changed.clear()
                pending = runnable()
                if not pending:
                    await asyncio.sleep(0)  # Drain task-completion callbacks.
                    if not runnable():
                        return
                    continue
                changed = asyncio.create_task(self.tasks_changed.wait())
                try:
                    await asyncio.wait((*pending, changed), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)

    async def wait_until(self, predicate):
        """Synchronize a race injection with dispatch/progress, without delay."""
        async with asyncio.timeout(5):
            while not predicate():
                self.tasks_changed.clear()
                if not predicate():
                    await self.tasks_changed.wait()

    def set(self, entity, state, attrs=None):
        # Each synthetic device report is a distinct arrival, even when test
        # code performs several writes without yielding the event loop. Keep
        # ordering realistic so a changed value never reuses an old timestamp.
        self.clock.advance(.001)
        old = self.data.get(entity, {})
        if entity in (B.ac_output_switch, B.heater_switch):
            attrs = {**(old.get("attributes", {}) if attrs is None else attrs),
                     "device_report": {"state": str(state),
                       "reported_at": datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()}}
        self.data[entity] = {"state": str(state),
                            "attributes": dict(old.get("attributes", {}) if attrs is None else attrs),
                            "last_changed": old.get("last_changed") if old.get("state") == str(state)
                                else datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat(),
                            "last_reported": datetime.fromtimestamp(self.clock.wall, timezone.utc).isoformat()}

    def refresh(self):
        for entity, old in list(self.data.items()):
            self.set(entity, old["state"], old["attributes"])

    async def service(self, domain, service, body, blocking):
        self.calls.append((domain, service, dict(body)))
        self.tasks_changed.set()
        self.clock.advance(.001)
        entity = body["entity_id"]
        if domain == "climate":
            if self.hang_climate:
                await self.wait_for_release(self.release, ignore_cancellation=self.ignore_cancellation)
            if self.ignore_climate:
                return
            self.set(entity, "off", {"hvac_action": "off"})
        elif service == "turn_off":
            self.set(entity, "off")
            self.set(B.heater, "unavailable", {})
        elif service == "turn_on":
            if self.hang_switch_on:
                await self.wait_for_release(self.release_switch_on)
            if self.ignore_switch_on_feedback:
                return
            self.set(entity, "on")
            self.set(B.heater, "heat" if self.restore_to_heat else "off",
                     {"hvac_action": "heating" if self.restore_to_heat else "off"})
            if self.restore_overload:
                self.set(B.battery, "39")


class PowerGuardAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.hass = FakeHass(self.clock)
        self.guard = power.HeaterPowerGuard(self.hass, B, live=True, entry_id="test", notify=lambda: None,
                                           clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        self.patchers = [patch.object(power, "OFF_ACK_SECONDS", TEST_OFF_ACK_SECONDS),
                         patch.object(power, "RESTORE_ACK_SECONDS", TEST_RESTORE_ACK_SECONDS),
                         patch.object(power, "SERVICE_TIMEOUT_SECONDS", TEST_SERVICE_TIMEOUT_SECONDS)]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        self.hass.release.set()
        self.hass.release_switch_on.set()
        await self.guard.async_close()
        await self.hass.settle()
        for p in self.patchers:
            p.stop()

    async def settle(self):
        await self.hass.settle()

    def trip(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.battery, "40")
        self.guard._observe()

    def healthy(self):
        self.hass.set(B.circuit_power, "140")
        self.hass.set(B.input_power, "130")
        self.hass.set(B.output_power, "150")
        self.hass.set(B.heater_current, ".05")
        self.hass.set(B.battery, "43")
        self.hass.refresh()

    def refresh_sensor_reports(self):
        """Keep legacy sensor packets current without inventing relay feedback."""
        for entity, old in list(self.hass.data.items()):
            if entity.startswith("sensor.") or entity == B.ac_output_switch:
                self.hass.set(entity, old["state"], old["attributes"])

    async def observe_for(self, seconds):
        """Represent ongoing observation instead of jumping over its validity."""
        target = self.clock.now + seconds
        while self.clock.now < target:
            self.clock.advance(min(30, target - self.clock.now))
            self.refresh_sensor_reports()
            self.guard._observe()
            await asyncio.sleep(0)

    async def test_off_ack_avoids_cutting_plug_and_never_touches_network(self):
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.hass.data[B.heater]["state"], "off")
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertEqual([c[0] for c in self.hass.calls], ["climate"])


    async def test_advisory_circuit_cannot_stop_heat_or_inhibit_activation(self):
        for case in ("high", "stale", "future", "restored", "assumed_state",
                     "unavailable", "unknown", "nonfinite", "wrong_unit", "missing"):
            with self.subTest(case=case):
                self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                for elapsed in (0, 15, 15):
                    self.clock.advance(elapsed)
                    for entity, state in list(self.hass.data.items()):
                        if entity != B.circuit_power:
                            self.hass.set(entity, state["state"], state["attributes"])
                    self.guard._observe()
                await self.settle()
                self.assertFalse(self.guard.blocked)
                self.assertFalse(self.guard.diagnostics["latched"])
                self.guard.check_activation(B.heater)
                self.assertEqual(self.hass.data[B.heater]["state"], "heat")
                self.assertEqual(self.hass.calls, [])

    async def test_advisory_circuit_remains_visible_without_enforcement(self):
        for case, status in (("high", "available"), ("stale", "unavailable"),
                             ("unavailable", "unavailable"), ("missing", "unavailable")):
            with self.subTest(case=case):
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                self.guard._observe()
                diagnostics = self.guard.diagnostics
                self.assertFalse(diagnostics["circuit_enforced"])
                wall = diagnostics["wall_power_budget"]
                self.assertEqual(wall["enforced_sources"], [])
                self.assertFalse(wall["sources"]["input"]["enforced"])
                advisory = wall["sources"]["circuit"]
                self.assertEqual(advisory["status"], "advisory")
                self.assertFalse(advisory["enforced"])
                self.assertEqual(advisory["reading_status"], status)
                self.assertEqual(wall["qualified_sources"], [])
                self.assertIn("no watt ceiling", advisory["reason"])


    async def test_advisory_circuit_cannot_block_manual_recovery_acknowledgement(self):
        for case in ("high", "unavailable", "missing"):
            with self.subTest(case=case):
                self.guard._manual_recovery_required = self.guard._identity_mismatch = True
                self.guard._latched = True
                self.guard._tripped_at = self.clock.now - 400
                self.guard._manual_recovery_wall = self.clock.wall
                self.hass.set(B.heater, "off", {"hvac_action": "off"})
                self.hass.set(B.heater_current, ".05")
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                self.assertTrue(await self.guard.async_acknowledge_emergency())
                self.assertFalse(self.guard.diagnostics["manual_recovery_required"])
                self.assertFalse(self.guard.diagnostics["binding_identity_mismatch"])
                self.assertEqual(self.hass.calls, [])

    async def test_advisory_circuit_cannot_block_owned_outlet_restoration(self):
        for case in ("high", "unavailable", "missing"):
            with self.subTest(case=case):
                await self.prepare_restore()
                circuit = advisory_report(self.clock, case)
                if circuit is None:
                    self.hass.data.pop(B.circuit_power, None)
                else:
                    self.hass.data[B.circuit_power] = circuit
                before = sum(call[1] == "turn_on" for call in self.hass.calls)
                self.guard._observe()
                await self.settle()
                self.assertEqual(sum(call[1] == "turn_on" for call in self.hass.calls), before + 1)
                self.assertEqual(self.hass.data[B.heater]["state"], "off")
                self.assertEqual(self.hass.data[B.heater_switch]["state"], "on")
                self.assertFalse(self.guard.blocked)



    async def test_buffered_normal_heating_above_twelve_output_amps_is_not_shed(self):
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.hass.set(B.input_power, "1200")
        self.hass.set(B.circuit_power, "1250")
        self.hass.set(B.output_power, "1800")
        self.hass.set(B.heater_current, "13")
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.assertEqual(self.hass.calls, [])

    async def test_hung_uncooperative_dyson_cannot_delay_dedicated_cutoff(self):
        self.hass.hang_climate = self.hass.ignore_cancellation = True
        self.trip()
        await self.settle()
        self.assertFalse(self.hass.release.is_set())
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertEqual({c[2]["entity_id"] for c in self.hass.calls},
                         {B.heater, B.heater_switch})

    async def test_service_return_without_observed_off_still_cuts_plug(self):
        self.hass.ignore_climate = True
        self.trip()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)


    async def test_recovery_never_turns_on_a_manually_off_plug(self):
        self.hass.set(B.heater_switch, "off")
        self.hass.set(B.battery, "39")
        self.guard._observe()
        await self.settle()
        self.healthy()
        self.guard._observe()
        self.clock.advance(400)
        self.hass.refresh()
        self.guard._observe()
        await self.settle()
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_manual_off_is_physical_isolation_without_restore_ownership(self):
        self.hass.set(B.heater_switch, "off")
        self.hass.set(B.heater, "unavailable", {})
        self.guard._observe()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertFalse(self.guard.switch_cut_owned)
        self.assertTrue(self.guard.blocked)
        self.assertTrue(await self.guard.async_close())
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_late_heat_after_confirmed_stop_cuts_without_second_dyson_wait(self):
        self.trip()
        await self.settle()
        self.hass.set(B.heater, "heat", {"hvac_action": "heating"})
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertEqual(sum(c[0] == "climate" for c in self.hass.calls), 1)

    async def test_output_spike_during_restore_cuts_again_and_keeps_gate_closed(self):
        await self.prepare_restore()
        self.hass.restore_overload = True
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.power_cut_confirmed)
        self.assertTrue(self.guard.blocked)
        self.assertEqual(self.guard.diagnostics["restore_attempts"], 1)

    async def test_shadow_and_close_never_restore_or_send_commands(self):
        self.guard.live = False
        self.trip()
        await self.settle()
        self.assertEqual(self.hass.calls, [])

    async def prepare_restore(self):
        self.hass.ignore_climate = True
        self.trip()
        # Establish the real cutoff before advancing the synthetic clock.
        # A fixed 90 ms sleep can finish first on a loaded test runner.
        await self.settle()
        self.healthy()
        # Fixture-only explicit approval: exercise the bounded restoration
        # mechanics after permission, not unattended fault recovery.
        self.guard._restore_approved_until = self.clock.now + 1000
        self.guard._manual_recovery_required = False
        self.guard._observe()
        # Complete observation up to just before eligibility, then leave the
        # final check to the test so it can install its actual failure mode.
        await self.observe_for(max(0, self.guard._tripped_at + 299 - self.clock.now))
        self.clock.advance(2)
        self.refresh_sensor_reports()
        self.hass.ignore_climate = False

    async def test_service_receipt_without_fresh_plug_on_never_clears_latch(self):
        await self.prepare_restore()
        self.hass.ignore_switch_on_feedback = True
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.blocked)
        self.assertTrue(self.guard.power_cut_confirmed)
        calls = [c[1] for c in self.hass.calls]
        self.assertEqual(calls[-2:], ["turn_on", "turn_off"])

    async def test_late_plug_on_is_tracked_then_superseded_before_close(self):
        await self.prepare_restore()
        self.hass.hang_switch_on = True
        self.guard._observe()
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertFalse(self.guard.power_cut_confirmed)  # Old On is still capable of undoing this Off.
        self.assertTrue(self.guard.diagnostics["power_restore_inflight"])
        self.assertFalse(await self.guard.async_close())
        self.assertFalse(self.guard._closed)
        self.hass.release_switch_on.set()
        await self.settle()
        self.assertEqual(self.hass.data[B.heater_switch]["state"], "off")
        self.assertEqual(self.hass.calls[-1][:2], ("switch", "turn_off"))
        self.assertTrue(await self.guard.async_close())

    async def test_manual_cycle_keeps_explicit_recovery_obligation(self):
        await self.prepare_restore()
        self.clock.advance(1)
        self.hass.set(B.heater_switch, "on")
        self.guard._observe()
        self.clock.advance(1)
        self.hass.set(B.heater_switch, "off")
        self.guard._observe()
        await self.settle()
        self.assertTrue(self.guard.switch_cut_owned)
        self.assertTrue(self.guard._manual_recovery_required)
        self.assertNotIn("turn_on", [c[1] for c in self.hass.calls])

    async def test_realistic_fan_only_load_does_not_block_recovery(self):
        self.hass.set(B.heater_current, ".6")
        self.guard._observe()
        self.assertTrue(self.guard._reading.safe_to_rearm)

    async def test_raw_measurements_do_not_wake_comfort_loop(self):
        self.guard.notify = Mock()
        self.guard._observe()
        self.assertEqual(self.guard.notify.call_count, 1)
        self.hass.set(B.circuit_power, "150")
        self.hass.set(B.battery, "44")
        self.guard._observe()
        self.assertEqual(self.guard.notify.call_count, 1)
        await self.guard.async_close()
        self.assertEqual(self.hass.calls, [])

    async def test_missing_startup_telemetry_inhibits_without_spurious_trip_timer(self):
        self.hass.set(B.output_power, "unavailable")
        self.guard._observe()
        self.assertTrue(self.guard.blocked)
        self.assertFalse(self.guard.diagnostics["latched"])
        self.hass.set(B.output_power, "130")
        self.guard._observe()
        self.assertFalse(self.guard.blocked)
        self.assertEqual(self.hass.calls, [])


if __name__ == "__main__":
    unittest.main()
