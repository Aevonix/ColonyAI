"""Explain empty projections without retaining model drafts or changing admission."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pacomind.beliefs.source_claims import extraction_diagnostics
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from test_source_claim_projection import Model, claim


TEXT = 'My office is in River.'


def setup(tmp_path, messages=None):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ledger.record_source('source', contact_id='person', session_id='first',
                         messages=messages or [{'role': 'user', 'content': TEXT}])
    return ledger, SourceClaimProjection(ledger)


def response(output, *, model='local-model', revision='config-a', role='extraction'):
    return SimpleNamespace(content=output, model_id=model, function_role=role,
                           config_revision=revision, model_revision='weights-a')


@pytest.mark.asyncio
@pytest.mark.parametrize('output,candidates,accepted,rejected,empty', [
    ('[]', 0, 0, 0, 1),
    (json.dumps([claim(TEXT, 'Lake')]), 1, 0, 1, 0),
    (json.dumps([claim(TEXT, 'River')]), 1, 1, 0, 0),
])
async def test_empty_rejected_and_accepted_have_distinct_durable_receipts(
        tmp_path, output, candidates, accepted, rejected, empty):
    ledger, projection = setup(tmp_path)
    model = Model({})
    model.complete = AsyncMock(side_effect=[response(output), response(json.dumps({
        '0': {'keep': True, 'reason': 'Controlled valid location.'}}), role='judging')])
    assert await projection.process_one(model)
    row = SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path)).status('person')[0]
    assert row['status'] == 'complete' and row['claim_count'] == accepted
    data = row['diagnostics']
    assert data['candidate_count'] == candidates
    assert data['accepted_count'] == accepted
    assert data['rejected_count'] == rejected
    assert data['empty_array_count'] == empty
    assert data['response_count'] == 1 and data['invalid_array_count'] == 0
    assert data['review_response_count'] == accepted
    assert model.complete.await_count == 1 + accepted
    assert data['rejection_counts'] == ({'value_not_grounded': 1} if rejected else {})
    assert data['last_model_provenance'] == {
        'function_role': 'extraction', 'model_id': 'local-model',
        'config_revision': 'config-a', 'weight_revision': 'weights-a'}
    assert TEXT not in json.dumps(data) and 'evidence' not in data
    ledger.erase_sources(contact_id='person', turn_ids=['source'])
    assert projection.status('person') == []
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_multi_message_counts_aggregate_and_last_completed_binding_is_explicit(tmp_path):
    second = 'My office is in Birch.'
    ledger, projection = setup(tmp_path, [{'role': 'user', 'content': TEXT},
                                         {'role': 'user', 'content': second}])
    model = Model({})
    model.complete = AsyncMock(side_effect=[
        response(json.dumps([claim(TEXT, 'River'), claim(TEXT, 'Lake')])),
        response(json.dumps({'0': {'keep': True, 'reason': 'Controlled valid location.'}}), role='judging'),
        response('[]', model='other-local-model', revision='config-b'),
    ])
    assert await projection.process_one(model)
    data = projection.status('person')[0]['diagnostics']
    assert data['response_count'] == 2 and data['candidate_count'] == 2
    assert data['accepted_count'] == data['rejected_count'] == data['empty_array_count'] == 1
    assert data['last_model_provenance']['model_id'] == 'other-local-model'
    assert data['last_model_provenance']['config_revision'] == 'config-b'


@pytest.mark.asyncio
async def test_failed_array_keeps_provenance_and_retry_replaces_attempt_counts(tmp_path):
    ledger, projection = setup(tmp_path)
    model = Model({})
    model.complete = AsyncMock(return_value=response('{"claims": []}'))
    assert await projection.process_one(model)
    failed = projection.status('person')[0]
    assert failed['status'] == 'pending' and failed['error'] == 'SourceClaimOutputError'
    assert failed['diagnostics']['invalid_array_count'] == 1
    assert failed['diagnostics']['empty_array_count'] == 0
    assert failed['diagnostics']['last_model_provenance']['config_revision'] == 'config-a'
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute('UPDATE source_claim_jobs SET next_attempt=0')
    model.complete.return_value = response('[]', revision='config-b')
    assert await projection.process_one(model)
    data = projection.status('person')[0]['diagnostics']
    assert data['response_count'] == 1 and data['invalid_array_count'] == 0
    assert data['empty_array_count'] == 1
    assert data['last_model_provenance']['config_revision'] == 'config-b'


def test_reclaimed_or_erased_job_cannot_receive_stale_diagnostics(tmp_path):
    ledger, projection = setup(tmp_path)
    first = projection.claim_job()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute('UPDATE source_claim_jobs SET lease_until=0')
    second = projection.claim_job()
    old = dict(extraction_diagnostics(), empty_array_count=1)
    current = dict(extraction_diagnostics(), accepted_count=1)
    projection.finish_job(first, diagnostics=old)
    assert projection.status('person')[0]['diagnostics'] is None
    projection.finish_job(second, diagnostics=current)
    projection.finish_job(first, error='late', diagnostics=old)
    assert projection.status('person')[0]['diagnostics'] == dict(current, attempt=2)
    ledger.erase_sources(contact_id='person', turn_ids=['source'])
    projection.finish_job(second, diagnostics=current)
    assert projection.status('person') == []


@pytest.mark.asyncio
async def test_predecessor_attempt_cannot_inherit_new_worker_measurements(tmp_path):
    ledger, projection = setup(tmp_path)
    model = Model({})
    model.complete = AsyncMock(return_value=response('[]'))
    await projection.process_one(model)
    assert projection.status('person')[0]['diagnostics']['attempt'] == 1
    # An older worker updates the existing columns and ignores the new one.
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE source_claim_jobs SET attempts=attempts+1,status='complete',model='predecessor'")
    row = projection.status('person')[0]
    assert row['attempts'] == 2 and row['model'] == 'predecessor'
    assert row['diagnostics'] is None


def test_existing_job_migrates_without_inventing_historical_counts(tmp_path):
    path = tmp_path / 'sources.db'
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE source_claim_jobs (
            turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            timezone TEXT NOT NULL DEFAULT 'UTC', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            error TEXT, model TEXT, extraction_version TEXT, lease_token TEXT NOT NULL DEFAULT '')''')
        conn.execute("INSERT INTO source_claim_jobs(turn_id,status,attempts,model) VALUES ('source','complete',2,'old-model')")
    ledger, projection = setup(tmp_path)
    row = projection.status('person')[0]
    assert row['diagnostics'] is None
    assert row['status'] == 'complete' and row['attempts'] == 2 and row['model'] == 'old-model'
    assert SourceClaimProjection(TurnIdempotencyLedger(path)).status('person') == [row]
