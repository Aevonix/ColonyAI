"""Prospective native task turnaround learning, without quality claims.

Attachment is the prediction origin, so turnaround includes queue delay. Native
run elapsed time is retained separately. Hermes' ledger cannot attest which
processor actually served a request; configured overrides are not substituted.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import time

from colony_sidecar import get_state_dir
from colony_sidecar.turns import get_turn_idempotency_ledger
from colony_sidecar.turns.idempotency import SourceErased
from .expectations import expectations_enabled

logger = logging.getLogger(__name__)
VERSION = 'native-task-forecast-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _identity(native):
    return {key: native[key] for key in ('source_home_id', 'native_board', 'native_task_id')}


def _reference(source_id):
    return 'receipt:'+source_id


def _current(ledger, references, versions, owner):
    ids = [r.removeprefix('receipt:') for r in references]
    current = {r['source_id']:r['source_version'] for r in ledger.source_references(
        ids, contact_id=owner, session_id='')}
    if not all(current.get(identifier) == versions.get(_reference(identifier))
               and not ledger.is_projection_erased(identifier) for identifier in ids):
        return False
    with closing(ledger._connect()) as db:
        for identifier in ids:
            if db.execute('SELECT 1 FROM source_annotations WHERE target_source_id=? LIMIT 1', (identifier,)).fetchone():
                return False
    return True


def _retain(ledger, *, source_id, owner, native, facts, occurred_at):
    # First observation survives replay. Native configuration can change later;
    # never rebind an old receipt to whichever model is configured today.
    with closing(ledger._connect()) as db:
        exists = db.execute('SELECT 1 FROM turn_sources WHERE turn_id=?', (source_id,)).fetchone()
    if not exists:
        ledger.record_source(source_id, contact_id=owner,
            session_id='native-runtime:'+native['native_task_id'],
            occurred_at=datetime.fromtimestamp(occurred_at, timezone.utc).isoformat(),
            messages=[{'role':'assistant', '_native_runtime_observation':VERSION, '_native_forecast_facts':facts,
                'content':'Runtime-owned task forecast/lifecycle observation. This is not an owner statement, '
                          'successful output evaluation, or permission for any action.\n'+json.dumps(facts, sort_keys=True)}],
            derive_claims=False)
    refs = ledger.source_references([source_id], contact_id=owner, session_id='')
    if not refs or ledger.is_projection_erased(source_id):
        raise SourceErased('runtime forecast evidence unavailable')
    with closing(ledger._connect()) as db:
        message = json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (source_id,)).fetchone()[0])[0]
    if message.get('_native_runtime_observation') != VERSION or message.get('_native_forecast_facts', {}).get('identity') != _identity(native):
        raise ValueError('runtime forecast source binding conflict')
    return {_reference(source_id):refs[0]['source_version']}, message['_native_forecast_facts']


def _parts(review, native, state, owner):
    from colony_sidecar.api.routers import host
    engine = host._expectations
    if engine is None or not expectations_enabled():
        return None
    selection = (review.get('native_work') or {}).get('outcome_learning') or {}
    if selection.get('version') != 'native-runtime-observation-v1':
        return None
    identity = _identity(native)
    forecast_id = 'native-task:'+_digest(identity)
    ledger = get_turn_idempotency_ledger(get_state_dir())
    return engine.store, ledger, selection, identity, forecast_id


def attach(review, native, state, owner):
    """Called after a verified prospective blocked task binding, before promote."""
    parts = _parts(review, native, state, owner)
    if parts is None:
        return {'status':'disabled_or_unselected'}
    store, ledger, selection, identity, fid = parts
    existing = store.forecast_history(fid)['forecasts']
    if existing:
        return {'status':'existing', 'forecast_id':fid, 'horizon':existing[0]['horizon']}
    if state['status'] != 'blocked' or state['attempt_count'] != 0:
        return {'status':'not_prospective'}
    bound = datetime.fromisoformat(selection['bound_at']).timestamp()
    # Task/run timestamps have one-second precision. The no-runs binding above
    # establishes ordering within that second; retain the precise bound too.
    origin = math.floor(bound)
    now = time.time()
    config = state['forecast_configuration']
    budget = config.get('runtime_budget_seconds')
    prior = float(budget) if isinstance(budget, (int, float)) and budget > 0 else 480.
    cohort = 'native-review:'+review['review']['action']
    def current(prediction, outcome):
        return (_current(ledger, prediction.evidence_refs, prediction.detail['source_versions'], owner)
                and _current(ledger, outcome['evidence_refs'], outcome['source_versions'], owner))
    estimate = store.estimate_duration(domain='task_duration',cohort=cohort,
        subject_person_id=owner,viewer_scope='owner',prior_seconds=prior,
        now=now,evidence_is_current=current)
    horizon = origin + estimate['seconds']
    if horizon <= now:
        return {'status':'binding_too_old_for_prospective_forecast'}
    source_id = 'native-forecast-binding:'+_digest(identity)
    versions, retained = _retain(ledger,source_id=source_id,owner=owner,native=native,occurred_at=bound,
        facts={'version':VERSION,'kind':'prospective_binding','identity':identity,
               'bound_at':selection['bound_at'],'configuration':config,
               'registered_action':review['review']['action'],
               'estimate':estimate,'measurement':'attachment_to_terminal_turnaround'})
    # Recovery after source capture uses the retained original estimate, not
    # a later improved estimate presented as the earlier prediction.
    estimate = retained['estimate']
    horizon = origin + estimate['seconds']
    config = retained['configuration']
    prediction = store.issue_forecast(forecast_id=fid,subject='task:'+native['native_task_id'],
        domain='task_duration',expectation='The accepted native review completes within its turnaround estimate',
        confidence=estimate['confidence'],horizon=horizon,origin_at=origin,issued_at=origin,
        evidence_refs=list(versions),source_versions=versions,source_kind='task_receipt',
        cohort=cohort,method=estimate['method'],
        model_provenance={'requested_role':'native_default_worker','served_model':None,
                          'model_revision':None,'capabilities':config,'fallback':None},
        subject_person_id=owner,viewer_scope='owner',shareability='owner_private',
        conditions={'measurement':'attachment_to_terminal_turnaround','timestamp_precision_seconds':1,
                    'bound_at':selection['bound_at'],'estimate':estimate})
    return {'status':'issued','forecast_id':fid,'horizon':prediction.horizon,'estimate':estimate}


def observe(review, native, state, owner):
    """Readback settles duration only. Pauses/failures are explicit censoring."""
    parts = _parts(review, native, state, owner)
    if parts is None:
        return {'status':'disabled_or_unselected'}
    store, ledger, selection, identity, fid = parts
    history = store.forecast_history(fid)
    if not history['forecasts']:
        return {'status':'no_prospective_forecast'}
    prediction = history['forecasts'][0]
    observed = state.get('duration_observation')
    if not observed or not isinstance(observed.get('ended_at'), (int,float)):
        return {'status':'pending','forecast_id':fid}
    ended = observed['ended_at']
    if ended < math.floor(datetime.fromisoformat(selection['bound_at']).timestamp()) or ended > time.time():
        return {'status':'unqualified_native_time'}
    source_id = 'native-forecast-outcome:'+_digest({**identity,'native_run_id':state.get('native_run_id'),
        'outcome':observed['outcome'],'ended_at':ended})
    receipt = _reference(source_id)
    if any(o['receipt_ref'] == receipt for o in history['outcomes']):
        return {'status':'observed','forecast_id':fid,'receipt_ref':receipt}
    valid = _current(ledger,prediction['evidence_refs'],prediction['detail']['source_versions'],owner)
    paused = any(o['reason'] == 'paused' for o in history['outcomes'])
    complete = state['status'] == 'done' and state.get('completed_run') and valid and not paused
    reason = '' if complete else 'source_retracted' if not valid else 'paused' if paused or (state['status']=='blocked' and not state.get('gave_up')) else 'cancelled' if state['status'] in {'archived','cancelled'} else 'unavailable'
    versions, retained = _retain(ledger,source_id=source_id,owner=owner,native=native,occurred_at=ended,
        facts={'version':VERSION,'kind':'duration_outcome','identity':identity,
               'native_run_id':state.get('native_run_id'),'observation':observed,
               'native_status':state['status'],'forecast_id':fid,
               'turnaround_seconds':ended-prediction['detail']['origin_at'],
               'measurement':'attachment_to_terminal_turnaround','quality_evaluated':False})
    result = store.record_forecast_outcome(forecast_id=fid,receipt_ref=receipt,
        evidence_refs=list(versions),source_versions=versions,source_kind='task_receipt',
        observed_at=ended,subject_person_id=owner,viewer_scope='owner',shareability='owner_private',
        status='observed' if complete else 'censored',value=True if complete else None,
        reason=reason,previous_revision=len(history['outcomes']))
    return {'status':'observed','forecast_id':fid,'outcome_status':result['status'],
            'receipt_ref':receipt,'quality_evaluated':False}


def safe(operation, review, native, state, owner):
    """A forecast observer failure is visible but cannot stall accepted work."""
    try:
        return operation(review,native,state,owner)
    except SourceErased:
        return {'status':'source_unavailable'}
    except Exception as error:
        logger.warning('Native task forecast observer unavailable: %s', type(error).__name__)
        return {'status':'unavailable','error_type':type(error).__name__}
