"""Canonical, prospective native request metadata; no transcript classifier."""
from contextlib import closing
import json
import time

from .runtime_forecasts import VERSION, _current, _digest, _identity, _retain


def _prefix(native, run_id):
    return 'native-model:'+_digest({**_identity(native), 'native_run_id': run_id})+':'


def retain(ledger, owner, native, state, value):
    """The API caller already verified the active native run and claim lock."""
    request = {key: value.get(key) for key in
               ('api_request_id', 'phase', 'requested_model', 'provider', 'response_model')}
    if request['phase'] != 'response':
        request['response_model'] = None
    source_id = _prefix(native, native['native_run_id'])+_digest([request['api_request_id'], request['phase']])
    versions, retained = _retain(ledger, source_id=source_id, owner=owner, native=native,
        occurred_at=time.time(), facts={'version': VERSION, 'kind': 'model_request',
            'identity': _identity(native), 'native_run_id': native['native_run_id'],
            'request': request, 'configuration': state['forecast_configuration']})
    if retained['request'] != request:
        raise ValueError('native_model_observation_conflict')
    return {'accepted': True, 'evidence_refs': list(versions),
            'identity_basis': 'provider_reported_response_model_not_weight_attestation'}


def summarize(ledger, owner, native, run_id):
    """Pair observed starts/ends; missing, erased and mixed remain explicit."""
    prefix = _prefix(native, run_id)
    with closing(ledger._connect()) as db:
        rows = db.execute('SELECT turn_id,messages_json FROM turn_sources WHERE contact_id=? '
                          'AND substr(turn_id,1,?)=? ORDER BY turn_id LIMIT 257',
                          (owner, len(prefix), prefix)).fetchall()
        erased = db.execute('SELECT 1 FROM source_erasures WHERE substr(turn_id,1,?)=? LIMIT 1',
                            (len(prefix), prefix)).fetchone()
    refs = ledger.source_references([r['turn_id'] for r in rows], contact_id=owner, session_id='')
    versions = {'receipt:'+r['source_id']: r['source_version'] for r in refs}
    valid = bool(rows) and len(refs) == len(rows) and len(rows) <= 256 and not erased and _current(ledger, list(versions), versions, owner)
    requests, configurations, models, requested = {}, [], set(), set()
    for row in rows:
        facts = json.loads(row['messages_json'])[0].get('_native_forecast_facts', {})
        if (facts.get('version') != VERSION or facts.get('kind') != 'model_request'
                or facts.get('identity') != _identity(native) or facts.get('native_run_id') != run_id):
            valid = False
            continue
        request = facts['request']
        requests.setdefault(request['api_request_id'], {})[request['phase']] = request
        configurations.append(facts['configuration'])
        if request.get('requested_model'):
            requested.add(request['requested_model'])
        if request['phase'] == 'response' and request.get('response_model'):
            models.add(request['response_model'])
    complete = valid and all(set(v) in ({'start', 'response'}, {'start', 'error'}) for v in requests.values())
    missing_model = any('response' in v and not v['response'].get('response_model') for v in requests.values())
    same_configuration = bool(configurations) and all(c == configurations[0] for c in configurations)
    return {'basis': 'provider_reported_response_model_not_weight_attestation',
        'coverage': 'registered native review API requests only; auxiliary and uninstrumented calls unknown',
        'request_count': len(requests), 'complete_observed_pairs': bool(complete),
        'response_models': sorted(models), 'requested_models': sorted(requested),
        'served_model': next(iter(models)) if complete and not missing_model and len(models) == 1 else None,
        'model_state': 'unknown' if not complete or missing_model or not models else 'mixed' if len(models) > 1 else 'observed',
        'configuration': configurations[0] if same_configuration else None,
        'configuration_state': 'observed' if same_configuration and complete else 'unknown_or_changed',
        'evidence_refs': list(versions), 'source_versions': versions}
