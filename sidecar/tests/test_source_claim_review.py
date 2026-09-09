"""Review controls admission, never rewrites source or grants truth/authority."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

from jsonschema import Draft202012Validator, ValidationError
import pytest

from colony_sidecar.beliefs.source_claims import (
    EXTRACTION_VERSION, extract_claims, projection_timeout_seconds,
    review_response_schema, validated_review, SourceClaimOutputError,
)
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.turns import TurnIdempotencyLedger
from test_function_routing import endpoint, router, config
from test_source_claim_projection import claim, Model, prepared as prepared_context

TEXT = 'My blue case is on the shelf. My green case is in the drawer.'
CLAIMS = [claim(TEXT, 'shelf', subject='blue case', predicate='location'),
          claim(TEXT, 'drawer', subject='green case', predicate='location')]


def review(*keeps):
    return json.dumps({str(i): {'keep': keep, 'reason': 'Controlled source-scope judgment.'}
                       for i, keep in enumerate(keeps)})


class ReviewedModel(Model):
    def __init__(self, output, *, before_review=None):
        super().__init__({})
        self.review_output, self.before_review = output, before_review

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        self.calls.append((deepcopy(payload), kwargs))
        is_review = kwargs['context']['task'] == 'source_claim_review'
        if is_review and self.before_review:
            await self.before_review()
        if is_review and isinstance(self.review_output, Exception):
            raise self.review_output
        return SimpleNamespace(content=self.review_output if is_review else json.dumps(CLAIMS),
            model_id='same-local-model', function_role='judging' if is_review else 'extraction',
            config_revision='local-config', model_revision='local-weights', binding='local')


def prepared(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'source.db')
    ledger.record_source('turn', contact_id='person', session_id='earlier',
                         messages=[{'role': 'user', 'content': TEXT}])
    return ledger, SourceClaimProjection(ledger)


@pytest.mark.asyncio
async def test_review_keeps_original_claim_bytes_and_records_distinct_judgment(tmp_path):
    ledger, projection = prepared(tmp_path)
    model = ReviewedModel(review(False, True))
    assert await projection.process_one(model)
    proposed = model.calls[1][0]['proposals']
    assert model.calls[1][0]['message'] == TEXT
    assert len(model.calls) == 2
    assert model.calls[0][1]['force_tier'] == model.calls[1][1]['force_tier']
    assert all(call[1]['context']['allow_fallback'] is False for call in model.calls)
    with sqlite3.connect(ledger.db_path) as conn:
        data = json.loads(conn.execute('SELECT data_json FROM source_claims').fetchone()[0])
    original = proposed[1]['claim']
    assert all(data[key] == value for key, value in original.items())
    assert data['value'] == 'drawer' and data['evidence'] == TEXT
    metadata = data['admission_review']
    assert metadata['basis'] == 'model_judgment_unverified'
    assert metadata['model_provenance']['function_role'] == 'judging'
    assert data['model_provenance']['function_role'] == 'extraction'
    status = projection.status('person')[0]
    assert status['status'] == 'complete' and status['extraction_version'] == EXTRACTION_VERSION
    assert status['claim_count'] == 1
    diagnostic = status['diagnostics']
    assert diagnostic['accepted_count'] == diagnostic['reviewed_count'] == 2
    assert diagnostic['review_kept_count'] == diagnostic['review_rejected_count'] == 1
    assert TEXT not in json.dumps(diagnostic) and 'Controlled source-scope' not in json.dumps(diagnostic)
    assert ledger.search_sources('blue case', contact_id='person', session_id='later')
    context = prepared_context(projection, query='green case', contact='person')
    assert context and 'admission_review' not in json.dumps(context)
    assert 'Controlled source-scope' not in json.dumps(context)


@pytest.mark.asyncio
async def test_semantic_rejection_completes_without_erasing_the_source(tmp_path):
    ledger, projection = prepared(tmp_path)
    await projection.process_one(ReviewedModel(review(False, False)))
    status = projection.status('person')[0]
    assert status['status'] == 'complete' and status['claim_count'] == 0
    assert status['diagnostics']['review_rejected_count'] == 2
    assert ledger.search_sources('case', contact_id='person', session_id='later')


@pytest.mark.asyncio
@pytest.mark.parametrize('output', [
    '{}', '[]', '{', review(True),
    '{"0":{"keep":true,"reason":"Valid"},"0":{"keep":false,"reason":"Duplicate"}}',
    '{"0":{"keep":true,"keep":false,"reason":"Duplicate field"},"1":{"keep":true,"reason":"Valid"}}',
    json.dumps({'0': {'keep': 'true', 'reason': 'Not boolean'}, '1': {'keep': True, 'reason': 'Valid'}}),
    json.dumps({'0': {'keep': True, 'reason': 'Valid', 'value': 'rewritten'}, '1': {'keep': True, 'reason': 'Valid'}}),
    RuntimeError('No judging role is available'),
])
async def test_invalid_or_unavailable_review_defers_whole_batch_without_commit(tmp_path, output):
    ledger, projection = prepared(tmp_path)
    await projection.process_one(ReviewedModel(output))
    status = projection.status('person')[0]
    assert status['status'] == 'pending' and status['claim_count'] == 0
    assert status['error'] in {'SourceClaimOutputError', 'RuntimeError'}
    assert ledger.search_sources('case', contact_id='person', session_id='later')


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['erase', 'reclaim'])
async def test_late_review_cannot_commit_after_source_erasure_or_lease_reclaim(tmp_path, race):
    ledger, projection = prepared(tmp_path)
    successor = []
    async def invalidate():
        if race == 'erase':
            ledger.erase_sources(contact_id='person', turn_ids=['turn'])
        else:
            with sqlite3.connect(ledger.db_path) as conn:
                conn.execute('UPDATE source_claim_jobs SET lease_until=0')
            successor.append(SourceClaimProjection(ledger).claim_job())
    await projection.process_one(ReviewedModel(review(True, True), before_review=invalidate))
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 0
    if race == 'erase':
        assert projection.status('person') == []
    else:
        assert successor[0] and projection.status('person')[0]['status'] == 'running'


@pytest.mark.asyncio
async def test_total_bound_cancels_review_and_keeps_job_pending(tmp_path, monkeypatch):
    from colony_sidecar.beliefs import source_projection as module
    ledger, projection = prepared(tmp_path)
    stopped = asyncio.Event()
    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    model = ReviewedModel(review(True, True), before_review=blocked)
    monkeypatch.setattr(module, 'projection_timeout_seconds', lambda router: .02)
    await projection.process_one(model)
    assert stopped.is_set()
    assert projection.status('person')[0]['status'] == 'pending'
    assert projection.status('person')[0]['error'] == 'TimeoutError'
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_single_configured_endpoint_can_extract_and_review_without_distinct_models():
    def answer(payload):
        packet = json.loads(payload['messages'][1]['content'])
        return review(True, False) if 'proposals' in packet else json.dumps(CLAIMS)
    with endpoint(content=answer) as (url, calls):
        cfg = config(url, url, timeoutSeconds=10, deadlineSeconds=20)
        cfg['modelPool'] = {'interactive': {**cfg['modelPool']['interactive'], 'supportsJsonSchema': True}}
        cfg['functionRoles'] = {role: ['interactive'] for role in ('extraction', 'judging')}
        model = router(cfg)
        rows, _ = await extract_claims(model, {'occurred_at': None}, {'role': 'user', 'content': TEXT}, [])
    assert len(rows) == 1 and len(calls) == 2
    assert {call['payload']['model'] for call in calls} == {'fast-neutral'}
    assert calls[1]['payload']['response_format']['json_schema']['schema'] == review_response_schema(2)['schema']
    assert calls[1]['payload']['max_tokens'] == 1400


def test_exact_keys_and_duplicate_keys_are_required_without_repair():
    check = Draft202012Validator(review_response_schema(2)['schema'])
    check.validate(json.loads(review(True, False)))
    with pytest.raises(ValidationError):
        check.validate(json.loads(review(True)))
    with pytest.raises(SourceClaimOutputError):
        validated_review('{"0":{"keep":true,"reason":"a"},"0":{"keep":true,"reason":"b"}}', 2)
    assert projection_timeout_seconds(SimpleNamespace()) == 40


@pytest.mark.asyncio
async def test_configured_judging_fallback_retains_actual_served_provenance():
    def answer(payload):
        packet = json.loads(payload['messages'][1]['content'])
        return review(True, False) if 'proposals' in packet else json.dumps(CLAIMS)
    with endpoint(status=503) as (unavailable, attempts), endpoint(content=answer) as (local, calls):
        cfg = config(local, unavailable, timeoutSeconds=10, deadlineSeconds=20)
        cfg['functionRoles']['extraction'] = ['interactive']
        cfg['functionRoles']['judging'] = ['deliberate', 'interactive']
        rows, _ = await extract_claims(router(cfg), {'occurred_at': None},
            {'role': 'user', 'content': TEXT}, [])
    assert len(attempts) == 1 and len(calls) == 2 and len(rows) == 1
    provenance = rows[0]['admission_review']['model_provenance']
    assert provenance['binding'] == 'interactive' and provenance['function_role'] == 'judging'
    assert provenance['model_id'] == 'openai/fast-neutral'
