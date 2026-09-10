"""Shadow remaining-duration forecasts from existing Hermes execution callbacks.

These measure first accepted API-start observation to observed turn terminal,
not queue time, independent task count, output quality, or complete model usage.
No forecast governs work. Missing callbacks remain missing evidence.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os

from .expectations import expectations_enabled
from .runtime_forecasts import _current, _digest

VERSION = 'execution-duration-observation-v1'
MEASUREMENT = 'first_api_observation_to_terminal'
PRIOR_SECONDS = 480.0
MAX_REQUESTS = 128
logger = logging.getLogger(__name__)


def accumulate(old, value, previous, now):
    """Called in the registry transaction; bounded metadata, no model calls."""
    data = old or {'version': VERSION, 'start_observed': previous is None
        and value['sequence'] == 1 and value['phase'] == 'turn'
        and value['state'] == 'observed', 'requests': {}, 'incomplete': False}
    if previous and value['sequence'] != previous['sequence'] + 1:
        data['incomplete'] = True
    event = value.get('runtime') or {}
    request_id = event.get('request_id')
    if event:
        if not request_id or (request_id not in data['requests'] and len(data['requests']) >= MAX_REQUESTS):
            data['incomplete'] = True
        else:
            pair = data['requests'].setdefault(request_id, {})
            phase = event['event']
            if phase in pair and pair[phase] != event:
                data['incomplete'] = True
            pair[phase] = event
        if 'first_api' not in data:
            data['first_api'] = {'observed_at': now, 'sequence': value['sequence'], 'event': event,
                'eligible_start': bool(data['start_observed'] and not data['incomplete']
                    and event.get('event') == 'start' and event.get('api_call_count') == 1
                    and event.get('retry_count') == 0 and request_id
                    and event.get('requested_model') and event.get('provider'))}
    return data


def _parts(registry, owner):
    from colony_sidecar.api.routers import host
    configured_owner = os.environ.get('COLONY_OWNER_PERSON_ID', '').strip() or os.environ.get('COLONY_OWNER_CONTACT_ID', '').strip()
    if owner != configured_owner or not configured_owner or not expectations_enabled() or host._expectations is None:
        return None
    return host._expectations.store, registry.ledger


def _read(registry, execution_id):
    with closing(registry.ledger._connect()) as db:
        row = db.execute('SELECT e.*,r.metadata_json FROM execution_observations e '
            'JOIN execution_runtime_observations r USING(execution_id) WHERE execution_id=?', (execution_id,)).fetchone()
    return (dict(row), json.loads(row['metadata_json'])) if row else (None, {})


ROUTING_KEYS = ('requested_model', 'provider', 'api_mode', 'profile_id', 'runtime_kind')


def _config(event):
    size = event.get('approx_input_tokens')
    bucket = ('up_to_4k' if size <= 4096 else '4k_to_16k' if size <= 16384
              else '16k_to_64k' if size <= 65536 else 'over_64k') if type(size) is int else 'unknown'
    return {**{key: event.get(key) or '' for key in ROUTING_KEYS},
        'input_bucket': bucket, 'max_tokens': event.get('max_tokens'), 'tool_count': event.get('tool_count')}


def _known(config):
    return (all(config.get(key) and config[key] != 'unknown' for key in (*ROUTING_KEYS, 'input_bucket'))
            and type(config.get('max_tokens')) is int and config['max_tokens'] > 0
            and type(config.get('tool_count')) is int)



def _processor(data, config):
    requests = data['requests']
    complete = bool(requests) and not data['incomplete']
    configuration_matches = _known(config)
    served = set()
    errors = 0
    for pair in requests.values():
        start = pair.get('start')
        terminal = pair.get('response') or pair.get('error')
        complete &= bool(start and terminal and not (pair.get('response') and pair.get('error')))
        for event in pair.values():
            configuration_matches &= all(event.get(key) == config[key] for key in ROUTING_KEYS)
        if start:
            configuration_matches &= _config(start) == config
        response = pair.get('response')
        if response:
            if response.get('response_model'):
                served.add(response['response_model'])
            else:
                complete = False
        errors += bool(pair.get('error'))
    # A response model is a provider-reported label, not weight attestation.
    # Aliases and fallback never borrow the requested label. The original
    # forecast separately freezes the historical served-model stratum.
    return {'served_models': sorted(served), 'served_model': next(iter(served)) if len(served) == 1 else None,
        'complete_observed_pairs': bool(complete), 'observed_request_count': len(requests),
        'error_request_count': errors, 'configuration': config,
        'conditions_comparable': bool(complete and configuration_matches and len(served) == 1),
        'configuration_matches': bool(configuration_matches),
        'coverage': 'registered callback pairs only; auxiliary or dropped hooks may be absent'}


def _receipt(ledger, owner, execution_id, kind, facts, occurred_at):
    source = 'execution-runtime:'+_digest({'execution_id': execution_id, 'kind': kind})
    # Empty content intentionally produces zero lexical/vector chunks. Runtime
    # facts remain exact canonical source metadata, not conversational memory.
    with closing(ledger._connect()) as db:
        previous = db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (source,)).fetchone()
    if previous:
        retained = json.loads(previous[0])[0].get('_execution_runtime_facts', {})
        if (retained.get('version'), retained.get('execution_id'), retained.get('kind')) != (VERSION, execution_id, facts['kind']):
            raise ValueError('execution evidence binding conflict')
        facts = retained
    else:
        ledger.record_source(source, contact_id=owner, session_id='execution-runtime:'+execution_id,
            occurred_at=datetime.fromtimestamp(occurred_at, timezone.utc).isoformat(),
            messages=[{'role': 'assistant', 'content': '', '_execution_runtime_facts': facts}], derive_claims=False)
    refs = ledger.source_references([source], contact_id=owner, session_id='')
    if not refs or ledger.is_projection_erased(source):
        raise ValueError('execution evidence unavailable')
    return {'receipt:'+source: refs[0]['source_version']}, facts


def _facts(ledger, outcome):
    with closing(ledger._connect()) as db:
        row = db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                         (outcome['receipt_ref'].removeprefix('receipt:'),)).fetchone()
    return json.loads(row[0])[0].get('_execution_runtime_facts', {}) if row else {}


def _fid(execution_id):
    return 'execution-duration:'+execution_id


def observe(registry, execution_id, owner):
    parts = _parts(registry, owner)
    if parts is None:
        return None
    store, ledger = parts
    row, data = _read(registry, execution_id)
    if row is None or row['contact_id'] != owner:
        return None
    first = data.get('first_api', {})
    history = store.forecast_history(_fid(execution_id))
    if not history['forecasts']:
        # Never issue from a terminal callback, a late first response, or a
        # preexisting execution whose sequence-1 start was not observed.
        if (row['state'] != 'observed' or not first.get('eligible_start')
                or row['sequence'] != first['sequence'] or row['phase'] != 'model'):
            return {'status': 'no_prospective_forecast', 'suggestion_enabled': False}
        config = _config(first['event'])
        cohort = 'hermes-execution:'+row['platform']+(':'+ 'child' if row['parent_execution_id'] else ':root')+':'+_digest(config)[:16]
        training_model = [None]
        def current(prediction, outcome):
            facts = _facts(ledger, outcome)
            processor = facts.get('processor', {})
            eligible = (_known(config) and prediction.detail['model_provenance']['capabilities'] == config
                and facts.get('version') == VERSION and facts.get('observation_eligible') is True
                and outcome['status'] == 'observed' and outcome['value'] is True
                and processor.get('configuration') == config
                and _current(ledger, prediction.evidence_refs, prediction.detail['source_versions'], owner)
                and _current(ledger, outcome['evidence_refs'], outcome['source_versions'], owner))
            if not eligible:
                return False
            # The estimator visits newest eligible outcomes first. Freeze one
            # historical actual processor, not an assumed requested alias.
            if training_model[0] is None:
                training_model[0] = processor['served_model']
            return processor['served_model'] == training_model[0]
        now = registry.clock()
        estimate = store.estimate_duration(domain='task_duration', cohort=cohort, subject_person_id=owner,
            viewer_scope='owner', prior_seconds=PRIOR_SECONDS, now=now, evidence_is_current=current)
        estimate['training_served_model'] = training_model[0]
        # Origin is server receipt time, not the host's earlier callback clock.
        origin = first['observed_at']
        if origin + estimate['seconds'] <= now:
            return {'status': 'observation_too_old', 'suggestion_enabled': False}
        facts = {'version': VERSION, 'kind': 'prospective', 'execution_id': execution_id,
            'measurement': MEASUREMENT, 'configuration': config, 'estimate': estimate,
            'first_api_observed_at': origin, 'issued_at': now, 'parent_execution_id': row['parent_execution_id']}
        versions, retained = _receipt(ledger, owner, execution_id, 'prospective', facts, now)
        estimate, config = retained['estimate'], retained['configuration']
        now = registry.clock()
        if origin + estimate['seconds'] <= now:
            return {'status': 'observation_too_old', 'suggestion_enabled': False}
        store.issue_forecast(forecast_id=_fid(execution_id), subject='execution:'+execution_id,
            domain='task_duration', expectation='The observed Hermes execution reaches completion within its remaining-duration estimate',
            confidence=estimate['confidence'], horizon=origin+estimate['seconds'], origin_at=origin, issued_at=now,
            evidence_refs=list(versions), source_versions=versions, source_kind='task_receipt', cohort=cohort,
            method=estimate['method'], model_provenance={'requested_role': None, 'served_model': None,
                'model_revision': None, 'capabilities': config, 'fallback': None},
            subject_person_id=owner, viewer_scope='owner', shareability='owner_private',
            conditions={'measurement': MEASUREMENT, 'estimate': estimate, 'prior_is_slo': False})
        return {'status': 'issued', 'forecast_id': _fid(execution_id), 'suggestion_enabled': False}
    if row['state'] == 'observed' or history['outcomes']:
        return {'status': 'pending' if not history['outcomes'] else 'observed', 'suggestion_enabled': False}
    prediction = history['forecasts'][0]
    processor = _processor(data, prediction['detail']['model_provenance']['capabilities'])
    ended = row['last_observed_at']
    valid = _current(ledger, prediction['evidence_refs'], prediction['detail']['source_versions'], owner)
    eligible = bool(row['state'] == 'completed' and valid and processor['conditions_comparable']
                    and prediction['created_at'] <= ended and ended >= prediction['detail']['origin_at'])
    training_model = prediction['detail']['conditions']['estimate'].get('training_served_model')
    comparable = bool(eligible and (training_model is None or training_model == processor['served_model']))
    facts = {'version': VERSION, 'kind': 'terminal', 'execution_id': execution_id, 'measurement': MEASUREMENT,
        'state': row['state'], 'ended_at': ended, 'processor': processor,
        'conditions_comparable': comparable, 'observation_eligible': eligible, 'quality_evaluated': False,
        'duration_seconds': ended-prediction['detail']['origin_at'],
        'parent_execution_id': row['parent_execution_id']}
    versions, retained = _receipt(ledger, owner, execution_id, 'terminal', facts, ended)
    comparable = bool(comparable and retained['conditions_comparable'])
    eligible = bool(eligible and retained['observation_eligible'])
    # A failed/cancelled/incomplete observation is retained without training
    # the estimator. There is no inference of successful external effects.
    reason = 'source_retracted' if not valid else 'cancelled' if row['state'] == 'interrupted' else 'unavailable'
    store.record_forecast_outcome(forecast_id=_fid(execution_id), receipt_ref=next(iter(versions)),
        evidence_refs=list(versions), source_versions=versions, source_kind='task_receipt', observed_at=ended,
        recorded_at=registry.clock(), subject_person_id=owner, viewer_scope='owner', shareability='owner_private',
        status='observed' if eligible else 'censored', value=True if eligible else None, reason='' if eligible else reason)
    return {'status': 'observed', 'conditions_comparable': comparable, 'suggestion_enabled': False}


def safe_reconcile(registry, owner):
    """Replay a bounded set of durable terminals on callbacks and owner reads.

    The marker is only an optimization in the existing operational metadata.
    A crash before it commits replays the same immutable outcome receipt.
    No terminal row can issue a retrospective forecast.
    """
    try:
        if _parts(registry, owner) is None:
            return
        with closing(registry.ledger._connect()) as db:
            rows = db.execute(
                "SELECT e.execution_id FROM execution_observations e "
                "JOIN execution_runtime_observations r USING(execution_id) "
                "WHERE e.contact_id=? AND e.state!='observed' "
                "AND e.last_observed_at>=? "
                "AND json_extract(r.metadata_json,'$.forecast_settled') IS NULL "
                "ORDER BY e.last_observed_at,e.execution_id LIMIT 20",
                (owner, registry.clock()-7*86400)).fetchall()
        for row in rows:
            safe_observe(registry, row['execution_id'], owner)
    except Exception as error:
        logger.warning('Execution duration reconciliation unavailable: %s', type(error).__name__)


def safe_observe(registry, execution_id, owner):
    try:
        result = observe(registry, execution_id, owner)
        if result and result.get('status') in {'observed', 'no_prospective_forecast'}:
            with closing(registry.ledger._connect()) as db, db:
                db.execute(
                    "UPDATE execution_runtime_observations "
                    "SET metadata_json=json_set(metadata_json,'$.forecast_settled',1) "
                    "WHERE execution_id=? AND execution_id IN "
                    "(SELECT execution_id FROM execution_observations "
                    "WHERE contact_id=? AND state!='observed')", (execution_id, owner))
        return result
    except Exception as error:
        logger.warning('Execution duration observer unavailable: %s', type(error).__name__)
        return {'status': 'unavailable', 'error_type': type(error).__name__, 'suggestion_enabled': False}


def project(registry, execution_id, owner):
    try:
        parts = _parts(registry, owner)
        if parts is None:
            return {'status': 'disabled', 'suggestion_enabled': False}
        store, ledger = parts
        history = store.forecast_history(_fid(execution_id))
        if not history['forecasts']:
            return {'status': 'no_prospective_forecast', 'suggestion_enabled': False}
        prediction = history['forecasts'][0]
        if not _current(ledger, prediction['evidence_refs'], prediction['detail']['source_versions'], owner):
            return {'status': 'source_unavailable', 'suggestion_enabled': False}
        estimate = prediction['detail']['conditions']['estimate']
        result = {'status': 'shadow', 'forecast_id': _fid(execution_id), 'measurement': MEASUREMENT,
            'original_horizon': prediction['horizon'], 'prior_horizon': prediction['detail']['origin_at']+PRIOR_SECONDS,
            'sample_n': estimate['sample_n'], 'uncertain': estimate['uncertain'],
            'suggestion_enabled': False, 'quality_evaluated': False, 'conditions_comparable': False}
        if history['outcomes']:
            outcome = history['outcomes'][-1]
            facts = _facts(ledger, outcome)
            result.update(outcome_status=outcome['status'], processor=facts.get('processor'),
                conditions_comparable=bool(facts.get('conditions_comparable') and
                    _current(ledger, outcome['evidence_refs'], outcome['source_versions'], owner)))
            if result['conditions_comparable']:
                ended = outcome['observed_at']
                result['comparison'] = {'duration_seconds': facts['duration_seconds'],
                    'forecast_absolute_error_seconds': abs(ended-result['original_horizon']),
                    'prior_absolute_error_seconds': abs(ended-result['prior_horizon']),
                    'forecast_premature_inspection': result['original_horizon'] < ended,
                    'prior_premature_inspection': result['prior_horizon'] < ended,
                    'forecast_inspection_lateness_seconds': max(0., result['original_horizon']-ended),
                    'prior_inspection_lateness_seconds': max(0., result['prior_horizon']-ended)}
        return result
    except Exception:
        return {'status': 'unavailable', 'suggestion_enabled': False}
