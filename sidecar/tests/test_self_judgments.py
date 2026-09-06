"""Ordinary sources → revisable agent stance → relevant scoped conversation."""
import asyncio
import json
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.self_model.judgments import SelfJudgments
from colony_sidecar.turns import TurnIdempotencyLedger
from test_self_perspective import perspective, tell
from test_turn_source_evidence import source_app


class Clock:
    def __init__(self):
        self.value = 1800000000.0

    def __call__(self):
        return self.value


class Processor:
    supports_function_routing = True

    def __init__(self, name='processor-a', *, decide=None, before_return=None, deadline=80):
        self.name, self.decide, self.before_return, self.deadline = name, decide, before_return, deadline
        self.requests = []

    def function_deadline_seconds(self, *, context):
        assert context['function_role'] == 'reasoning'
        return self.deadline

    async def complete(self, *, messages, context):
        assert context == {'task': 'self_judgment', 'function_role': 'reasoning', 'allow_fallback': True}
        payload = json.loads(messages[-1]['content'])
        self.requests.append(payload)
        if self.before_return:
            await self.before_return(payload)
        result = self.decide(payload) if self.decide else revise(payload)
        return SimpleNamespace(content=json.dumps(result), raw=None, model_id=self.name,
                               binding=self.name + '-binding', config_revision='config-' + self.name,
                               model_revision='weights-' + self.name)


def revise(payload, *, stance='I favor explicit checkpoints for long local work.', contrary=False):
    previous = next((p for p in payload['previous_judgments'] if p['topic'] == 'local work checkpoints'), None)
    handle = payload['evidence'][0]['handle']
    return {'action': 'revise', 'topic': 'local work checkpoints',
            'supersedes': previous['id'] if previous else None, 'stance': stance,
            'reason': 'The reported interruption shows a recovery benefit; that benefit must be weighed against measured overhead.',
            'certainty': 'tentative', 'support': [handle], 'contrary': [handle] if contrary else []}


@pytest.fixture
def judgments(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    clock = Clock()
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    return SelfJudgments(ledger, owner_id='contact-a', clock=clock), clock


def source(judgments, turn='first', text='Long local work lost progress after an interruption. Checkpoints could help.', **kwargs):
    judgments.ledger.record_source(turn, contact_id=kwargs.pop('contact_id', 'contact-a'), session_id='session-' + turn,
        messages=[{'role': 'user', 'content': text}], **kwargs)


def run_row(judgments, turn):
    with judgments.ledger._connect() as conn:
        return dict(conn.execute('SELECT * FROM self_judgment_runs WHERE turn_id=?', (turn,)).fetchone())


@pytest.mark.asyncio
async def test_two_processors_revise_with_history_restart_and_relevant_owner_context(source_app, perspective, monkeypatch):
    state, learner, _ = perspective
    clock = Clock()
    state.judgments.clock = clock
    first = Processor('model-a')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, 'Long local work lost progress after interruption; checkpoints restored it.', 'judgment-a')
        assert await state.judgments.process_one(first)
        assert len(state.judgments.revisions()) == 1
        assert state.preferences() == []
        clock.value += 86401
        await tell(client, 'For short local work, frequent checkpoints doubled run time without preventing any lost progress.', 'judgment-b')
        second = Processor('model-b', decide=lambda p: revise(p, stance='I favor checkpoints at meaningful stages, rather than after every short operation.', contrary=True))
        assert await state.judgments.process_one(second)
        assert second.requests[0]['previous_evidence'][0]['text'] == 'Long local work lost progress after interruption; checkpoints restored it.'
        history = state.judgments.revisions(history=True)
        assert [r['processor']['model_id'] for r in history] == ['model-b', 'model-a']
        assert history[0]['supersedes'] == history[1]['id']
        assert history[0]['contrary'][0]['turn_id'] == 'judgment-b'
        assert history[0]['applies_to'] == 'owner_turn_deliberation' and history[0]['authority_changed'] is False
        from colony_sidecar.self_model.perspective import SelfPerspective
        reopened = SelfPerspective(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=clock)
        learner.perspective = reopened
        for person, token, query, expected in [
            ('contact-a', 'owner-key', 'What is your view on local work checkpoints?', True),
            ('contact-a', 'owner-key', 'What should I cook for dinner?', False),
            ('contact-b', 'guest-key', 'What is your view on local work checkpoints?', False),
        ]:
            response = await client.post('/v1/host/context/assemble', headers={'Authorization': 'Bearer ' + token}, json={
                'identity': {'host_id': 'fixture'}, 'context': {'contact_id': person, 'session_id': 'later-' + person},
                'incoming_message': {'role': 'user', 'content': query}})
            assert response.status_code == 200
            text = '\n'.join(s['body'] for s in response.json()['sections'])
            assert ('I favor checkpoints at meaningful stages' in text) is expected
            if expected:
                assert 'fallible agent judgments' in text and 'Source turn:judgment-b' in text
        assert not await reopened.judgments.process_one(second)  # no repeated source vote
        from colony_sidecar.api.routers import host
        unattested = await host.context_assemble(host.ContextAssembleRequest(
            identity={'host_id': 'fixture'}, context={'contact_id': 'contact-a', 'session_id': 'unattested'},
            incoming_message={'role': 'user', 'content': 'What is your view on local work checkpoints?'}), request=None)
        assert not any('I favor checkpoints at meaningful stages' in s.body for s in unattested.sections)


