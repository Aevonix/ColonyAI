"""Exact input parents survive media normalization and delayed delivery."""
import copy
import json
import sqlite3

import httpx
import pytest

from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.idempotency import SourceErased, SourceInputPending, source_message_hash
from test_answer_source_lineage import stored
from test_turn_source_evidence import source_app
from test_source_media import message as image_message
from test_hermes_turn_outbox import _load_client


def parent_ref(message, source='parent', session='original'):
    return {'source_id': source, 'input_message_hash': source_message_hash(session, message)}


@pytest.mark.parametrize('multimodal', [False, True])
def test_admitted_input_hash_resolves_exact_canonical_parent_and_erases_paraphrase(tmp_path, multimodal):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    parent = image_message() if multimodal else {'role': 'user', 'content': 'My fixture lamp is violet.'}
    ref = parent_ref(parent)
    answer = [{'role': 'assistant', 'content': 'Your lamp has a purple finish.', '_supplied_inputs': [ref]}]
    original = copy.deepcopy(answer)
    with pytest.raises(SourceInputPending):
        ledger.record_source('reply', contact_id='person', session_id='call', messages=answer)
    ledger.record_source('parent', contact_id='person', session_id='original', messages=[parent], derive_claims=False)
    assert ledger.record_source('reply', contact_id='person', session_id='call', messages=answer)
    assert not ledger.record_source('reply', contact_id='person', session_id='call', messages=answer)
    assert answer == original
    resolved = ledger.source_references(['parent'], contact_id='person', session_id='call')
    assert stored(ledger)['reply'][0]['_supplied_sources'] == resolved
    assert stored(ledger)['reply'][0]['_supplied_inputs'] == [ref]
    ledger.erase_sources(contact_id='person', turn_ids=['parent'])
    assert stored(ledger) == {}
    with pytest.raises(SourceErased):
        ledger.record_source('late', contact_id='person', session_id='call', messages=answer)
    assert stored(ledger) == {}


@pytest.mark.parametrize('variant', ['foreign', 'wrong-session', 'wrong-hash', 'self', 'assistant-parent'])
def test_input_parent_cannot_change_scope_or_invent_current_utterance(tmp_path, variant):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    parent = {'role': 'assistant' if variant == 'assistant-parent' else 'user', 'content': 'A scoped fixture.'}
    ref = parent_ref(parent)
    ledger.record_source('parent', contact_id='other' if variant == 'foreign' else 'person',
        session_id='original', scope='session' if variant == 'wrong-session' else 'person',
        messages=[parent], derive_claims=False)
    if variant == 'wrong-hash': ref['input_message_hash'] = '0'*64
    if variant == 'self': ref['source_id'] = 'child'
    with pytest.raises(ValueError, match='invalid_source_input_dependency'):
        ledger.record_source('child', contact_id='person', session_id='later',
            messages=[{'role': 'assistant', 'content': 'A derived statement.', '_supplied_inputs': [ref]}])
    assert 'child' not in stored(ledger)


@pytest.mark.asyncio
async def test_missing_parent_does_not_reserve_effects_and_replay_succeeds(source_app, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    parent = {'role': 'user', 'content': 'My fixture lamp is violet.'}
    body = {'identity': {'host_id': 'fixture'}, 'context': {'session_id': 'call', 'contact_id': 'person',
            'channel_id': 'fixture', 'turn_id': 'reply'}, 'source_only': True,
            'assistant_message': {'role': 'assistant', 'content': 'Your lamp has a purple finish.'},
            'assistant_input_refs': [parent_ref(parent)]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        path = '/v2/host/turns/source-linked/input-parent/reply'
        missing = await client.put(path, json=body)
        assert missing.status_code == 202 and not missing.json()['accepted']
        assert missing.json()['skipped_reason'] == 'source_input_parent_pending'
        with sqlite3.connect(ledger.db_path) as connection:
            assert connection.execute('SELECT count(*) FROM turn_ingestion').fetchone()[0] == 0
        ledger.record_source('parent', contact_id='person', session_id='original', messages=[parent], derive_claims=False)
        captured = await client.put(path, json=body)
        assert captured.status_code == 201 and captured.json()['source_recorded']
        assert (await client.put(path, json=body)).status_code == 200
    ledger.erase_sources(contact_id='person', turn_ids=['parent'])
    assert stored(ledger) == {}


def test_offline_outbox_removes_input_dependent_answer_before_replay(tmp_path):
    client = _load_client('input_parent_outbox')
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    parent = image_message()
    ref = parent_ref(parent)
    ledger.record_source('parent', contact_id='person', session_id='original', messages=[parent], derive_claims=False)
    outbox = client.TurnOutbox(tmp_path/'outbox.db')
    outbox.enqueue('late', {'turn_id': 'late', 'contact_id': 'person', 'session_id': 'call',
        'assistant_message': 'A paraphrase of the captured picture.', 'assistant_input_refs': [ref]})
    ledger.erase_sources(contact_id='person', turn_ids=['parent'])
    client.TurnOutbox(outbox.path).apply_erasure_page('person', ledger.erasure_feed('person'))
    assert outbox.snapshot() == []


@pytest.mark.asyncio
async def test_predecessor_linked_route_rejects_parent_protocol_before_ingestion(source_app, monkeypatch):
    from apsimo.api.routers import host
    from apsimo.api.schemas.host import TurnSyncRequest
    from fastapi import HTTPException, Response
    monkeypatch.setattr(host, 'turns_sync_v2', lambda *args, **kwargs: pytest.fail('old route ingested unknown parents'))
    body = TurnSyncRequest.model_validate({'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'person', 'session_id': 'call', 'turn_id': 'reply'},
        'assistant_message': {'role': 'assistant', 'content': 'A dependent response.'},
        'assistant_source_refs': [{'source_id': 'prior', 'source_version': 'a'*64}]})
    # This is the exact predecessor's generic linked-route dispatch argument.
    with pytest.raises(HTTPException) as error:
        await host.source_linked_sync('input-parent/reply', body, Response())
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_existing_literal_source_id_with_new_route_prefix_remains_valid(source_app):
    from test_turn_source_evidence import envelope
    identifier = 'source-linked/input-parent/older-source'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = await client.put('/v2/host/turns/' + identifier, json=envelope(identifier, checkpoint=True))
    assert result.status_code == 201
