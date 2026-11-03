"""Delayed native Off feedback must settle the stop before lease expiry."""
import asyncio
import unittest

from tests_homeassistant import test_heartbeat_delivery as heartbeat


class AuxiliaryDelayedOffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = heartbeat.HeartbeatDeliveryTests.asyncSetUp
    asyncTearDown = heartbeat.HeartbeatDeliveryTests.asyncTearDown
    handle = heartbeat.HeartbeatDeliveryTests.handle
    until = heartbeat.HeartbeatDeliveryTests.until
    runtime = heartbeat.HeartbeatDeliveryTests.runtime
    claim = heartbeat.HeartbeatDeliveryTests.claim
    settle_watchdog = heartbeat.HeartbeatDeliveryTests.settle_watchdog
    timed_runtime = heartbeat.HeartbeatDeliveryTests.timed_runtime
    controlled_clock = heartbeat.HeartbeatDeliveryTests.controlled_clock
    set_device_mode = heartbeat.HeartbeatDeliveryTests.set_device_mode

    async def delayed_off(self):
        runtime, _ = await self.timed_runtime()
        clock = self.controlled_clock()
        await self.claim(runtime)
        c = runtime.controls
        await self.settle_watchdog(c)
        c._schedule_check = lambda *_: None
        heater = runtime.config.heater_entity
        await asyncio.to_thread(
            runtime.api.service, "climate", "set_temperature",
            entity_id=heater, hvac_mode="heat", temperature=86,
        )
        guest = runtime.config.heads["guest"]
        self.set_device_mode(guest, "cool")

        async def completed_without_report(call):
            if call.data.get("entity_id") == heater and call.data.get("hvac_mode") == "off":
                self.calls.append((call.domain, call.service, dict(call.data)))
                return
            await self.handle(call)

        self.hass.services.async_register("climate", "set_hvac_mode", completed_without_report)
        clock.value = 150
        await asyncio.to_thread(
            runtime.api.service, "climate", "set_hvac_mode",
            entity_id=heater, hvac_mode="off",
        )
        self.assertTrue(c._auxiliary_heartbeat_off_receipt)
        self.assertTrue(c._auxiliary_heat_possible)
        self.assertFalse(c._auxiliary_heartbeat_off_ack)
        self.assertFalse(c.pending_device_jobs({heater}))
        self.hass.services.async_register("climate", "set_hvac_mode", self.handle)
        return runtime, clock

    async def test_delayed_off_is_reconciled_before_watchdog_expiry(self):
        runtime, clock = await self.delayed_off()
        c = runtime.controls
        authorized = (c.auxiliary_authorized_at, c.auxiliary_authorized_wall)
        epoch = c._auxiliary_stop_epoch
        clock.value = 151
        self.set_device_mode(runtime.config.heater_entity, "off")
        baseline = list(self.calls)
        # The next watchdog read crosses the lease deadline. The completed Off
        # and subsequent report already suffice to settle this stop.
        clock.value = 221
        await c.async_check()
        self.assertFalse(c.auxiliary_heartbeat_blocked)
        self.assertFalse(c._auxiliary_heat_possible)
        self.assertTrue(c._auxiliary_heartbeat_off_ack)
        self.assertEqual(c._auxiliary_stop_epoch, epoch)
        self.assertEqual((c.auxiliary_authorized_at, c.auxiliary_authorized_wall), authorized)
        self.assertEqual(self.calls, baseline)
        self.assertTrue(c.enabled)
        self.assertEqual(self.hass.states.get(runtime.config.heads["guest"]).state, "cool")
