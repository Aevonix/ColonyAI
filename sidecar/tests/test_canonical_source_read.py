"""Scoped complete-source opening, stable paging, and honest oversized conflicts."""
from datetime import datetime, timezone
import json

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.beliefs.source_time import interpret_time_query
from colony_sidecar.intelligence.graph.recall import pack_memory_context
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.source_read import read
from test_scoped_api_authority import _principal, _write_keyring
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


def ref(ledger, identifier='source', **kwargs):
    return ledger.source_references([identifier], contact_id='person', session_id='later', **kwargs)[0]


def opened(ledger, identifier='source', **kwargs):
    return read(ledger, contact_id='person', session_id='later', **ref(ledger, identifier), **kwargs)


def test_long_procedure_opens_every_step_and_scope_or_revision_never_widens(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    text = 'Pump procedure: isolate pressure. ' + 'Check the seal; ' * 700 + 'Only then reconnect power.'
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'user', 'content': text}])
    first = opened(ledger)
    assert not first['complete'] and len(first['content']) == 4096
    pages, page = [first['content']], first
    while not page['complete']:
        page = opened(ledger, offset=page['next_offset'], read_revision=page['read_revision'])
        pages.append(page['content'])
    assert json.loads(''.join(pages))['messages'] == [{'role': 'user', 'content': text}]
    assert first['source_refs'] == [ref(ledger)]
    for scope in ({'contact_id': 'other', 'session_id': 'later'},):
        with pytest.raises(ValueError, match='unavailable'):
            read(ledger, **scope, **ref(ledger))
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', source_id='source', source_version='0'*64)
    saved = ref(ledger)
    ledger.erase_sources(contact_id='person', turn_ids=['source'])
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **saved)


def test_checkpoint_opening_stays_in_its_session_and_correction_changes_continuation(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    ledger.record_source('private-session', contact_id='person', session_id='original', scope='session',
                         messages=[{'role': 'user', 'content': 'Session-scoped appendix.'}])
    private_ref = ledger.source_references(['private-session'], contact_id='person', session_id='original')[0]
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **private_ref)
    text = 'Archive digest matched. ' + 'Recorded details. ' * 500
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'assistant', 'content': text}])
    first = opened(ledger)
    correction = 'The digest was not compared; that claim is unsupported.'
    ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='note',
        **ref(ledger), excerpt='Archive digest matched.', correction=correction, author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, offset=first['next_offset'], read_revision=first['read_revision'])
    content, page = '', opened(ledger)
    while True:
        content += page['content']
        if page['complete']:
            break
        page = opened(ledger, offset=page['next_offset'], read_revision=page['read_revision'])
    assert correction in content and 'attributed_correction' in content
    assert len(page['source_refs']) == 2


@pytest.mark.asyncio
async def test_oversized_conflict_is_discoverable_and_history_pages_current_sources(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    projection = SourceClaimProjection(ledger)
    for index in range(10):
        text = f'My office is in District{index}.'
        ledger.record_source(f's{index}', contact_id='person', session_id='original',
            messages=[{'role': 'user', 'content': text}])
        await projection.process_one(Model({text: claim(text, f'District{index}')}))
    _, rows = projection.prepare_context([], ledger.search_sources('office', contact_id='person', session_id='later'),
        contact_id='person', session_id='later', time_query=interpret_time_query('office', now=datetime.now(timezone.utc)))
    selected, packet = pack_memory_context(rows)
    assert 'incomplete_assertion_history' in packet and 'history_anchor' in packet
    assert not any(f'District{i}' in packet for i in range(10))
    anchor = selected[0]['history_anchor']
    first = opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'])
    second = opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'],
                    offset=first['next_offset'], read_revision=first['read_revision'])
    assert first['total'] == 10 and not first['complete'] and second['complete']
    assertions = json.loads(first['content'])['assertions'] + json.loads(second['content'])['assertions']
    assert {c['value'] for c in assertions} == {f'District{i}' for i in range(10)}
    assert all(c['superseded_by'] is None and c['retracted_by'] is None for c in assertions)
    victim = next(c['turn_id'] for c in assertions if c['turn_id'] != anchor['source_id'])
    ledger.erase_sources(contact_id='person', turn_ids=[victim])
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'],
               offset=first['next_offset'], read_revision=first['read_revision'])
    assert opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'])['total'] == 9


@pytest.mark.asyncio
async def test_memory_read_canonical_mode_enforces_principal_without_graph(source_app, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'user', 'content': 'Useful procedure.'}])
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='reader', secret='read', viewer='person', scopes=['memory:read']),
                            _principal(principal='other', secret='other', viewer='other', scopes=['memory:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    body = {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later', **ref(ledger)}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        good = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer read'})
        assert good.status_code == 200 and good.json()['source']['complete'], good.text
        denied = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer other'})
        assert denied.status_code == 403
        missing_cursor = await client.post('/v1/host/memory/read', json={**body, 'offset': 1}, headers={'Authorization': 'Bearer read'})
        assert missing_cursor.status_code == 422