@pytest.mark.asyncio
async def test_contrary_source_survives_topic_interval_and_reopen(judgments):
    state, clock = judgments
    source(state)
    await state.process_one(Processor())
    source(state, 'contrary', 'Local work checkpoints cost more time than the work they recover.')
    second = Processor('processor-b', decide=lambda p: revise(p, stance='I now prefer fewer checkpoints for short local work.', contrary=True))
    await state.process_one(second)
    row = run_row(state, 'contrary')
    assert row['status'] == 'pending' and row['disposition'] == 'topic_rate_limited'
    assert row['next_attempt'] == clock.value + 86400
    assert not await state.process_one(second)
    clock.value += 86401
    reopened = SelfJudgments(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=clock)
    assert await reopened.process_one(second)
    assert len(reopened.revisions(history=True)) == 2
    assert reopened.revisions()[0]['contrary'][0]['turn_id'] == 'contrary'


@pytest.mark.asyncio
async def test_erasure_removes_derived_history_and_keeps_head_tombstone(judgments):
    state, clock = judgments
    source(state)
    await state.process_one(Processor())
    clock.value += 86401
    source(state, 'later', 'Local work checkpoints now have lower overhead.')
    await state.process_one(Processor('processor-b'))
    state.ledger.erase_sources(contact_id='contact-a', turn_ids=['first'])
    assert state.revisions() == []
    assert all(r['status'] == 'erased' for r in state.revisions(history=True))
    with state.ledger._connect() as conn:
        assert all(r[0] == '{}' and r[1] == '' for r in conn.execute('SELECT payload_json,topic FROM self_judgment_revisions'))
    clock.value += 86401
    source(state, 'fresh', 'A new local work experiment measured useful checkpoint recovery.')
    await state.process_one(Processor('processor-c'))
    assert len(state.revisions()) == 1  # fresh evidence may establish a new view
    assert state.revisions()[0]['supersedes'] is not None


@pytest.mark.asyncio
async def test_forgetting_during_inference_cannot_commit(judgments):
    state, _ = judgments
    source(state)
    async def erase(_payload):
        state.ledger.erase_sources(contact_id='contact-a', turn_ids=['first'])
    await state.process_one(Processor(before_return=erase))
    assert state.revisions(history=True) == []


@pytest.mark.asyncio
async def test_later_topic_head_prevents_stale_model_commit(judgments, monkeypatch):
    state, clock = judgments
    monkeypatch.setenv('COLONY_SELF_JUDGMENT_INTERVAL_SECONDS', '0')
    source(state)
    await state.process_one(Processor())
    source(state, '01-slow', 'Local work checkpoints have one measured benefit.')
    source(state, '02-fast', 'Local work checkpoints have a different measured cost.')
    ready, release = asyncio.Event(), asyncio.Event()
    async def wait(_payload):
        ready.set()
        await release.wait()
    slow = asyncio.create_task(state.process_one(Processor('slow-model', before_return=wait)))
    await ready.wait()
    assert await state.process_one(Processor('fast-model'))
    release.set()
    await slow
    assert state.revisions()[0]['processor']['model_id'] == 'fast-model'
    assert run_row(state, '01-slow')['disposition'] == 'head_changed'
    assert run_row(state, '01-slow')['status'] == 'pending'


@pytest.mark.asyncio
async def test_real_lease_tracks_role_deadline_and_reclaimed_worker_is_fenced(judgments):
    state, clock = judgments
    source(state)
    ready, release = asyncio.Event(), asyncio.Event()
    async def wait(_payload):
        ready.set()
        await release.wait()
    slow = asyncio.create_task(state.process_one(Processor('stale-model', before_return=wait, deadline=300)))
    await ready.wait()
    assert run_row(state, 'first')['lease_until'] == clock.value + 335
    clock.value += 100
    assert not await state.process_one(Processor('competing-model'))
    clock.value += 236
    assert await state.process_one(Processor('replacement-model'))
    release.set()
    await slow
    assert state.revisions()[0]['processor']['model_id'] == 'replacement-model'
    assert len(state.revisions(history=True)) == 1


