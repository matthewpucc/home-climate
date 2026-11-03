"""Bounded shutdown observations; no control decisions or inferred actor identity."""
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from math import isfinite


EVENTS = ('guard_trip', 'guard_requested', 'guard_dispatched', 'guard_completed',
          'guard_failed', 'guard_cancelled', 'guard_native_ack', 'guard_evidence',
          'ha_off_requested', 'ha_off_reported')
METRICS = (*(f'shutdown_{event}_count' for event in EVENTS),
           *(f'shutdown_{event}_at' for event in EVENTS),
           'shutdown_sequence', 'shutdown_observation_errors', 'shutdown_sampled_at',
           'shutdown_isolation_confirmed', 'shutdown_electrical_stop_confirmed',
           'shutdown_dedicated_load_w', 'shutdown_dedicated_load_a', 'shutdown_aggregate_drop_w')
HISTORY_LIMIT = 16


def observational(method):
    @wraps(method)
    def safe(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self.errors += 1
            return None
    return safe


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


class ShutdownDiagnostics:
    def __init__(self):
        self.sequence = self.errors = 0
        self.counts = dict.fromkeys(EVENTS, 0)
        self.times = dict.fromkeys(EVENTS)
        self.history = deque(maxlen=HISTORY_LIMIT)
        self.current = {}
        self.sampled_at = None
        self._signature = None
        self._last_native_ack = None
        self._ha_requests = {}  # At most the three bound target roles; IDs never exported.

    @staticmethod
    def role(bindings, entity):
        return next((role for role, target in (
            ('native_heater', bindings.heater), ('dedicated_relay', bindings.heater_switch),
            ('shared_ac', getattr(bindings, 'emergency_ac_switch', None)))
            if target is not None and target == entity), None)

    @observational
    def record(self, event, *, at=None, initiator='home_climate_guard', **detail):
        if detail.get('reason') is not None:
            detail['reason'] = str(detail['reason'])[:256]
        self.sequence += 1
        self.counts[event] += 1
        stamp = datetime.now(timezone.utc).timestamp() if at is None else at
        self.times[event] = stamp
        row = {'sequence': self.sequence, 'event': event, 'at': stamp,
               'initiator': initiator, **detail}
        self.history.append(row)
        return row

    @observational
    def request(self, guard, domain, service, entity, data):
        role = self.role(guard.bindings, entity)
        if role is None or not ((domain == 'switch' and service == 'turn_off')
                or (domain == 'climate' and service == 'set_hvac_mode' and data.get('hvac_mode') == 'off')):
            return None
        return self.record('guard_requested', at=guard._wall_clock(), target=role,
                           reason=str(guard._reason)[:256] if guard._reason else None,
                           stage=guard._shutdown_stage, caller='unknown')

    @observational
    def command(self, event, request):
        if request is not None:
            return self.record(event, request=request['sequence'], target=request['target'],
                               reason=request['reason'], stage=request['stage'], caller='unknown')
        return None

    @observational
    def native_ack(self, guard):
        boundary = guard._off_request_wall
        if boundary == self._last_native_ack:
            return
        self._last_native_ack = boundary
        self.record('guard_native_ack', at=guard._wall_clock(), target='native_heater',
                    request_at=boundary, proof='ha_state', service_completion='independent')

    @observational
    def capture(self, guard, current):
        """Consume the result already calculated by guard publication, never re-evaluate it."""
        aggregate = current.get('aggregate_stop') or {}
        watts, amps = number(current.get('heater_power_w')), number(current.get('heater_current_a'))
        now, observed_at = guard._wall_clock(), guard._last_observation_wall
        reading_fresh = observed_at is not None and 0 <= now - observed_at <= 30
        meter_known = reading_fresh and not current.get('heater_meter_issue') and (watts is not None or amps is not None)
        baseline, latest = number(aggregate.get('baseline_w')), number(aggregate.get('latest_w'))
        drop_baseline = (number(aggregate.get('qualified_baseline_w'))
                         if aggregate.get('classification') == 'relative_drop' else baseline)
        drop = (drop_baseline - latest if reading_fresh and aggregate.get('status') == 'corroborated'
                and aggregate.get('classification') in ('drop', 'relative_drop')
                and drop_baseline is not None and latest is not None else None)
        self.sampled_at = now
        self.current = {
            'isolation_confirmed': int(bool(current.get('power_cut_confirmed'))),
            'electrical_stop_confirmed': int(bool(current.get('electrical_stop_confirmed'))) if meter_known else None,
            'dedicated_load_w': watts if meter_known else None,
            'dedicated_load_a': amps if meter_known else None,
            'aggregate_drop_w': drop,
        }
        signature = (current.get('shutdown_evidence'), current.get('shutdown_confirmation'),
                     self.current['isolation_confirmed'], self.current['electrical_stop_confirmed'],
                     aggregate.get('status'), aggregate.get('classification'))
        if signature != self._signature:
            self._signature = signature
            self.record('guard_evidence', at=self.sampled_at, initiator='unknown',
                proof=current.get('shutdown_evidence'), confirmation=current.get('shutdown_confirmation'),
                native_request_at=guard._off_request_wall, relay_request_at=guard._cut_request_wall,
                ac_request_at=guard._emergency_ac_request_wall,
                electrical=dict(self.current), aggregate_status=aggregate.get('status'),
                aggregate_classification=aggregate.get('classification'),
                aggregate_baseline_w=baseline, aggregate_latest_w=latest,
                aggregate_qualified_baseline_w=number(aggregate.get('qualified_baseline_w')),
                aggregate_evidence_seconds=number(aggregate.get('evidence_seconds')),
                causality='not_established')

    @observational
    def ha_request(self, event, bindings):
        data = event.data
        domain, service = data.get('domain'), data.get('service')
        body = data.get('service_data') or {}
        if not ((domain in ('switch', 'climate', 'homeassistant') and service == 'turn_off')
                or (domain == 'climate' and service == 'set_hvac_mode' and body.get('hvac_mode') == 'off')):
            return
        entities = body.get('entity_id', ())
        if isinstance(entities, str):
            entities = (entities,)
        for entity in dict.fromkeys(entities):
            if (role := self.role(bindings, entity)) is None:
                continue
            row = self.record('ha_off_requested', at=event.time_fired.timestamp(), initiator='unknown',
                              target=role, reason=None, outcome='unknown', emergency_intent='unknown',
                              user_context_present=bool(event.context.user_id))
            if row is not None:
                self._ha_requests[role] = (row['sequence'], row['at'], event.context.id)

    @observational
    def ha_state(self, event, bindings):
        role = self.role(bindings, event.data.get('entity_id'))
        request = self._ha_requests.get(role)
        state = event.data.get('new_state')
        if role is None or state is None or state.state != 'off':
            return
        stamp = state.last_reported.timestamp()
        now = datetime.now(timezone.utc).timestamp()
        if (not 0 <= now - stamp <= 180
                or state.attributes.get('restored') or state.attributes.get('assumed_state')
                or state.attributes.get('hvac_action') in ('heating', 'cooling')):
            return
        if request is not None and not 0 <= now - request[1] <= 180:
            self._ha_requests.pop(role, None)
            request = None
        matched = request is not None and request[1] < stamp
        old = event.data.get('old_state')
        if not matched and old is not None and old.state == 'off':
            return
        self.record('ha_off_reported', initiator='unknown', target=role,
                    request=request[0] if matched else None, request_at=request[1] if matched else None,
                    report_at=stamp, proof='ha_state', service_completion='unknown',
                    context_matches_request=bool(request[2] and request[2] == state.context.id) if matched else None,
                    causality='not_established')
        if matched:
            self._ha_requests.pop(role, None)

    def values(self, now):
        result = {f'shutdown_{event}_count': count for event, count in self.counts.items()}
        result.update({f'shutdown_{event}_at': stamp for event, stamp in self.times.items()})
        result.update(shutdown_sequence=self.sequence, shutdown_observation_errors=self.errors,
                      shutdown_sampled_at=self.sampled_at)
        fresh = self.sampled_at is not None and 0 <= now - self.sampled_at <= 30
        for key in ('isolation_confirmed', 'electrical_stop_confirmed', 'dedicated_load_w',
                    'dedicated_load_a', 'aggregate_drop_w'):
            result[f'shutdown_{key}'] = self.current.get(key) if fresh else None
        return result, {'schema_version': 1, 'snapshot_fresh': fresh,
                        'last_event': dict(self.history[-1]) if self.history else None,
                        'external_initiator_attribution': 'unknown'}

    def download(self):
        return {'schema_version': 1, 'history_limit': HISTORY_LIMIT, 'sequence': self.sequence,
                'counts': dict(self.counts), 'last_occurrence': dict(self.times),
                'observation_errors': self.errors, 'sampled_at': self.sampled_at,
                'recent_events': [dict(row) for row in self.history]}
