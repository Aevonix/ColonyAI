"""Cross-channel corrections must reach active recall and exact transport resolution."""
import json
from types import SimpleNamespace

import pytest

from test_native_request_erasure import runtime
from test_identity_corrections import store
from apsimo.identity.participants import ParticipantResolver
from apsimo.turns.source_attribution import correct, history
from apsimo.turns.idempotency import canonical_turn_digest
from test_turn_source_evidence import source_app


@pytest.mark.parametrize('shape', ['chat', 'responses'])
def test_current_cached_recall_is_withheld_when_its_source_changes_person(runtime, source_app, monkeypatch, shape):
    import importlib
    import time
    from fastapi.testclient import TestClient
    import apsimo.turns
    from apsimo.api.middleware import ApiKeyMiddleware
    rt = runtime
    # This test verifies attribution changes through the real source API, not
    # disk latency on a shared CI worker. Keep its local freshness clock fixed;
    # the dedicated request/outbox deadline tests retain their real clocks.
    clock = SimpleNamespace(monotonic=lambda: 1000.0, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(rt.module, 'time', clock)
    monkeypatch.setattr(importlib.import_module(type(rt.outbox).__module__), 'time', clock)
    monkeypatch.setattr(apsimo.turns, 'get_turn_idempotency_ledger', lambda *args: rt.ledger)
    source_app.add_middleware(ApiKeyMiddleware, api_key='identity-continuity-fixture')
    api = TestClient(source_app)
    calls = []
    def method(name):
        def dispatch(path, **kwargs):
            kwargs.pop('_deadline_monotonic', None)
            kwargs.pop('timeout', None)
            kwargs['headers'] = {'Authorization': 'Bearer identity-continuity-fixture'}
            response = getattr(api, name)(path, **kwargs)
            calls.append((name, path, response.status_code))
            return response
        return dispatch
    boundary = rt.module.RequestMemory(SimpleNamespace(get=method('get'), post=method('post')), rt.outbox)
    scope = SimpleNamespace(contact_id='owner', session_id='later', task_id='task', turn_id='turn',
                            valid_participant=True)
    ref = rt.ledger.source_references(['fixture-source'], contact_id='owner', session_id='later')[0]
    answer = [{'role': 'assistant', 'content': 'The orchard badge is cobalt-716.', '_supplied_sources': [ref]}]
    rt.ledger.record_source('answer', contact_id='owner', session_id='earlier-answer', messages=answer)
    answer_ref = rt.ledger.source_references(['answer'], contact_id='owner', session_id='later')[0]
    child = [{'role': 'assistant', 'content': 'The badge was cobalt-716.', '_supplied_sources': [answer_ref]}]
    rt.ledger.record_source('child-answer', contact_id='owner', session_id='earlier-child', messages=child)
    with rt.ledger._connect() as conn:
        original = [tuple(row) for row in conn.execute('SELECT turn_id,messages_json,content_sha256,ingested_at FROM turn_sources ORDER BY turn_id')]
    current = {'role': 'user', 'content': 'What was the badge?'}
    boundary.observe(scope, [current], user_message=current['content'])
    stamp = json.dumps({'contact_id': 'owner', 'watermark': 0, 'sources': [ref]})
    current['api_content'] = current['content'] + '\n\n<memory-context>\n[colony-recall-v1 ' + stamp + ']\n' + rt.fact + '\n[/colony-recall-v1]\n</memory-context>'
    key = 'messages' if shape == 'chat' else 'input'
    request = {key: [{'role': 'user', 'content': current['api_content']}]}
    assert rt.fact in json.dumps(boundary(request, scope)['request'])
    opened = json.dumps({'colony_source_read_v1': True, 'content': rt.fact})
    assert boundary.register_source_read(scope, 'opened', opened, {'watermark': 0, 'source_refs': [ref]})
    request[key].append({'role': 'tool', 'tool_call_id': 'opened', 'content': opened} if shape == 'chat'
                       else {'type': 'function_call_output', 'call_id': 'opened', 'output': opened})
    assert opened in json.dumps(boundary(request, scope)['request']).replace('\\"', '"')
    correction = correct(rt.ledger, operation_id='move-source', performed_by='owner-fixture',
            old_contact_id='owner', contact_id='different-person', source_ids=['fixture-source'],
            evidence_refs=['fixture:owner-correction'])
    assert correction['invalidated_source_ids'] == ['answer', 'child-answer']
    assert rt.ledger.erasure_watermark('owner') == 0
    assert not rt.ledger.source_references(['fixture-source'], contact_id='owner', session_id='later')
    assert rt.fact not in json.dumps(boundary(request, scope)['request'])
    assert rt.fact in current['api_content'] and request[key][-1].get('content', request[key][-1].get('output')) == opened
    assert rt.ledger.source_references(['fixture-source'], contact_id='different-person', session_id='other-channel') == [ref]
    assert rt.ledger.source_references(['answer', 'child-answer'], contact_id='owner', session_id='later') == []
    correct(rt.ledger, operation_id='reverse-source', performed_by='owner-fixture',
            old_contact_id='different-person', contact_id='owner', source_ids=['fixture-source'],
            evidence_refs=['fixture:owner-reversal'])
    assert rt.ledger.source_references(['fixture-source'], contact_id='owner', session_id='voice-channel') == [ref]
    assert rt.ledger.source_references(['answer', 'child-answer'], contact_id='owner', session_id='voice-channel') == []
    assert rt.fact in json.dumps(boundary(request, scope)['request'])
    assert len(history(rt.ledger, source_id='fixture-source', contact_id='owner')) == 2
    with rt.ledger._connect() as conn:
        assert [tuple(row) for row in conn.execute('SELECT turn_id,messages_json,content_sha256,ingested_at FROM turn_sources ORDER BY turn_id')] == original
    with pytest.raises(ValueError, match='invalid_source_dependency'):
        rt.ledger.record_source('stale-child-completion', contact_id='owner', session_id='after-reversal',
            messages=[{'role': 'assistant', 'content': 'Repeating the old conclusion.', '_supplied_sources': [
                {'source_id': 'child-answer', 'source_version': canonical_turn_digest(child)}]}])
    assert calls and set(calls) == {('post', '/v1/host/memory/sources/erasures', 200)}


@pytest.mark.asyncio
async def test_freshness_feed_preserves_scoped_authority_and_source_session(runtime, source_app, monkeypatch, tmp_path):
    import apsimo.turns
    from httpx import ASGITransport, AsyncClient
    from test_scoped_api_authority import _principal, _write_keyring, _app, _headers
    rt = runtime
    monkeypatch.setattr(apsimo.turns, 'get_turn_idempotency_ledger', lambda *args: rt.ledger)
    keyring = tmp_path / 'scoped-fixture.json'
    _write_keyring(keyring, [_principal(viewer='owner', scopes=['turns:write']),
        _principal(principal='memory-only', secret='memory-fixture', viewer='owner', scopes=['memory:read'])])
    app = _app(keyring)
    rt.ledger.record_source('session-only', contact_id='owner', session_id='original', scope='session',
        messages=[{'role': 'user', 'content': 'A session-limited fixture.'}], derive_claims=False)
    ref = rt.ledger.source_references(['session-only'], contact_id='owner', session_id='original')[0]
    body = {'contact_id': 'owner', 'session_id': 'original', 'source_refs': [ref]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test', headers=_headers('scoped-secret')) as api:
        checked = await api.post('/v1/host/memory/sources/erasures', json=body)
        assert checked.status_code == 200 and checked.json()['sources_current'] is True
        foreign = await api.post('/v1/host/memory/sources/erasures', json=body | {'contact_id': 'another-person'})
        assert foreign.status_code == 403
        for invalid in (body | {'session_id': 'another-session'},
                        body | {'source_refs': [ref | {'source_version': 'f' * 64}]},
                        body | {'source_refs': [ref | {'source_id': 'missing'}]}):
            checked = await api.post('/v1/host/memory/sources/erasures', json=invalid)
            assert checked.status_code == 200 and checked.json()['sources_current'] is False
            assert 'session-limited' not in checked.text
        assert (await api.post('/v1/host/memory/sources/erasures', json=body,
            headers=_headers('memory-fixture'))).status_code == 403
        assert (await api.post('/v1/host/memory/sources/erasures', json=body | {'source_refs': [ref] * 513})).status_code == 422
        assert (await api.get('/v1/host/memory/sources/erasures', params={'contact_id': 'owner'})).status_code == 200


@pytest.mark.asyncio
async def test_exact_corrected_phone_handle_does_not_become_an_unknown_third_person(store):
    first = await store.create(display_name='First')
    second = await store.create(display_name='Second')
    await store.add_handle(first.contact_id, 'sms', '+12125550101', verified=True)
    await store.add_handle(first.contact_id, 'whatsapp', '12125550101@s.whatsapp.net', verified=True)
    await store.correct_handle_identity(operation_id='split-channel', performed_by='owner-fixture',
        gateway='whatsapp', address='12125550101@s.whatsapp.net', expected_contact_id=first.contact_id,
        contact_id=second.contact_id, evidence_refs=['fixture:owner-correction'])
    resolved = await ParticipantResolver(store).resolve(platform='whatsapp', user_id='12125550101@s.whatsapp.net')
    assert resolved.contact_id == second.contact_id
    assert (await store.resolve_messaging_handle('sms', '+12125550101')).contact_id == first.contact_id
    assert len(await store.list()) == 2
    await store.close()
    await store.connect()
    await store.correct_handle_identity(operation_id='reverse-channel', performed_by='owner-fixture',
        gateway='whatsapp', address='12125550101@s.whatsapp.net', expected_contact_id=second.contact_id,
        contact_id=first.contact_id, evidence_refs=['fixture:owner-reversal'])
    assert (await ParticipantResolver(store).resolve(platform='whatsapp', user_id='12125550101@s.whatsapp.net')).contact_id == first.contact_id
    assert (await store.resolve_messaging_handle('rcs', '(212) 555-0101')).contact_id == first.contact_id
    assert len(await store.list()) == 2


@pytest.mark.asyncio
async def test_inflight_extractor_cannot_commit_old_person_interpretation_after_correction(tmp_path):
    import asyncio
    from apsimo.turns import TurnIdempotencyLedger
    from apsimo.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import Model, claim
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    text = 'My office is in River.'
    ledger.record_source('inflight', contact_id='old-person', session_id='text',
                         messages=[{'role': 'user', 'content': text}])
    entered, release = asyncio.Event(), asyncio.Event()
    class PausedExtractor(Model):
        async def complete(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 2)
            return await super().complete(*args, **kwargs)
    task = asyncio.create_task(SourceClaimProjection(ledger).process_one(PausedExtractor({text: claim(text, 'River')})))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        correct(ledger, operation_id='extractor-race', performed_by='owner-fixture',
                old_contact_id='old-person', contact_id='new-person', source_ids=['inflight'],
                evidence_refs=['fixture:correction-during-extraction'])
    finally:
        release.set()
    assert await task
    with ledger._connect() as conn:
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 0
        assert conn.execute("SELECT status FROM source_claim_jobs WHERE turn_id='inflight'").fetchone()[0] == 'pending'
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert await SourceClaimProjection(reopened).process_one(Model({text: claim(text, 'River')}, model='new-fixture-processor'))
    with reopened._connect() as conn:
        rows = conn.execute('SELECT s.contact_id,j.status,j.model FROM source_claims c '
            'JOIN turn_sources s USING(turn_id) JOIN source_claim_jobs j USING(turn_id)').fetchall()
    assert [tuple(row) for row in rows] == [('new-person', 'complete', 'new-fixture-processor')]
