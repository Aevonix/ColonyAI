"""Corrections move selected attribution, preserving evidence and revoking derivatives."""
import json

import pytest

from colony_sidecar.turns.idempotency import TurnIdempotencyLedger, SourceErased, canonical_turn_digest
from colony_sidecar.turns.source_attribution import correct, history, visible_hits


def add(ledger, sid, text, **kwargs):
    messages = [{'role': 'user', 'content': text}]
    ledger.record_source(sid, contact_id='cid-old', session_id='session', messages=messages, **kwargs)
    return messages


def correction(ledger, **kwargs):
    return correct(ledger, **(dict(operation_id='identity-1', performed_by='owner-test',
        old_contact_id='cid-old', contact_id='cid-new', source_ids=['source-one'],
        evidence_refs=['source:owner-confirmation']) | kwargs))


def test_selected_correction_preserves_evidence_and_replay_across_restart(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    messages = add(ledger, 'source-one', 'The bicycle is turquoise.', timezone_name='Europe/Paris')
    add(ledger, 'source-other', 'The bicycle has a basket.')
    add(ledger, 'source-import', 'The bicycle has reflectors.', derive_claims=False)
    with ledger._connect() as conn:
        before = dict(conn.execute("SELECT * FROM turn_sources WHERE turn_id='source-one'").fetchone())
        conn.execute("UPDATE source_claim_jobs SET lease_token='old-worker',lease_until=99999999999 WHERE turn_id='source-one'")
    result = correction(ledger)
    assert result['affected_source_ids'] == ['source-one']
    assert result['authority_granted'] is False
    assert result['source_text_preserved'] is True
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert correction(reopened) == result
    assert not reopened.record_source('source-one', contact_id='cid-old', session_id='session', messages=messages)
    with reopened._connect() as conn:
        after = dict(conn.execute("SELECT * FROM turn_sources WHERE turn_id='source-one'").fetchone())
        job = dict(conn.execute("SELECT * FROM source_claim_jobs WHERE turn_id='source-one'").fetchone())
    assert after == before | {'contact_id': 'cid-new'}
    assert job['timezone'] == 'Europe/Paris' and job['lease_token'] == ''
    assert not reopened.search_sources('turquoise', contact_id='cid-old', session_id='later')
    assert reopened.search_sources('turquoise', contact_id='cid-new', session_id='later')
    assert reopened.search_sources('basket', contact_id='cid-old', session_id='later')
    assert history(reopened, source_id='source-one', contact_id='cid-old') == []
    assert history(reopened, source_id='source-one', contact_id='cid-new')[0]['original_digest'] == before['content_sha256']
    correction(ledger, operation_id='import-correction', source_ids=['source-import'])
    with ledger._connect() as conn:
        assert conn.execute("SELECT 1 FROM source_claim_jobs WHERE turn_id='source-import'").fetchone() is None
    with pytest.raises(ValueError, match='operation_conflict'):
        correction(reopened, source_ids=['source-other'])


def test_linked_answer_copies_are_invalidated_without_adopting_or_erasing_them(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    messages = add(ledger, 'source-one', 'The bicycle is turquoise.')
    answer = [{'role': 'assistant', 'content': 'I remember your turquoise bicycle.',
               '_supplied_sources': [{'source_id': 'source-one', 'source_version': canonical_turn_digest(messages)}]}]
    ledger.record_source('answer-one', contact_id='cid-old', session_id='later', messages=answer)
    child = [{'role': 'assistant', 'content': 'Your turquoise bicycle was discussed.',
              '_supplied_sources': [{'source_id': 'answer-one', 'source_version': canonical_turn_digest(answer)}]}]
    ledger.record_source('answer-two', contact_id='cid-old', session_id='next', messages=child)
    result = correction(ledger)
    assert result['invalidated_source_ids'] == ['answer-one', 'answer-two']
    assert not visible_hits(ledger, ledger.search_sources('turquoise', contact_id='cid-old', session_id='later'))
    assert visible_hits(ledger, ledger.search_sources('turquoise', contact_id='cid-new', session_id='later'))
    with ledger._connect() as conn:
        rows = conn.execute("SELECT contact_id,messages_json FROM turn_sources WHERE turn_id LIKE 'answer-%' ORDER BY turn_id").fetchall()
    assert [row['contact_id'] for row in rows] == ['cid-old', 'cid-old']
    assert [json.loads(row['messages_json']) for row in rows] == [answer, child]
    assert ledger.is_projection_erased('answer-two')


def test_conflict_and_erasure_are_atomic_and_do_not_resurrect_sources(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    add(ledger, 'source-one', 'The bicycle is turquoise.')
    add(ledger, 'source-two', 'The helmet is green.')
    with pytest.raises(ValueError, match='preimage_changed'):
        correction(ledger, source_ids=['source-one', 'missing'])
    assert ledger.search_sources('turquoise', contact_id='cid-old', session_id='session')
    ledger.erase_sources(contact_id='cid-old', turn_ids=['source-two'])
    with pytest.raises(SourceErased):
        correction(ledger, source_ids=['source-one', 'source-two'])
    assert ledger.search_sources('turquoise', contact_id='cid-old', session_id='session')
    assert not ledger.search_sources('helmet', contact_id='cid-new', session_id='session')
