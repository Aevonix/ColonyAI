"""Ordinary source capture reaches scoped guidance and owner correction."""
import json
import importlib

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import social_state
from colony_sidecar.turns import TurnIdempotencyLedger
from test_canonical_scoped_context import context, headers
from test_scoped_api_authority import _principal, _write_keyring
from test_source_appraisals import Processor, observation
from test_turn_source_evidence import source_app
from test_hermes_turn_outbox import _load_plugin
from test_native_request_erasure import packet


@pytest.mark.asyncio
async def test_capture_reflection_recall_correction_and_erasure(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    keys = tmp_path/'keys.json'
    _write_keyring(keys, [_principal(principal=p, secret='fixture-'+p, viewer=p)
                          for p in ('owner', 'person', 'stranger')])
    source_app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keys))
    source_app.include_router(social_state.router)
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        captured = await client.put('/v2/host/turns/explicit-preference', headers=headers('person'), json={
            'identity': {'host_id': 'fixture'},
            'context': {'contact_id': 'person', 'session_id': 'first', 'turn_id': 'explicit-preference'},
            'user_message': {'role': 'user', 'content': 'Please keep export explanations concise.'}})
        assert captured.status_code == 201, captured.text
        store = social_state.appraisal_store()
        await store.process_one(Processor(lambda p: observation(p, kind='preference',
            dimension='communication', hint='keep_concise')))
        recalled = await client.post('/v1/host/context/assemble', headers=headers('person'),
                                     json=context('person', 'export task', session='later-channel'))
        assert recalled.status_code == 200, recalled.text
        sections = {s['id']: s for s in recalled.json()['sections']}
        section = sections['colony-appraisals']
        assert 'Keep relevant explanations concise' in section['body']
        assert section['citations'][0]['source_id'] == 'explicit-preference'
        # Inspection permits the owner, while other contacts cannot select this person.
        inspected = await client.get('/v1/host/social/appraisals', headers=headers('owner'),
            params={'contact_id': 'owner', 'subject_id': 'person'})
        record = inspected.json()['records'][0]
        denied = await client.get('/v1/host/social/appraisals', headers=headers('stranger'),
            params={'contact_id': 'stranger', 'subject_id': 'person'})
        assert denied.status_code == 403
        correction = {'contact_id': 'owner', 'record_id': record['id'], 'action': 'withdraw',
                      'correction_id': 'owner-fix', 'reason': 'This preference was task-specific.'}
        changed = await client.post('/v1/host/social/appraisals/correct', headers=headers('owner'), json=correction)
        assert changed.status_code == 200 and changed.json()['created']
        replay = await client.post('/v1/host/social/appraisals/correct', headers=headers('owner'), json=correction)
        assert replay.status_code == 200 and replay.json()['created'] is False
        after = await client.post('/v1/host/context/assemble', headers=headers('person'), json=context('person', 'export task'))
        assert 'colony-appraisals' not in [s['id'] for s in after.json()['sections']]
        ledger.erase_sources(contact_id='person', turn_ids=['explicit-preference'])
        assert store.view('person', viewer_contact_id='owner', history=True)['records'] == []
        with ledger._connect() as conn:
            assert all(json.loads(r[0]) == {} for r in conn.execute('SELECT operation_json FROM appraisal_corrections'))


@pytest.mark.asyncio
async def test_withdrawn_social_hint_is_not_replayed_as_current_request_guidance(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    text = 'Please keep export explanations concise.'
    ledger.record_source('preference', contact_id='person', session_id='earlier',
                         messages=[{'role': 'user', 'content': text}])
    store = social_state.appraisal_store()
    from colony_sidecar.self_model import appraisals
    with ledger._connect() as db, db:
        appraisals.enqueue(db, 'preference', 'person', [{'role': 'user', 'content': text}], scope='person')
    await store.process_one(Processor(lambda p: observation(p, kind='preference',
        dimension='communication', hint='keep_concise')))
    prior, sources = social_state.appraisal_context(contact_id='person', session_id='earlier', query='export task')
    hint = 'Keep relevant explanations concise.'
    assert hint in prior and sources
    watermark = ledger.erasure_feed(contact_id='person', after=0)['head']
    request = {'messages': [
        {'role': 'user', 'content': 'Earlier export request\n' + packet('person', watermark, prior)},
        {'role': 'assistant', 'content': 'Earlier response.'},
        {'role': 'user', 'content': 'New export request'}]}
    record = store.view('person', viewer_contact_id='owner')['records'][0]
    store.correct(record['id'], action='withdraw', correction_id='withdraw-preference',
                  reason='The earlier preference applied only to one task.', actor_id='owner')
    current, _ = social_state.appraisal_context(contact_id='person', session_id='another', query='export task')
    assert hint not in current
    plugin = _load_plugin('social_withdrawn_history')
    module = importlib.import_module(plugin.__name__ + '.request_memory')
    feed = ledger.erasure_feed(contact_id='person', after=0)
    filtered = module.filter_request(request, contact_id='person', watermark=feed['head'],
                                     rules=feed['events'], fresh=True)
    assert 'Earlier export request' in json.dumps(filtered)
    assert hint not in json.dumps(filtered), 'withdrawn social hint survived in a historical memory packet'
