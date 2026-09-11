"""Observe exact replies by a declared horizon, without scheduling or sending.

An accepted dispatch is not delivery/read evidence. The fixed 0.5 probability
is an explicitly uncalibrated baseline, not a judgment about the recipient.
Only existing trusted callbacks can issue a forecast; reads only reconcile it.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
import time

from colony_sidecar import get_state_dir
from colony_sidecar.contacts.transport_ingress import TransportIngress
from colony_sidecar.turns import get_turn_idempotency_ledger
from .expectations import expectations_enabled
from .runtime_forecasts import _current, _digest

METHOD = 'accepted-dispatch-reply-by-declared-horizon-v1'
logger = logging.getLogger(__name__)


def _parts():
    from colony_sidecar.api.routers import host
    owner = (os.environ.get('COLONY_OWNER_PERSON_ID') or os.environ.get('COLONY_OWNER_CONTACT_ID', '')).strip()
    if (not owner or not expectations_enabled() or host._expectations is None
            or host._commitment_store is None or host._comms_log is None):
        return None
    return (host._expectations.store, get_turn_idempotency_ledger(get_state_dir()),
            host._commitment_store, host._comms_log, owner)


def _fid(wait_id):
    return 'expected-reply:'+_digest(wait_id)


def _metadata(ledger, source):
    with closing(ledger._connect()) as db:
        row = db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (source,)).fetchone()
    return json.loads(row[0])[0].get('_expected_reply_facts') if row else None


def _retain(ledger, source, person, facts, stamp, dependencies=None, session_id=''):
    previous = _metadata(ledger, source)
    if previous is None:
        ledger.record_source(source, contact_id=person, session_id=session_id or source,
            occurred_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(), derive_claims=False,
            messages=[{'role':'assistant', 'content':'', '_expected_reply_facts':facts,
                '_supplied_sources':[{'source_id':sid, 'source_version':version}
                    for sid, version in (dependencies or {}).items()]}])
        previous = _metadata(ledger, source)
    refs = ledger.source_references([source], contact_id=person, session_id='')
    if not refs or ledger.is_projection_erased(source) or previous.get('method') != METHOD:
        raise ValueError('reply_forecast_evidence_unavailable')
    return {'receipt:'+source:refs[0]['source_version']}, previous


def _parent_current(ledger, commitments, conditions, owner):
    from colony_sidecar.api.routers.temporal_followups import source_bindings
    parent = commitments.get(conditions['commitment_id'])
    with closing(commitments._connect()) as db:
        row = db.execute('SELECT material_digest FROM temporal_followups WHERE wait_id=?',
                         (conditions['wait_id'],)).fetchone()
    return bool(parent and parent['person_id'] == owner and row
        and row[0] == conditions['wait_material_digest']
        and not source_bindings(conditions['parent_sources'], conditions['parent_sources'],
            person=owner, session_id=conditions['parent_session_id']))


def _reply(ingress, ledger, conditions, item):
    row = ingress.get(item['receipt_ref'])
    if not row or row['state'] != 'completed' or row['outcome'] != 'captured':
        return None
    metadata = json.loads(row['metadata_json'])
    channel = conditions['channel']
    if (row['producer'] != conditions['producer'] or row['account_id'] != conditions['account_id']
            or row['contact_id'] != conditions['recipient_id'] or not row['media_available']
            or metadata.get('channel') != channel or metadata.get('from_owner') or metadata.get('is_group')
            or channel+':'+metadata.get('reply_to_ref', '') != conditions['provider_external_ref']
            or channel+':'+row['event_id'] != item['external_ref']):
        return None
    native = json.loads(row['native_turn_json'] or '{}')
    versions = json.loads(row['source_versions_json'] or '{}')
    if set(versions) != {native.get('turn_id')}:
        return None
    from colony_sidecar.api.routers.temporal_followups import source_bindings
    if source_bindings(versions, versions, person=row['contact_id'], session_id=native['session_id']):
        return None
    return row


def evidence_current(prediction, observation=None):
    """Narrow read predicate; raw forecast_history remains historical evidence."""
    if prediction.detail.get('method') != METHOD:
        return True
    try:
        parts = _parts()
        if parts is None:
            return False
        store, ledger, commitments, comms, owner = parts
        c = prediction.detail['conditions']
        if (prediction.subject_person_id != owner or prediction.viewer_scope != owner
                or prediction.shareability != 'owner_private'
                or not _parent_current(ledger, commitments, c, owner)
                or not _current(ledger, prediction.evidence_refs, prediction.detail['source_versions'], owner)):
            return False
        if observation is None:
            outcomes = store.forecast_history(prediction.detail['forecast_id'])['outcomes']
            observation = outcomes[-1] if outcomes else None
        if observation is None:
            return True
        facts = _metadata(ledger, observation['receipt_ref'].removeprefix('receipt:'))
        if not facts or facts.get('method') != METHOD:
            return False
        person = c['recipient_id'] if facts['kind'] == 'reply' else owner
        if not _current(ledger, observation['evidence_refs'], observation['source_versions'], person):
            return False
        with closing(comms.read_connection()) as db:
            ingress = TransportIngress(db)
            if facts['kind'] == 'reply':
                row = _reply(ingress, ledger, c, facts['reply'])
                return bool(row and json.loads(row['source_versions_json']) == facts['reply_sources'])
            coverage = ingress.coverage(producer=c['producer'], account_id=c['account_id'],
                contact_id=c['recipient_id'], since=prediction.detail['origin_at'], until=prediction.horizon,
                now=facts['coverage']['observed_at'], observation=facts['coverage'])
            return coverage['observed'] and coverage['observed_through'] >= prediction.horizon
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return False


def observe_dispatch(waits, row, *, producer, receipt, created):
    parts = _parts()
    if parts is None or receipt.direction != 'out':
        return
    store, ledger, commitments, comms, owner = parts
    fid = _fid(row['wait_id'])
    if store.forecast_history(fid)['forecasts']:
        return
    source = fid+':dispatch'
    retained = _metadata(ledger, source)
    # A later ledger scan cannot invent a forecast. An interrupted first
    # callback can replay its already frozen declaration, never a new horizon.
    if not created and retained is None:
        return
    if retained is None:
        now = time.time()
        parent = commitments.get(row['commitment_id'])
        if (not parent or parent['person_id'] != owner or row['reply'] or row['state'] not in {'open','deferred'}
                or row['dispatch_receipt_ref'] != receipt.receipt_ref
                or row['outbound_ref'] not in {receipt.outbound_ref, receipt.channel+':'+receipt.external_ref}
                or not row['expected_at'] or row['expected_at'] <= now or row['expires_at'] <= now):
            return
        with closing(comms.read_connection()) as db:
            accounts = db.execute('SELECT account_id FROM transport_ingress_coverage WHERE producer=?',
                                  (producer,)).fetchall()
        # This first method uses the existing one-account WhatsApp contract.
        if receipt.channel != 'whatsapp' or len(accounts) != 1:
            return
        with closing(commitments._connect()) as db:
            material = db.execute('SELECT material_digest FROM temporal_followups WHERE wait_id=?',
                                  (row['wait_id'],)).fetchone()[0]
        c = {'wait_id':row['wait_id'], 'commitment_id':row['commitment_id'], 'work_id':row['work_id'],
            'wait_material_digest':material, 'parent_sources':row['source_versions'],
            'parent_origin':parent['made_at'],
            'parent_session_id':row['source_session_id'], 'recipient_id':row['contact_id'],
            'producer':producer, 'account_id':accounts[0]['account_id'], 'channel':receipt.channel,
            'provider_external_ref':receipt.channel+':'+receipt.external_ref,
            'dispatch_receipt_ref':receipt.receipt_ref, 'dispatch_status':receipt.status,
            'outbound_ref':row['outbound_ref'], 'probability_basis':'fixed uncalibrated 0.5 baseline'}
        if not _parent_current(ledger, commitments, c, owner):
            return
        retained = {'method':METHOD, 'kind':'dispatch', 'conditions':c,
            'origin_at':row['dispatch_occurred_at'], 'horizon':row['expected_at'], 'issued_at':now}
    c = retained['conditions']
    if not _parent_current(ledger, commitments, c, owner):
        return
    versions, retained = _retain(ledger, source, owner, retained, retained['issued_at'],
                                 c['parent_sources'], c['parent_session_id'])
    store.issue_forecast(forecast_id=fid, subject=fid, domain='expected_reply',
        expectation='An exact reply is observed by the declared horizon after the recorded dispatch acknowledgment.',
        confidence=.5, horizon=retained['horizon'], origin_at=retained['origin_at'], issued_at=retained['issued_at'],
        evidence_refs=versions, source_versions=versions, source_kind='transport_receipt',
        cohort='declared-reply:whatsapp:'+c['dispatch_status'], method=METHOD,
        model_provenance={'served_model':None}, subject_person_id=owner, viewer_scope=owner,
        shareability='owner_private', conditions=c)


def reconcile(wait_id):
    parts = _parts()
    if parts is None:
        return
    store, ledger, commitments, comms, owner = parts
    history = store.forecast_history(_fid(wait_id))
    if not history['forecasts']:
        return
    raw = history['forecasts'][0]
    prediction = store.get(raw['prediction_id'])
    if not evidence_current(prediction):
        return
    latest = history['outcomes'][-1] if history['outcomes'] else None
    if latest and latest['status'] == 'observed':
        return
    c = prediction.detail['conditions']
    now = time.time()
    with closing(comms.read_connection()) as db:
        ingress = TransportIngress(db)
        matches = comms.match_reply(contact_id=c['recipient_id'], outbound_ref=c['provider_external_ref'],
            since_iso=c['parent_origin'],
            until_iso=datetime.fromtimestamp(now, timezone.utc).isoformat(), connection=db)
        facts = None
        for item in matches['matches']:
            row = _reply(ingress, ledger, c, item)
            if row is None:
                continue
            observed = row['occurred_at']
            state = ('retrospective_unscored' if observed < prediction.created_at else
                     'reply_observed_in_time' if observed <= prediction.horizon else 'deadline_elapsed_unobserved')
            facts = {'method':METHOD, 'kind':'reply', 'state':state, 'reply':item,
                     'reply_sources':json.loads(row['source_versions_json']), 'observed_at':observed}
            source = _fid(wait_id)+':reply:'+_digest(row['receipt_id'])
            versions, facts = _retain(ledger, source, c['recipient_id'], facts, now,
                facts['reply_sources'], json.loads(row['native_turn_json'])['session_id'])
            if latest and latest['receipt_ref'] == next(iter(versions)):
                facts = None
                break
            store.record_forecast_outcome(forecast_id=_fid(wait_id), receipt_ref=next(iter(versions)),
                evidence_refs=versions, source_versions=versions, source_kind='transport_receipt',
                observed_at=facts['observed_at'], recorded_at=now, subject_person_id=owner,
                viewer_scope=owner, shareability='owner_private',
                status='observed' if state == 'reply_observed_in_time' else 'unresolved',
                value=True if state == 'reply_observed_in_time' else None,
                reason='' if state == 'reply_observed_in_time' else 'unknown',
                previous_revision=latest['revision'] if latest else 0)
            if state in {'reply_observed_in_time','retrospective_unscored'}:
                return
            latest = store.forecast_history(_fid(wait_id))['outcomes'][-1]
            break
        if now < prediction.horizon:
            return
        accounts = db.execute('SELECT * FROM transport_ingress_coverage WHERE producer=?',
                                      (c['producer'],)).fetchall()
        if len(accounts) != 1 or accounts[0]['account_id'] != c['account_id']:
            return
        coverage = dict(accounts[0])
        checked = ingress.coverage(producer=c['producer'], account_id=c['account_id'],
            contact_id=c['recipient_id'], since=prediction.detail['origin_at'], until=prediction.horizon, now=now)
        if not checked['observed'] or checked['observed_through'] < prediction.horizon:
            return
        facts = {'method':METHOD, 'kind':'coverage', 'state':'no_reply_with_coverage', 'coverage':coverage}
        versions, facts = _retain(ledger, _fid(wait_id)+':coverage', owner, facts, now,
                                 c['parent_sources'], c['parent_session_id'])
        store.record_forecast_outcome(forecast_id=_fid(wait_id), receipt_ref=next(iter(versions)),
            evidence_refs=versions, source_versions=versions, source_kind='transport_receipt',
            observed_at=prediction.horizon, coverage_until=facts['coverage']['observed_at'], recorded_at=now,
            subject_person_id=owner, viewer_scope=owner, shareability='owner_private', value=False,
            previous_revision=latest['revision'] if latest else 0)


def safe(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as error:
        logger.warning('Declared reply forecast observation unavailable: %s', type(error).__name__)


def reconcile_existing(*, receipt=None, producer=None, coverage=None):
    """At most 100 newest relevant waits, never a FIFO of historical unknowns.

    Exact incoming lineage bypasses unrelated waits. Coverage considers only
    this account's observed connected interval. Older broad-coverage history
    is not exhaustively drained; an owner wait read reconciles that exact row.
    """
    parts = _parts()
    if parts is None:
        return
    store = parts[0]
    query = "SELECT detail FROM predictions WHERE domain='expected_reply' AND source=? AND outcome IN ('pending','unresolved') "
    parameters = [METHOD]
    if receipt is not None:
        metadata = json.loads(receipt['metadata_json'])
        if not metadata.get('reply_to_ref') or not receipt['contact_id']:
            return
        for name, value in {'recipient_id':receipt['contact_id'], 'producer':receipt['producer'],
                'account_id':receipt['account_id'],
                'provider_external_ref':metadata['channel']+':'+metadata['reply_to_ref']}.items():
            query += "AND json_extract(detail,'$.conditions."+name+"')=? "
            parameters.append(value)
    elif coverage is not None and coverage['connected'] and coverage['connected_since'] is not None:
        query += ("AND json_extract(detail,'$.conditions.producer')=? "
            "AND json_extract(detail,'$.conditions.account_id')=? "
            "AND json_extract(detail,'$.origin_at')>=? AND horizon<=? ")
        parameters += [producer, coverage['account_id'], coverage['connected_since'], coverage['observed_at']]
    else:
        return
    with store._lock:
        rows = store._conn.execute(query+"ORDER BY created_at DESC,prediction_id LIMIT 100", parameters).fetchall()
    for row in rows:
        c = json.loads(row[0])['conditions']
        safe(reconcile, c['wait_id'])


def project(wait_id):
    result = {'status':'no_prospective_forecast', 'suggestion_enabled':False}
    parts = _parts()
    if parts is None:
        return result
    safe(reconcile, wait_id)
    store = parts[0]
    history = store.forecast_history(_fid(wait_id))
    if not history['forecasts']:
        return result
    p = store.get(history['forecasts'][0]['prediction_id'])
    result.update(status='pending' if time.time() < p.horizon else 'deadline_elapsed_unobserved',
        forecast_id=_fid(wait_id), horizon=p.horizon, probability=.5, probability_calibrated=False)
    if not evidence_current(p):
        return {**result, 'status':'source_unavailable'}
    if history['outcomes']:
        facts = _metadata(parts[1], history['outcomes'][-1]['receipt_ref'].removeprefix('receipt:'))
        result['status'] = facts['state']
    return result
