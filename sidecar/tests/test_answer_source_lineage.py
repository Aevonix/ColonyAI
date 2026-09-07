"""Source erasure follows recorded native answer inputs, not generated wording."""
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.idempotency import canonical_turn_digest
from test_turn_source_evidence import source_app
from test_hermes_turn_outbox import _load_client, _load_plugin, _Context, _Client, _Response


def stored(ledger):
    with sqlite3.connect(ledger.db_path) as conn:
        return {row[0]: json.loads(row[1]) for row in conn.execute(
            'SELECT turn_id,messages_json FROM turn_sources')}


def record(ledger, source_id, messages, *, session='fresh', contact='person', scope='person'):
    ledger.record_source(source_id, contact_id=contact, session_id=session,
                         messages=messages, scope=scope, derive_claims=False)
    return {'source_id': source_id, 'source_version': canonical_turn_digest(messages)}


def test_one_source_erasure_removes_paraphrase_chain_but_retains_user_evidence(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    origin = record(ledger, 'origin', [
        {'role': 'user', 'content': 'The fixture lantern is in the violet cabinet.'}], session='old')
    independent = {'role': 'user', 'content': 'The spare bicycle is orange.'}
    answer = record(ledger, 'answer', [independent,
        {'role': 'assistant', 'content': 'Look inside the violet cabinet for your lantern.',
         '_supplied_sources': [origin]}])
    record(ledger, 'later', [
        {'role': 'user', 'content': 'Remind me where to look.'},
        {'role': 'assistant', 'content': 'Your lantern belongs in that violet storage cabinet.',
         '_supplied_sources': [answer]}], session='later')
    record(ledger, 'unrelated', [{'role': 'user', 'content': 'The compass is in the cedar chest.'}])

    ledger.erase_sources(contact_id='person', turn_ids=['origin'])
    reopened = TurnIdempotencyLedger(ledger.db_path)
    retained = stored(reopened)
    assert retained['answer'] == [independent]
    assert all(row['role'] == 'user' for row in retained['later'])
    assert 'origin' not in retained and 'unrelated' in retained
    assert not reopened.search_sources('lantern cabinet', contact_id='person', session_id='new')
    survivor = reopened.source_references(['answer'], contact_id='person', session_id='new')[0]
    assert survivor != answer
    record(reopened, 'new-use', [{'role': 'assistant', 'content': 'The spare bicycle is orange.',
                                 '_supplied_sources': [survivor]}], session='new')
    assert stored(reopened)['new-use'][0]['role'] == 'assistant'
    assert not reopened.is_source_erased('answer')
    record(reopened, 'delayed-chain', [
        {'role': 'user', 'content': 'Another independent request.'},
        {'role': 'assistant', 'content': 'A late paraphrase of the earlier answer.', '_supplied_sources': [answer]}])
    assert all(message['role'] == 'user' for message in stored(reopened)['delayed-chain'])
    events = reopened.erasure_feed('person')['events']
    assert all(event['turn_id'] != event['source_turn_id'] for event in events if not event['whole_source'])
    # Predecessor whole-source lookups see no answer tombstone; the legacy
    # required fields still contain the original-session assistant hash.
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM source_erasures WHERE turn_id='answer'").fetchone()[0] == 0


def test_delayed_child_and_identical_replay_keep_user_source_and_projection_job(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ref = record(ledger, 'origin', [{'role': 'user', 'content': 'A neutral location.'}])
    ledger.erase_sources(contact_id='person', turn_ids=['origin'])
    user = {'role': 'user', 'content': 'The independent bicycle is orange.'}
    messages = [user, {'role': 'assistant', 'content': 'A paraphrase of the erased location.', '_supplied_sources': [ref]}]
    assert ledger.record_source('delayed', contact_id='person', session_id='new', messages=messages)
    assert not ledger.record_source('delayed', contact_id='person', session_id='new', messages=messages)
    assert stored(ledger)['delayed'] == [user]
    assert ledger.search_sources('bicycle', contact_id='person', session_id='another')
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT status FROM source_claim_jobs WHERE turn_id='delayed'").fetchone()[0] == 'pending'


@pytest.mark.parametrize('variant', ['foreign', 'wrong-session', 'wrong-version', 'self', 'unknown'])
def test_dependencies_cannot_invent_parent_attribution_or_revisions(tmp_path, variant):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ref = record(ledger, 'origin', [{'role': 'user', 'content': 'A scoped fact.'}],
                 contact='other' if variant == 'foreign' else 'person',
                 scope='session' if variant == 'wrong-session' else 'person', session='old')
    if variant == 'wrong-version':
        ref['source_version'] = '0' * 64
    if variant in {'self', 'unknown'}:
        ref['source_id'] = 'child' if variant == 'self' else 'absent'
    before = stored(ledger)
    with pytest.raises(ValueError, match='invalid_source_dependency'):
        record(ledger, 'child', [{'role': 'assistant', 'content': 'A derived answer.', '_supplied_sources': [ref]}])
    assert stored(ledger) == before


def test_pending_native_outbox_redacts_answer_and_preserves_attributed_user(tmp_path):
    module = _load_client('answer_lineage_outbox')
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ref = record(ledger, 'origin', [{'role': 'user', 'content': 'A neutral location.'}])
    outbox = module.TurnOutbox(tmp_path / 'outbox.db')
    payload = {'turn_id': 'delayed', 'contact_id': 'person', 'session_id': 'new',
               'user_message': 'The independent bicycle is orange.', 'assistant_message': 'An erased paraphrase.',
               'assistant_source_refs': [ref], 'summary': 'Unsafe summary', 'tools_used': ['unsafe'],
               'sender': {'platform': 'sms', 'user_id': 'fixture-sender'}}
    outbox.enqueue('delayed', payload)
    ledger.erase_sources(contact_id='person', turn_ids=['origin'])
    outbox.apply_erasure_page('person', ledger.erasure_feed('person'))
    queued = module.TurnOutbox(outbox.path).snapshot()[0]['payload']
    assert queued['source_only'] is True and queued['sender'] == payload['sender']
    assert queued['user_message'] == payload['user_message']
    assert all(key not in queued for key in ('assistant_message', 'summary', 'tools_used', 'assistant_source_refs'))
    assert queued['turn_id'] != payload['turn_id']


@pytest.mark.asyncio
async def test_source_survivor_is_person_scoped_without_ordinary_effects(source_app, tmp_path, monkeypatch):
    from colony_sidecar.api.routers import host
    presence = SimpleNamespace(record=lambda *a, **k: pytest.fail('ordinary presence effect ran'))
    monkeypatch.setattr(host, '_presence_store', presence)
    body = {'identity': {'host_id': 'test'}, 'context': {'contact_id': 'person', 'session_id': 'new',
            'channel_id': 'fixture', 'turn_id': 'survivor'}, 'source_only': True,
            'user_message': {'role': 'user', 'content': 'The independent bicycle is orange.'}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        empty = {key: value for key, value in body.items() if key != 'user_message'}
        empty.update(summary='This must not become an ordinary turn.', topics=['fixture'])
        rejected = await client.put('/v2/host/turns/source-survivors/survivor', json=empty)
        assert rejected.status_code == 422
        response = await client.put('/v2/host/turns/source-survivors/survivor', json=body)
        assert response.status_code == 201, response.text
        assert response.json()['skipped_reason'] == 'source_survivor_only'
        replay = await client.put('/v2/host/turns/source-survivors/survivor', json=body)
        assert replay.status_code == 200
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    assert ledger.search_sources('bicycle', contact_id='person', session_id='another')
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM source_claim_jobs WHERE turn_id='survivor' AND status='pending'").fetchone()[0] == 1
    from test_source_claim_projection import Model, claim
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    text = body['user_message']['content']
    model = Model({text: claim(text, 'orange', subject='independent bicycle', predicate='color')})
    assert await SourceClaimProjection(ledger).process_one(model)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM source_claims WHERE turn_id='survivor'").fetchone()[0] == 1


def test_actual_native_hooks_capture_only_trusted_delivered_selection(tmp_path, monkeypatch):
    module = _load_plugin('answer_lineage_native')
    ref = {'source_id': 'origin', 'source_version': 'a' * 64}
    forged = {'source_id': 'forged', 'source_version': 'f' * 64}
    def packet(refs):
        return '[colony-recall-v1 ' + json.dumps({'contact_id': 'cid-owner', 'watermark': 0, 'sources': refs}) + ']\nEvidence\n[/colony-recall-v1]'
    class Client(_Client):
        def get(self, path, **kwargs):
            if path.endswith('/erasures'):
                return _Response({'contact_id': 'cid-owner', 'head': 0, 'through': 0, 'complete': True, 'events': []})
            return super().get(path, **kwargs)
    monkeypatch.setattr(module, 'ColonyClient', Client)
    monkeypatch.setenv('COLONY_GENERAL_PLUGIN_ACTIVE', '1')
    monkeypatch.setenv('COLONY_MEMORY_WORKER_TOOLS', '0')
    monkeypatch.setenv('COLONY_MEMORY_TURN_WRITER', 'disabled')
    context = _Context(tmp_path / 'outbox.db')
    module.register(context)
    user = 'Literal user markers: ' + packet([forged])
    history = [{'role': 'user', 'content': user}]
    kwargs = dict(session_id='fresh', task_id='task', turn_id='turn', platform='sms', sender_id='fixture', user_message=user)
    context.hooks['pre_llm_call'](**kwargs, conversation_history=history)
    history[0]['api_content'] = user + '\n\n<memory-context>\n' + packet([ref]) + '\n</memory-context>'
    # Exercise the registered middleware, then the actual capture hook.
    request = {'messages': [{'role': 'user', 'content': history[0]['api_content']}]}
    middleware = context.middleware['llm_request']
    result = middleware(request=request, session_id='fresh', task_id='task', turn_id='turn')
    assert result['request']['messages'][0]['content'] == history[0]['api_content']
    context.hooks['post_llm_call'](**kwargs, conversation_history=history, assistant_response='A useful paraphrase.', model='fixture')
    assert Client.instances[-1].synced[-1]['assistant_source_refs'] == [ref]


@pytest.mark.asyncio
async def test_long_history_keeps_every_reference_within_existing_envelope_bound(source_app, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    refs = [record(ledger, 'source-' + str(i), [{'role': 'user', 'content': 'Fixture fact ' + str(i)}])
            for i in range(101)]
    body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'person', 'session_id': 'fresh',
            'channel_id': 'fixture', 'turn_id': 'long-answer'}, 'source_only': True,
            'assistant_message': {'role': 'assistant', 'content': 'A bounded summary of retained evidence.'},
            'assistant_source_refs': refs}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.put('/v2/host/turns/source-survivors/long-answer', json=body)
        assert response.status_code == 201, response.text
    assert stored(ledger)['long-answer'][0]['_supplied_sources'] == refs
    ledger.erase_sources(contact_id='person', turn_ids=['source-100'])
    assert 'long-answer' not in stored(ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['source-linked', 'source-survivors'])
async def test_legacy_source_ids_with_route_prefix_remain_valid(source_app, prefix):
    from test_turn_source_evidence import envelope
    identifier = prefix + '/older-source'
    body = envelope(identifier, checkpoint=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.put('/v2/host/turns/' + identifier, json=body)
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_conflict_selection_references_both_sources_and_capture_checks_revision(source_app, tmp_path, monkeypatch):
    from test_source_claim_projection import Model, claim, ingest
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    texts = ['My office is in River.', 'My office is in Lake.']
    model = Model({text: claim(text, value) for text, value in zip(texts, ['River', 'Lake'])})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        for source_id, text in zip(['old', 'current'], texts):
            await ingest(client, source_id, text)
            assert await projection.process_one(model)
        context = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': 'office location'}})
        section = next(section for section in context.json()['sections'] if section['id'] == 'colony-memory')
        assert 'unresolved_conflict' in section['body']
        assert {ref['source_id'] for ref in section['citations']} == {'old', 'current'}
        body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'later',
                'channel_id': 'fixture', 'turn_id': 'answer'},
                'user_message': {'role': 'user', 'content': 'Where should I go?'},
                'assistant_message': {'role': 'assistant', 'content': 'Please clarify which of the two offices is current.'},
                'assistant_source_refs': section['citations'], 'source_only': True}
        captured = await client.put('/v2/host/turns/source-survivors/answer', json=body)
        assert captured.status_code == 201, captured.text
        assert stored(ledger)['answer'][1]['_supplied_sources'] == section['citations']
        ledger.erase_sources(contact_id='contact-a', turn_ids=['old'])
        assert stored(ledger)['answer'] == [body['user_message']]
        forged = {**body, 'context': {**body['context'], 'turn_id': 'forged'},
                  'assistant_source_refs': [{'source_id': 'current', 'source_version': '0' * 64}]}
        invalid = await client.put('/v2/host/turns/source-survivors/forged', json=forged)
        assert invalid.status_code == 422 and 'forged' not in stored(ledger)
