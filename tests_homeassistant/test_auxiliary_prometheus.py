"""Use HA's real sensor serialization, exporter and /api/prometheus renderer."""
from datetime import timedelta
from functools import partial
import logging
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

import prometheus_client
from prometheus_client.parser import text_string_to_metric_families
from homeassistant.components import prometheus
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import EVENT_STATE_CHANGED
from homeassistant.helpers import area_registry as ar, floor_registry as fr
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.entity_values import EntityValues

from custom_components.house_climate.const import DOMAIN
from custom_components.house_climate.controls import NativeControls
from custom_components.house_climate.sensor import AuxiliaryDiagnostic, AUXILIARY_METRICS, async_setup_entry
from tests_homeassistant import test_auxiliary_diagnostics as diagnostic_tests


class AuxiliaryPrometheusTests(unittest.IsolatedAsyncioTestCase):
    handle = diagnostic_tests.AuxiliaryDiagnosticTests.handle
    until = diagnostic_tests.AuxiliaryDiagnosticTests.until
    runtime = diagnostic_tests.AuxiliaryDiagnosticTests.runtime
    claim = diagnostic_tests.AuxiliaryDiagnosticTests.claim
    settle_watchdog = diagnostic_tests.AuxiliaryDiagnosticTests.settle_watchdog
    feedback = diagnostic_tests.AuxiliaryDiagnosticTests.feedback

    async def asyncSetUp(self):
        await diagnostic_tests.AuxiliaryDiagnosticTests.asyncSetUp(self)
        self.controller, _ = await self.runtime()
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.entry = ConfigEntry(
            version=3, minor_version=1, domain=DOMAIN, entry_id='native',
            title='Home Climate', source='user', data={}, options={}, unique_id=None,
            discovery_keys=MappingProxyType({}), subentries_data=(),
        )
        self.entry.runtime_data = self.controller
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        added = []
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.sensors = {entity.key: entity for entity in added if isinstance(entity, AuxiliaryDiagnostic)}
        self.assertEqual(set(self.sensors), set(AUXILIARY_METRICS))
        for key, sensor in self.sensors.items():
            sensor.entity_id = f'sensor.home_climate_auxiliary_{key}'
        self.group = self.sensors['sequence']._diagnostic_group
        self.platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__), domain='sensor',
            platform_name=DOMAIN, platform=None, scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        self.platform.config_entry = self.entry
        await self.platform.async_add_entities(list(self.sensors.values()))
        await ar.async_load(self.hass)
        await fr.async_load(self.hass)
        registry = prometheus_client.CollectorRegistry()
        self.enterContext(patch.object(prometheus_client, 'REGISTRY', registry))
        for kind in ('Counter', 'Gauge'):
            self.enterContext(patch.object(prometheus_client, kind,
                                          partial(getattr(prometheus_client, kind), registry=registry)))
        self.exporter = prometheus.PrometheusMetrics(
            lambda entity_id: entity_id.startswith('sensor.home_climate_auxiliary_'),
            'homeassistant', self.hass.config.units.temperature_unit, EntityValues(), None, None,
            ar.async_get(self.hass), dr.async_get(self.hass), er.async_get(self.hass), fr.async_get(self.hass),
        )
        self.unsubscribe = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.exporter.handle_state_changed_event)
        for sensor in self.sensors.values():
            self.exporter.handle_state(self.hass.states.get(sensor.entity_id))

    async def asyncTearDown(self):
        self.unsubscribe()
        await self.platform.async_reset()
        await diagnostic_tests.AuxiliaryDiagnosticTests.asyncTearDown(self)

    async def scrape(self):
        self.group.refresh()
        await self.hass.async_block_till_done()
        response = await prometheus.PrometheusView(True).get(SimpleNamespace(app={prometheus.KEY_HASS: self.hass}))
        self.assertEqual(response.status, 200)
        samples = [sample for family in text_string_to_metric_families(response.text)
                   for sample in family.samples]
        return samples, response.text

    def sample(self, samples, key, metric='homeassistant_sensor_state'):
        return [sample for sample in samples if sample.name == metric
                and sample.labels.get('entity') == self.sensors[key].entity_id]

    async def test_actual_export_contains_every_numeric_sensor_and_timestamp(self):
        c = self.controller.controls
        await self.claim(self.controller)
        # Several renewals between scrapes retain their count.
        for _ in range(3):
            c.confirm_auxiliary_authorization()
        samples, text = await self.scrape()
        self.assertEqual(self.sample(samples, 'authorization_renewed_count')[0].value, 4)
        self.assertEqual(self.sample(samples, 'authorization_valid')[0].value, 1)
        self.assertEqual(self.sample(samples, 'ownership_retained')[0].value, 1)
        renewed = self.sample(samples, 'authorization_renewed_at', 'homeassistant_sensor_unit_s')
        self.assertAlmostEqual(renewed[0].value, c.auxiliary_authorized_wall, places=5)
        for key in AUXILIARY_METRICS:
            value = self.group.values[key]
            metric = 'homeassistant_sensor_unit_s' if key.endswith('_at') else 'homeassistant_sensor_state'
            series = self.sample(samples, key, metric)
            if value is None:
                self.assertEqual(series, [], key)
            else:
                self.assertAlmostEqual(series[0].value, value, places=5, msg=key)
                self.assertEqual(set(series[0].labels), {'entity', 'friendly_name', 'domain'})
        self.assertNotIn('authorization_renewed{', text)  # Attributes aren't fabricated metrics.
        self.assertNotIn('request_generation=', text)
        self.assertNotIn('input_', text)
        self.assertLess(len(str(self.hass.states.get(self.sensors['sequence'].entity_id).attributes)), 1500)
        self.assertFalse(any('recent_events' in self.hass.states.get(sensor.entity_id).attributes
                             for sensor in self.sensors.values()))

    async def test_stale_restored_unknown_unavailable_remove_prior_exported_value(self):
        samples, _ = await self.scrape()
        self.assertEqual(self.sample(samples, 'reported_off')[0].value, 1)
        reported = self.hass.states.get(self.controller.config.heater_entity).last_reported
        for kwargs in ({'reported': reported - timedelta(seconds=181)},
                       {'attributes': {'restored': True}}, {'attributes': {'assumed_state': True}},
                       {'state': 'unknown'}, {'state': 'unavailable'}):
            with self.subTest(kwargs=kwargs), self.feedback(self.controller, **kwargs):
                samples, _ = await self.scrape()
                self.assertEqual(self.hass.states.get(self.sensors['reported_off'].entity_id).state, 'unknown')
                self.assertEqual(self.sample(samples, 'reported_off'), [])
                self.assertEqual(self.sample(samples, 'reported_off', 'homeassistant_entity_available')[0].value, 0)
            samples, _ = await self.scrape()
            self.assertEqual(self.sample(samples, 'reported_off')[0].value, 1)
        sensor = self.sensors['reported_off']
        sensor._attr_available = False
        samples, _ = await self.scrape()
        self.assertEqual(self.hass.states.get(sensor.entity_id).state, 'unavailable')
        self.assertEqual(self.sample(samples, 'reported_off'), [])

    async def test_unload_cleans_up_the_single_refresh_subscription(self):
        self.assertEqual(len(self.group.unsubscribers), 2)
        await self.platform.async_reset()
        self.assertEqual(self.group.listeners, set())
        self.assertEqual(self.group.unsubscribers, [])

    async def test_new_session_removes_old_event_timestamp_series(self):
        await self.claim(self.controller)
        samples, _ = await self.scrape()
        self.assertTrue(self.sample(samples, 'authorization_renewed_at', 'homeassistant_sensor_unit_s'))
        restarted = NativeControls(self.hass, self.controller.config, True, 'native', lambda: None)
        await restarted.async_load()
        self.group.controls = restarted
        samples, _ = await self.scrape()
        self.assertEqual(self.sample(samples, 'authorization_renewed_at', 'homeassistant_sensor_unit_s'), [])
        self.assertEqual(self.sample(samples, 'authorization_renewed_count')[0].value, 0)
        self.assertEqual(self.sample(samples, 'ownership_retained')[0].value, 1)
        self.assertEqual(self.sample(samples, 'authorization_valid'), [])
