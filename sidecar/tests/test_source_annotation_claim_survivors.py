"""Partial erasure must preserve a different message's real atomic claim projection."""
import json
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.beliefs.source_claims import validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.idempotency import source_message_hash
from pacomind.turns.source_vectors import SourceVectors
from test_turn_source_evidence import source_app


def commit_user_claim(projection, *, predicate, value):
    """Controlled extraction bytes; normal admission, lease and projection are real."""
    job = projection.claim_job()
    assert job is not None
    message = next(m for m in json.loads(job['messages_json']) if m['role'] == 'user')
    claims = validated_claims(json.dumps([{
        'subject': 'I', 'predicate': predicate, 'value': value,
        'evidence': message['content'], 'operation': 'assert', 'prior_claim_id': None,
        'memory_kind': 'personal_context', 'recall_reason': 'Use the reported location for future visits.',
        'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None,
    }]), message=message['content'], prior=[], observed_at=job['occurred_at'])
    assert len(claims) == 1
    assert projection.commit(job, message, claims, model='controlled-fixture',
                             lease_token=job['lease_token']) == 1
    projection.finish_job(job, model='controlled-fixture')


async def recalled(client, query):
    response = await client.post('/v1/host/context/assemble', json={
        'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'contact-a', 'session_id': 'later'},
        'incoming_message': {'role': 'user', 'content': query}})
    assert response.status_code == 200, response.text
    return next((row for row in response.json()['sections'] if row['id'] == 'pacomind-memory'), None)


@pytest.mark.asyncio
async def test_partial_erase_preserves_independent_atomic_user_claim(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    async def no_semantic_request(*args, **kwargs):
        return [], []
    monkeypatch.setattr(SourceVectors, 'search', no_semantic_request)
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    ledger.record_source('removable-parent', contact_id='contact-a', session_id='work',
        messages=[{'role': 'assistant', 'content': 'A separate temporary support note.'}], derive_claims=False)
    removable = ledger.source_references(['removable-parent'], contact_id='contact-a', session_id='work')[0]
    user = {'role': 'user', 'content': 'My office is in River.'}
    report = {'role': 'assistant', 'content': 'The archive digest matched. Verified at 09:14.'}
    dependent = {'role': 'assistant', 'content': 'An independent temporary remark.',
                 '_supplied_sources': [removable]}
    ledger.record_source('mixed-source', contact_id='contact-a', session_id='work',
                         messages=[user, report, dependent], derive_claims=True)
    commit_user_claim(projection, predicate='office_location', value='River')
    ledger.record_source('unrelated-source', contact_id='contact-a', session_id='other-work',
        messages=[{'role': 'user', 'content': 'My studio is in Meadow.'}], derive_claims=True)
    commit_user_claim(projection, predicate='studio_location', value='Meadow')
    original = ledger.source_references(['mixed-source'], contact_id='contact-a', session_id='later')[0]
    annotation = ledger.append_source_annotation(contact_id='contact-a', session_id='later',
        annotation_id='report-audit', **original, excerpt='Verified at 09:14.',
        correction='The digest comparison is supported, but the verification time was not measured.',
        author_principal='fixture-operator')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        before = await recalled(client, 'office location')
        assert before and 'River' in before['body'] and 'source_assertion' in before['body']
        ledger.erase_sources(contact_id='contact-a', turn_ids=['removable-parent'])
        with sqlite3.connect(ledger.db_path) as db:
            messages = json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                            ('mixed-source',)).fetchone()[0])
            assert messages == [user, report]
            assert db.execute('SELECT count(*) FROM source_claims WHERE turn_id=? AND message_hash=?',
                ('mixed-source', source_message_hash('work', user))).fetchone()[0] == 1
        current = ledger.source_references(['mixed-source'], contact_id='contact-a', session_id='later')[0]
        assert current['source_version'] != original['source_version']
        assert ledger.source_references([annotation['source_id']], contact_id='contact-a', session_id='later') == []
        unrelated = await recalled(client, 'studio location')
        assert unrelated and 'Meadow' in unrelated['body'] and 'source_assertion' in unrelated['body']
        assert {ref['source_id'] for ref in unrelated['citations']} == {'unrelated-source'}
        affected = await recalled(client, 'archive digest verification')
        assert not affected or ('09:14' not in affected['body'] and 'archive digest matched' not in affected['body'].lower())
        retained = await recalled(client, 'office location')
        assert retained is not None, 'The retained user claim disappeared when an unrelated sibling message was erased'
        assert 'River' in retained['body'] and 'source_assertion' in retained['body']
        assert '09:14' not in retained['body'] and 'verification time' not in retained['body']
        assert retained['citations'] == [current]
