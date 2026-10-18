"""Native heartbeat delivery remains safe without a synchronous helper round trip."""
import asyncio
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.house_climate.controller.ha import HAActivationDeferred, HAError
from tests_homeassistant import test_native as fixtures
from tests_homeassistant import test_native_controls as native


class HeartbeatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeTests.asyncSetUp
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    handle = fixtures.NativeTests.handle
    until = fixtures.NativeTests.until
    runtime = native.NativeControlTests.runtime
    claim = native.NativeControlTests.claim
    settle_watchdog = native.NativeControlTests.settle_watchdog

    def controlled_clock(self, instant=100):
        clock = SimpleNamespace(value=instant)
        timer = SimpleNamespace(monotonic=lambda: clock.value)
        self.enterContext(patch("custom_components.house_climate.runtime.time", timer))
        self.enterContext(patch("custom_components.house_climate.controls.time", timer))
        return clock

    def worker_without_yielding(self, operation):
        """Prove the operation completes while HA cannot process callbacks."""
        finished, errors = threading.Event(), []
        def run():
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(.5), "Heartbeat waited for HA's event loop")
        worker.join(.1)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]

    def queue_heartbeat(self, runtime, *, pulses=1):
        """Hold the actual scheduled HA callback, without guessing its internals."""
        def send():
            for _ in range(pulses):
                runtime.api.service(
                    "input_datetime", "set_datetime",
                    entity_id=runtime.config.entity("heartbeat", "input_datetime"),
                    timestamp=123,
                )
        with patch.object(self.hass.loop, "call_soon_threadsafe") as scheduled:
            self.worker_without_yielding(send)
        self.assertEqual(scheduled.call_count, 1)
        callback, *args = scheduled.call_args.args
        return lambda: callback(*args)

    async def test_heartbeat_and_native_status_finish_without_ha_loop_round_trip(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        previous = runtime.controls.heartbeat_monotonic
        def send():
            for _ in range(50):
                runtime.api.service(
                    "input_datetime", "set_datetime",
                    entity_id=runtime.config.entity("heartbeat", "input_datetime"),
                    timestamp=0,
                )
                runtime.api.service(
                    "input_text", "set_value",
                    entity_id=runtime.config.entity("status", "input_text"),
                    value="Requested cool",
                )
        with patch.object(
            self.hass.loop, "call_soon_threadsafe",
            wraps=self.hass.loop.call_soon_threadsafe,
        ) as scheduled:
            self.worker_without_yielding(send)
            self.assertEqual(scheduled.call_count, 1)
            self.assertEqual(runtime.controls.heartbeat_monotonic, previous)
            self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "pending")
        await asyncio.sleep(0)
        self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "confirmed")
        self.assertGreaterEqual(runtime.controls.heartbeat_monotonic, previous)
        self.assertEqual(self.calls, [])
        self.assertEqual(runtime.fault_count, 0)

    async def test_coalescing_never_extends_dispatch_deadline_and_fresh_ack_recovers(self):
        runtime, _ = await self.runtime()
        clock = self.controlled_clock()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        original_lease = runtime.controls.heartbeat_monotonic
        clock.value = 101
        expired_callback = self.queue_heartbeat(runtime, pulses=20)
        clock.value = 106
        self.assertTrue(runtime.api.heartbeat_diagnostics["blocked"])
        with patch.object(self.hass.loop, "call_soon_threadsafe") as scheduled:
            self.worker_without_yielding(lambda: runtime.api.service(
                "input_datetime", "set_datetime",
                entity_id=runtime.config.entity("heartbeat", "input_datetime"), timestamp=999,
            ))
        self.assertEqual(scheduled.call_count, 0)
        self.assertEqual(runtime.api.heartbeat_diagnostics["pending_seconds"], 5)
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(
                runtime.api.service, "climate", "set_temperature",
                entity_id=runtime.config.heads["guest"], hvac_mode="cool", temperature=72,
            )
        self.assertEqual(self.calls, [])
        self.assertEqual(runtime.controls.heartbeat_monotonic, original_lease)
        expired_callback()
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(runtime.controls.heartbeat_monotonic, original_lease)
        clock.value = 107
        fresh_callback = self.queue_heartbeat(runtime)
        expired_callback()  # An old callback cannot consume the replacement.
        self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "pending")
        fresh_callback()
        self.assertFalse(runtime.api.heartbeat_diagnostics["blocked"])
        self.assertEqual(runtime.controls.heartbeat_monotonic, 107)
        await asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=runtime.config.heads["guest"], hvac_mode="cool", temperature=72,
        )
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")
        self.assertEqual(runtime.fault_count, 0)

    async def test_delivery_inside_request_deadline_cannot_revive_expired_ownership(self):
        runtime, _ = await self.runtime()
        clock = self.controlled_clock()
        await self.claim(runtime)
        guest = runtime.config.heads["guest"]
        state = self.hass.states.get(guest)
        self.hass.states.async_set(guest, "cool", dict(state.attributes))
        await self.settle_watchdog(runtime.controls)
        clock.value = 219  # The confirmed ownership lease has one second left.
        callback = self.queue_heartbeat(runtime)
        clock.value = 221  # Still inside this request's five-second dispatch limit.
        callback()  # Deliver before a separately scheduled watchdog can run.
        self.assertEqual(runtime.controls.heartbeat_monotonic, 100)
        self.assertFalse(runtime.controls.enabled)
        await self.settle_watchdog(runtime.controls)
        await runtime.controls.async_check()
        self.assertEqual(self.hass.states.get(guest).state, "off")
        self.assertTrue(all(
            self.hass.states.get(entity).state == "off"
            for entity in runtime.config.climate_entities
        ))
        self.assertIn("heartbeat expired", runtime.controls.hint)
        with self.assertRaises(HAError):
            await asyncio.to_thread(
                runtime.api.service, "climate", "set_temperature",
                entity_id=guest, hvac_mode="cool", temperature=72,
            )

    async def test_old_generation_callback_cannot_renew_or_clear_new_claim_request(self):
        runtime, switch = await self.runtime()
        clock = self.controlled_clock()
        await self.claim(runtime)
        await self.settle_watchdog(runtime.controls)
        old_callback = self.queue_heartbeat(runtime)
        old_generation = runtime.controls.ownership_generation
        await switch.async_turn_off()
        await self.settle_watchdog(runtime.controls)
        self.assertEqual(runtime.controls.owned, ())
        clock.value = 101
        await self.claim(runtime)
        self.assertGreater(runtime.controls.ownership_generation, old_generation)
        current_callback = self.queue_heartbeat(runtime)
        clock.value = 102
        old_callback()
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(runtime.controls.heartbeat_monotonic, 101)
        self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "pending")
        current_callback()
        self.assertEqual(runtime.controls.heartbeat_monotonic, 102)
        self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "confirmed")
        self.assertEqual(self.calls, [])

    async def test_pulse_without_confirmed_claim_cannot_enable_or_own_devices(self):
        runtime, _ = await self.runtime()
        clock = self.controlled_clock()
        await runtime.controls.async_set_enabled(True)
        callback = self.queue_heartbeat(runtime)
        clock.value = 101
        callback()
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(runtime.controls.owned, ())
        self.assertEqual(runtime.controls.heartbeat_monotonic, 0)
        self.assertTrue(runtime.api.heartbeat_diagnostics["blocked"])
        with self.assertRaises(HAActivationDeferred):
            await asyncio.to_thread(
                runtime.api.service, "climate", "set_temperature",
                entity_id=runtime.config.heads["guest"], hvac_mode="cool", temperature=72,
            )
        self.assertEqual(self.calls, [])

    async def test_stop_shadow_and_unrelated_helpers_keep_strict_boundaries(self):
        runtime, _ = await self.runtime()
        await self.claim(runtime)
        runtime.stop_event.set()
        with self.assertRaisesRegex(HAError, "stopping"):
            await asyncio.to_thread(
                runtime.api.service, "input_datetime", "set_datetime",
                entity_id=runtime.config.entity("heartbeat", "input_datetime"), timestamp=0,
            )
        runtime.stop_event.clear()
        runtime.api.live = False
        with self.assertRaisesRegex(HAError, "Shadow"):
            await asyncio.to_thread(
                runtime.api.service, "input_datetime", "set_datetime",
                entity_id=runtime.config.entity("heartbeat", "input_datetime"), timestamp=0,
            )
        runtime.api.live = True
        with patch.object(runtime.api, "_async_request", new=AsyncMock(
            side_effect=HAError("Ordinary bridge failure"),
        )):
            with self.assertRaisesRegex(HAError, "Ordinary bridge failure"):
                await asyncio.to_thread(
                    runtime.api.service, "input_datetime", "set_datetime",
                    entity_id="input_datetime.some_other_helper", timestamp=0,
                )
        self.assertEqual(runtime.api.heartbeat_diagnostics["state"], "idle")
        self.assertEqual(self.calls, [])

    async def test_overdue_heartbeat_defers_retargets_but_worker_safety_and_ack_continue(self):
        runtime, switch = await self.runtime()
        runtime.async_start()
        await self.until(lambda: runtime.status["event"] == "shadow")
        await switch.async_turn_on()
        guest, heater = runtime.config.heads["guest"], runtime.config.heater_entity
        await self.until(lambda: (
            self.hass.states.get(guest).state == "cool"
            and self.hass.states.get(heater).state == "heat"
            and runtime.status.get("phase") == "running"
            and not runtime.status.get("pending_head_commands")
        ))
        old_target = self.hass.states.get(guest).attributes["temperature"]
        baseline = len(self.calls)
        original = runtime.api._async_confirm_heartbeat
        with patch(
            "custom_components.house_climate.runtime.NATIVE_HEARTBEAT_DISPATCH_SECONDS", .05,
        ), patch.object(runtime.api, "_async_confirm_heartbeat", new=Mock()):
            await self.until(lambda: runtime.api.heartbeat_diagnostics["blocked"])
            await runtime.comfort.async_set_target("guest", 73)
            await self.until(lambda: (
                runtime.status.get("room_targets", {}).get("guest", {}).get("target_f") == 73
            ))
            self.assertEqual(self.hass.states.get(guest).state, "cool")
            self.assertEqual(self.hass.states.get(guest).attributes["temperature"], old_target)
            self.assertFalse(runtime.status.get("pending_head_commands"))
            self.assertEqual(runtime.status.get("device_issues"), {})
            self.assertTrue(runtime.controls.enabled)
            self.assertEqual(runtime.fault_count, 0)
            self.assertFalse(any(
                service == "set_hvac_mode" and data.get("hvac_mode") == "off"
                and data["entity_id"] in runtime.config.heads.values()
                for _, service, data in self.calls[baseline:]
            ))
            # The independent HA-loop probe check must still stop actual heat.
            for probe in runtime.config.zones[runtime.config.auxiliary_room].probes:
                self.hass.states.async_set(probe.entity_id, "70", {"unit_of_measurement": "°F"})
            await self.until(lambda: self.hass.states.get(heater).state == "off")
            self.assertEqual(self.hass.states.get(guest).state, "cool")
            self.assertEqual(runtime.fault_count, 0)
        # Discard the held expired pulse, then let the next real callback confirm.
        if runtime.api._heartbeat.request is not None:
            original(runtime.api._heartbeat.request)
        runtime.wake_event.set()
        await self.until(lambda: (
            not runtime.api.heartbeat_diagnostics["blocked"]
            and self.hass.states.get(guest).attributes["temperature"] == 73
            and not runtime.status.get("pending_head_commands")
        ))
        self.assertTrue(runtime.controls.enabled)
        self.assertEqual(runtime.fault_count, 0)
        self.assertEqual(runtime.status["event"], "live")