@pytest.mark.asyncio
async def test_abstention_scope_and_invalid_output_do_not_create_opinions(judgments):
    state, clock = judgments
    source(state, 'logistics', 'Please set aside the package until this afternoon.')
    source(state, 'guest', contact_id='contact-b')
    source(state, 'checkpoint', scope='session')
    source(state, 'historical', derive_claims=False)
    abstainer = Processor(decide=lambda _: {'action': 'abstain'})
    await state.process_one(abstainer)
    assert state.revisions() == [] and run_row(state, 'logistics')['disposition'] == 'abstained'
    assert not await state.process_one(abstainer)
    source(state, 'invalid')
    invalid = Processor(decide=lambda p: revise(p) | {'support': ['invented-handle']})
    for _ in range(3):
        await state.process_one(invalid)
        clock.value += 1000
    assert run_row(state, 'invalid')['status'] == 'unavailable'
    assert state.revisions() == []
    assert run_row(state, 'invalid')['validation_code'] == 'invalid_judgment_evidence'


@pytest.mark.asyncio
async def test_existing_worker_indexes_while_reasoning_is_in_flight_and_cancels_it(judgments, monkeypatch):
    state, _ = judgments
    source(state)
    from colony_sidecar.beliefs import source_projection
    from colony_sidecar.turns import media, source_vectors
    entered, indexed_again, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow(_payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def no_work(*_args):
        return False
    class Vectors:
        def __init__(self, *_args):
            pass
        def backfill(self):
            pass
        async def process_one(self):
            if entered.is_set():
                indexed_again.set()
            return False
    monkeypatch.setattr(source_vectors, 'SourceVectors', Vectors)
    monkeypatch.setattr(source_projection.SourceClaimProjection, 'process_one', no_work)
    monkeypatch.setattr(media.SourceMedia, 'process_one', no_work)
    monkeypatch.setattr(media.SourceMedia, 'recover_unowned_files', lambda _self: None)
    worker = asyncio.create_task(source_projection.run_source_claim_worker(state.ledger, lambda: Processor(before_return=slow)))
    await asyncio.wait_for(indexed_again.wait(), 4)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_prior_model_or_prior_source_alone_cannot_justify_an_update(judgments):
    state, clock = judgments
    source(state)
    await state.process_one(Processor())
    clock.value += 86401
    source(state, 'unrelated-basis', 'Local work checkpoints are mentioned in this new conversation.')
    def old_only(payload):
        assert payload['previous_evidence'][0]['text'].startswith('Long local work lost progress')
        return revise(payload) | {'support': [payload['previous_evidence'][0]['handle']]}
    await state.process_one(Processor('second-model', decide=old_only))
    assert len(state.revisions(history=True)) == 1
    assert run_row(state, 'unrelated-basis')['error'] == 'JudgmentValidationError'


@pytest.mark.asyncio
async def test_reasoning_or_truncated_provider_output_is_not_a_judgment(judgments):
    state, _ = judgments
    source(state)
    class Truncated(Processor):
        async def complete(self, **kwargs):
            response = await super().complete(**kwargs)
            response.raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',
                message=SimpleNamespace(content=response.content))])
            return response
    await state.process_one(Truncated())
    assert state.revisions(history=True) == []
    assert run_row(state, 'first')['error'] == 'JudgmentValidationError'
    assert json.loads(run_row(state, 'first')['processor_json'])['model_id'] == 'processor-a'
    assert run_row(state, 'first')['validation_code'] == 'incomplete_final_answer'


