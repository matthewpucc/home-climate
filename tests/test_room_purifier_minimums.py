"""Per-room floors retain automatic boosts and exact overrides."""
from dataclasses import replace
from datetime import timedelta
import unittest

from house_climate.bindings import HouseBindings, example_bindings
from house_climate.cadence import DecisionCadence
from house_climate.config import Config, Settings
from house_climate.house_policy import editable_policy_settings, house_configuration
from house_climate.policy import Policy
from house_climate.executor import Executor
from tests.test_controller import FakeHA
from tests.test_directed_airflow import directed_config, FAN
from tests.test_rooms import configuration, states, NOW


class RoomPurifierMinimumTests(unittest.TestCase):
    def test_house_minimums_roundtrip_roles_and_global_fallback(self):
        bindings = example_bindings()
        previous = house_configuration(bindings=bindings).to_dict()
        previous['rooms']['saved_office'] = previous['rooms'].pop('office')
        cfg = house_configuration(previous, bindings, policy_settings={
            'purifiers': {'room_minimums': {'primary': 0, 'office': 35}}})
        self.assertEqual(cfg.fan_controls[bindings.fans['primary']].minimum_percentage, 0)
        self.assertEqual(cfg.fan_controls[bindings.fans['office']].room, 'saved_office')
        self.assertEqual(cfg.fan_controls[bindings.fans['office']].minimum_percentage, 35)
        self.assertEqual(cfg.fan_controls[bindings.fans['guest']].minimum_percentage, 20)
        saved = editable_policy_settings(cfg, bindings)
        restored = house_configuration(previous, bindings, policy_settings=saved)
        self.assertEqual(restored.fan_controls, cfg.fan_controls)
        self.assertEqual(Config.from_dict(cfg.to_dict()).fan_controls, cfg.fan_controls)
        saved['purifiers']['room_minimums'] = {}
        cleared = house_configuration(previous, bindings, policy_settings=saved)
        self.assertTrue(all(f.minimum_percentage == 20 for f in cleared.fan_controls.values()))

    def test_invalid_room_minimums_rejected(self):
        for mapping in (None, [], 70, {'unknown': 20}, {'Office': 20},
                        *({'primary': value} for value in
                          (None, True, '20', -1, 101, float('nan'), float('inf')))):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                house_configuration(bindings=example_bindings(), policy_settings={
                    'purifiers': {'room_minimums': mapping}})

    def test_living_always_policy_is_not_lowered_by_room_floor(self):
        raw = example_bindings().to_dict()
        raw['fans']['living'] = 'fan.fixture_living'
        cfg = house_configuration(bindings=HouseBindings.from_dict(raw), policy_settings={
            'purifiers': {'room_minimums': {'living': 20}}})
        fan = cfg.fan_controls['fan.fixture_living']
        self.assertEqual(fan.percentage, 100)
        self.assertEqual(fan.requested_percentage(fan.percentage), 100)
        self.assertEqual(replace(fan, speed_override=30).requested_percentage(100), 30)

    def test_live_floor_change_invalidates_cadence_and_preserves_schedule_override(self):
        raw = configuration()
        raw['rooms']['alpha']['fans'][0].update(strategy='schedule', minimum_percentage=20)
        raw['rooms']['alpha']['fans'][0].pop('source_room', None)
        raw['rooms']['alpha']['profile']['fan_percentage'] = 60
        cfg = Config.from_dict(raw)
        entity = next(iter(cfg.fan_controls))
        settings = Settings.read({}, cfg)
        cadence = DecisionCadence(cfg, Policy(cfg), clock=lambda: 0)
        data = states(cfg)
        for tick, floor, override, expected in ((0, 0, {}, 60), (1, 80, {}, 80),
                                               (2, 0, {'alpha': 10}, 10),
                                               (3, 0, {'alpha': None}, 60)):
            selected = replace(settings, room_fan_minimums={'alpha': floor}, room_fan_overrides=override)
            plan = cadence.decide(data, NOW + timedelta(seconds=tick), selected, monotonic_now=tick)
            self.assertEqual(plan.fans[entity], expected)
            self.assertEqual(cfg.fan_controls[entity].minimum_percentage, 20)

    def test_live_floor_reaches_actual_device_with_supported_rung_rounding(self):
        raw = configuration()
        raw['rooms']['alpha']['fans'][0].update(strategy='schedule', minimum_percentage=20)
        raw['rooms']['alpha']['fans'][0].pop('source_room', None)
        raw['rooms']['alpha']['profile']['fan_percentage'] = 0
        cfg = Config.from_dict(raw)
        for floor, expected in ((0, 0), (10, 10), (75, 80)):
            with self.subTest(floor=floor):
                data = states(cfg)
                data[FAN]['attributes']['percentage_step'] = 10
                settings = replace(Settings.read({}, cfg), room_fan_minimums={'alpha': floor})
                plan = Policy(cfg).decide(data, NOW, settings)
                ha = FakeHA(data)
                Executor(cfg, ha, '°F').step(plan, ha.states(), 0)
                self.assertEqual(ha.data[FAN]['state'], 'on' if expected else 'off')
                if expected:
                    self.assertEqual(ha.data[FAN]['attributes']['percentage'], expected)

    def test_directed_missing_adapter_fallback_uses_effective_floor(self):
        cfg = Config.from_dict(directed_config())
        for floor, expected in ((0, 0), (10, 10), (75, 80)):
            with self.subTest(floor=floor):
                data = states(cfg, {'alpha': 73, 'beta': 72, 'gamma': 68})
                data[FAN]['attributes']['percentage_step'] = 10
                settings = replace(Settings.read({}, cfg), room_fan_minimums={'alpha': floor})
                plan = Policy(cfg).decide(data, NOW, settings)
                ha = FakeHA(data)
                executor = Executor(cfg, ha, '°F')
                for tick in (0, 15, 30):
                    executor.step(plan, ha.states(), tick)
                self.assertEqual(ha.data[FAN]['state'], 'on' if expected else 'off')
                self.assertEqual(plan.fan_airflow[FAN]['execution']['effective_percentage'], floor)
                if expected:
                    self.assertEqual(ha.data[FAN]['attributes']['percentage'], expected)


if __name__ == '__main__':
    unittest.main()