@pytest.mark.asyncio
async def test_owner_api_withdraw_reconsider_restart_and_correction_history(source_app, perspective):
    state, _, _ = perspective
    state.judgments.clock = Clock()
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, 'Long local work checkpoints recovered lost progress.', 'original')
        await state.judgments.process_one(Processor())
        original = state.judgments.revisions()[0]
        body = {'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'correction'},
                'original': '', 'correction': 'Withdraw this working view until I ask for reconsideration.',
                'correction_id': 'withdraw-1', 'judgment_id': original['id'], 'judgment_action': 'withdraw'}
        guest = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer guest-key'}, json=body)
        assert guest.status_code == 403
        response = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer owner-key'}, json=body)
        assert response.status_code == 200, response.text
        withdrawn = response.json()['judgment']
        assert state.judgments.revisions() == [] and state.judgments.brief('local work checkpoints') == ''
        again = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer owner-key'}, json=body)
        assert again.json()['judgment'] == withdrawn
        stale = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer owner-key'},
                                  json=body | {'correction_id': 'stale-control'})
        assert stale.status_code == 409 and stale.json()['detail'] == 'judgment_head_changed'
        conflict = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer owner-key'},
                                     json=body | {'correction': 'Changed operation under an old ID'})
        assert conflict.status_code == 409 and conflict.json()['detail'] == 'judgment_correction_id_conflict'
        await tell(client, 'Another local work checkpoint observation is available.', 'fresh')
        await state.judgments.process_one(Processor())
        assert run_row(state.judgments, 'fresh')['disposition'] == 'owner_withdrawn'
        reopened = SelfJudgments(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=state.judgments.clock)
        assert reopened.revisions() == []
        correction_text = 'The measured overhead was large for short work. Reconsider the checkpoint judgment using this correction.'
        await tell(client, correction_text, 'owner-evidence')
        body.update(judgment_id=withdrawn['revision_id'], judgment_action='reconsider', correction_id='reconsider-1',
                    correction='Reconsider using the retained observation.', source_id='owner-evidence')
        response = await client.post('/v1/host/learning/correction', headers={'Authorization': 'Bearer owner-key'}, json=body)
        assert response.status_code == 200, response.text
        correction = response.json()['judgment']
        assert state.judgments.revisions() == []  # no copied owner stance before inference
        model = Processor('reconsidered', decide=lambda p: revise(p, stance='I favor phase checkpoints when expected recovery exceeds their overhead.', contrary=True))
        await state.judgments.process_one(model)
        assert model.requests[0]['owner_correction']['source_id'] == 'owner-evidence'
        assert model.requests[0]['evidence'][0]['text'] == correction_text
        assert model.requests[0]['previous_evidence'][0]['text'].startswith('Long local work')
        current = state.judgments.revisions()[0]
        assert current['supersedes'] == correction['revision_id']
        assert current['processor']['model_id'] == 'reconsidered'
        assert len(state.judgments.revisions(history=True)) == 4
        assert state.preferences() == [] and current['authority_changed'] is False
        inspected = await client.get('/v1/host/self', headers={'Authorization': 'Bearer owner-key'})
        assert inspected.status_code == 200
        assert inspected.json()['perspective']['judgment_processing'][0]['disposition'] == 'revised'
        # Forgetting dependency prose retains value-free owner control history.
        state.ledger.erase_sources(contact_id='contact-a', turn_ids=['original'])
        assert state.judgments.revisions() == []
        history = state.judgments.revisions(history=True)
        assert all(not r['topic'] for r in history)
        assert [r.get('correction_id') for r in history if r.get('correction_id')] == ['reconsider-1', 'withdraw-1']


@pytest.mark.asyncio
async def test_withdraw_fences_inflight_view_and_reconsider_abstention_stays_withdrawn(judgments):
    state, _ = judgments
    source(state)
    await state.process_one(Processor())
    old = state.revisions()[0]
    source(state, 'later')
    async def withdraw(_):
        state.correct(old['id'], action='withdraw', correction_id='stop', reason='Owner withdrew this judgment.')
    await state.process_one(Processor(before_return=withdraw))
    assert state.revisions() == [] and run_row(state, 'later')['disposition'] == 'head_changed'
    control = state.revisions(history=True)[0]
    with pytest.raises(ValueError, match='retained_owner_source_required'):
        state.correct(control['id'], action='reconsider', correction_id='missing', reason='Reconsider', source_id='missing')
    state.correct(control['id'], action='reconsider', correction_id='again', reason='Reconsider the source.', source_id='later')
    await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    assert state.revisions() == [] and state.revisions(history=True)[0]['status'] == 'withdrawn'
    source(state, 'another')
    await state.process_one(Processor())
    assert state.revisions() == [] and run_row(state, 'another')['disposition'] == 'owner_withdrawn'


@pytest.mark.asyncio
async def test_reconsider_cannot_rename_topic_and_validation_code_excludes_provider_text(judgments):
    state, _ = judgments
    source(state)
    await state.process_one(Processor())
    source(state, 'correction')
    state.correct(state.revisions()[0]['id'], action='reconsider', correction_id='revisit', reason='Reconsider.', source_id='correction')
    await state.process_one(Processor(decide=lambda p: revise(p) | {'topic': 'invented new topic'}))
    assert run_row(state, 'correction')['validation_code'] == 'invalid_judgment_reconsideration'
    assert state.revisions() == []
    class Broken(Processor):
        async def complete(self, **kwargs):
            result = await super().complete(**kwargs)
            result.content = 'unparseable private provider content'
            return result
    source(state, 'broken')
    await state.process_one(Broken())
    row = run_row(state, 'broken')
    assert row['validation_code'] == 'invalid_judgment_json'
    assert 'private provider content' not in json.dumps(row)
