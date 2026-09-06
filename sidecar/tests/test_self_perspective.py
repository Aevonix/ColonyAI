"""Source → stable correction / work outcome → judgment → actual ranking/context."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.intelligence.components.preference_learner import PreferenceLearner
from colony_sidecar.self_model.perspective import SelfPerspective
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.turns import TurnIdempotencyLedger
from test_turn_source_evidence import source_app


@pytest.fixture
def perspective(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'contact-a')
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    perspective = SelfPerspective(ledger, owner_id='contact-a')
    learner = PreferenceLearner(db_path=str(tmp_path / 'old-preferences.db'), perspective=perspective)
    sm = SelfModel(CompetenceStore(str(tmp_path / 'competence.db')))
    sm.perspective = perspective
    monkeypatch.setattr(host, '_preference_learner', learner)
    monkeypatch.setattr(host, '_self_model', sm)
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    from test_scoped_api_authority import _principal, _write_keyring
    principals = [_principal(principal=who, secret=who+'-key', viewer=person,
        scopes=['context:read', 'memory:write', 'turns:write'])
        for who, person in [('owner', 'contact-a'), ('guest', 'contact-b')]]
    for principal in principals:
        principal['allow_unscoped_api'] = False
    keys = tmp_path / 'perspective-keys.json'; _write_keyring(keys, principals)
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keys), api_key=None)
    return perspective, learner, sm


async def tell(client, text, turn, *, person='contact-a', occurred='2026-09-05T12:00:00+00:00'):
    response = await client.put('/v2/host/turns/' + turn,
        headers={'Authorization': 'Bearer owner-key' if person == 'contact-a' else 'Bearer guest-key'}, json={
        'identity': {'host_id': 'fixture'},
        'context': {'contact_id': person, 'session_id': 's-' + turn, 'turn_id': turn,
                    'channel_id': 'test:' + person, 'metadata': {'occurred_at': occurred}},
        'user_message': {'role': 'user', 'content': text},
    })
    assert response.status_code in {200, 201}, response.text


async def context(client, session, *, headers=None):
    response = await client.post('/v1/host/context/assemble', headers=headers or {'Authorization': 'Bearer owner-key'}, json={
        'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'contact-a', 'session_id': session},
        'incoming_message': {'role': 'user', 'content': 'What is your working judgment?'},
    })
    assert response.status_code == 200, response.text
    return '\n'.join(section['body'] for section in response.json()['sections']
                     if section['id'] in {'colony-owner-preferences', 'colony-self-perspective'})


@pytest.mark.asyncio
async def test_ordinary_correction_survives_reopen_and_cannot_be_overwritten_or_resurrected(source_app, perspective, tmp_path, monkeypatch):
    state, learner, sm = perspective
    await learner.learn_directive('be concise')  # a legacy value must not reappear after erase
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, 'Be concise.', 'early', occurred='2026-09-01T00:00:00+00:00')
        await tell(client, 'Actually be detailed and thorough.', 'correction', occurred='2026-09-03T00:00:00+00:00')
        await tell(client, 'Be brief.', 'late-old', occurred='2026-09-02T00:00:00+00:00')
        await tell(client, 'Be concise.', 'guest', person='contact-b')
        await tell(client, 'Alice said "be concise".', 'quote')
        assert len(state.preferences(history=True)) == 3
        assert state.preferences()[0]['value'] == 'long'
        assert state.preferences()[0]['supersedes'] is not None
        for _ in range(10):
            await learner.learn_from_behavior('clicked_short_response')
        assert await learner.get_preference('communication_style', 'length') == 'long'
        first = await context(client, 'new-model-session')
        assert 'thorough' in first and 'turn:correction' in first
        reopened = SelfPerspective(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a')
        replacement = PreferenceLearner(db_path=str(tmp_path / 'old-preferences.db'), perspective=reopened)
        monkeypatch.setattr(host, '_preference_learner', replacement)
        assert await context(client, 'another-model-session') == first
        erased = await client.post('/v1/host/memory/sources/forget', headers={'Authorization': 'Bearer owner-key'}, json={'contact_id': 'contact-a', 'source_ids': ['correction']})
        assert erased.status_code == 200
        assert reopened.preferences() == []  # older history is not an active correction
        assert replacement.build_brief() == ''
        await tell(client, 'Be concise.', 'even-older', occurred='2026-08-31T00:00:00+00:00')
        assert reopened.preferences() == []
        with sqlite3.connect(state.ledger.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM self_preference_events WHERE source_turn_id=?', ('correction',)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_strict_owner_scope_can_read_corrections_but_guest_cannot(source_app, perspective, tmp_path):
    state, learner, _ = perspective
    state.ledger.record_source('owner-source', contact_id='contact-a', session_id='s', messages=[{'role': 'user', 'content': 'Use bullet points please.'}])
    learner.learn_source('owner-source')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        text = await context(client, 'strict-owner', headers={'Authorization': 'Bearer owner-key'})
        assert 'bullet' in text and 'turn:owner-source' in text
        for route in ('/v1/host/self', '/v1/host/preferences'):
            assert (await client.get(route, headers={'Authorization': 'Bearer owner-key'})).status_code == 200
            assert (await client.get(route, headers={'Authorization': 'Bearer guest-key'})).status_code == 403
        assert (await client.post('/v1/host/preferences/learn', headers={'Authorization': 'Bearer guest-key'}, json={'source_id': 'owner-source'})).status_code == 403
        assert (await client.post('/v1/host/preferences/learn', headers={'Authorization': 'Bearer owner-key'}, json={'text': 'be concise'})).status_code == 422


def candidates():
    return [SimpleNamespace(id='research-next', type='research', priority=0.8),
            SimpleNamespace(id='follow-up', type='follow_up', priority=0.77),
            SimpleNamespace(id='urgent', type='commitment', priority=0.95)]


async def phase(sm, items):
    engine = SimpleNamespace(clear_context=lambda: None, generate=AsyncMock(return_value=items), _context={})
    loop = AutonomyLoop(SimpleNamespace(self_model=sm, initiative_engine=engine))
    for name in ('_feed_pending_tasks', '_feed_neglected_contacts', '_feed_commitment_reminders', '_feed_introduction_candidates'):
        setattr(loop, name, AsyncMock())
    loop._in_quiet_hours = lambda: False
    await loop._phase_initiative()
    return loop._pending_initiatives


@pytest.mark.asyncio
async def test_actual_executor_outcomes_change_initiative_order_and_context(perspective, source_app, tmp_path):
    from colony_sidecar.initiatives.store import InitiativeStore
    from colony_sidecar.reasoning.loop import ReasoningResult
    from colony_sidecar.services.initiative_executor import InitiativeExecutorService
    state, learner, sm = perspective
    store = InitiativeStore(tmp_path / 'initiatives')
    executor = InitiativeExecutorService(store, None, None, self_model=sm)
    executor._find_recent_completion = AsyncMock(return_value=None)
    executor._run_turn_resilient = AsyncMock(return_value=ReasoningResult(status='error', error='timed out',
        model_provenance={'model_role': 'large', 'model_id': 'fixture-model-a', 'model_revision': 'unknown'}))
    original = candidates()
    assert [i.id for i in await phase(sm, original)] == ['urgent', 'research-next', 'follow-up']
    for i in range(3):
        item = store.create(type='research', description=f'Neutral research task {i}', dedup_key=f'work-{i}')
        item = store.assign(item.id, 'colony-executor')
        await executor._execute_one(item)
        if i < 2:
            state.refresh(sm.store)
            assert state.opinions() == []
    events = sm.store.events('research')
    assert len(events) == 3 and all(e['source_ref'] and e['event_key'] for e in events)
    ranked = await phase(sm, original)
    assert [i.id for i in ranked] == ['urgent', 'follow-up', 'research-next']
    assert ranked[-1].priority == 0.76
    assert original[0].priority == 0.8
    assert (await phase(sm, original))[-1].priority == 0.76  # cached candidates do not compound
    opinion = state.opinions()[0]
    assert opinion['weight'] == 0.95 and len(opinion['basis']) == 3
    assert all(item['model_id'] == 'fixture-model-a' for item in opinion['basis'])
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        text = await context(client, 'different-session')
        assert '3/3' in text and 'revision' in text and 'research-next' in text
        await tell(client, 'Prefer research initiatives.', 'override')
        after = await phase(sm, original)
        assert [i.id for i in after] == ['urgent', 'research-next', 'follow-up']
        assert after[1].priority == 0.89  # optional preference cannot become urgent
        trace = state.status()['attention']
        assert trace['authority_changed'] is False
        assert trace['decisions'][0]['basis'] == 'owner_correction'
        before_count = len(sm.store.events('research'))
        state.refresh(sm.store)
        assert len(sm.store.events('research')) == before_count
        reopened = SelfPerspective(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a')
        assert reopened.rank(original)[1].priority == 0.89
        assert 'turn:override' in await context(client, 'model-swapped')


def test_distinct_work_required_and_reconciliation_removes_stale_judgment(perspective):
    state, _, sm = perspective
    for i in range(12):
        sm.store.record('research', 'timeout', source='worker', source_ref='same-work', event_key=f'attempt-{i}', outcome_contract='fixture/v1')
    state.refresh(sm.store)
    assert state.opinions() == []
    for i in range(2):
        sm.store.record('research', 'timeout', source='worker', source_ref=f'other-{i}', event_key=f'other-{i}', outcome_contract='fixture/v1')
    state.refresh(sm.store)
    assert state.opinions()[0]['weight'] == 0.95
    # Exercise the effective-evidence contract with evidence now unavailable.
    state.refresh(SimpleNamespace(events=lambda *args, **kwargs: []))
    assert state.opinions()[0]['weight'] == 1.0
    assert 'Insufficient' in state.opinions()[0]['reason']


@pytest.mark.parametrize('text', ['Stop using bullet points.', 'Be concise and detailed.',
    'Use prose rather than bullets.', 'Alice says be formal.', 'Should you be concise?',
    'Use the code example to explain this bug.'])
def test_ambiguous_or_reported_directives_remain_evidence(perspective, text):
    state, learner, _ = perspective
    state.ledger.record_source('uncertain', contact_id='contact-a', session_id='s', messages=[{'role': 'user', 'content': text}])
    assert learner.learn_source('uncertain') == []
    assert state.preferences() == []


def test_negative_emoji_directive_keeps_its_polarity(perspective):
    state, learner, _ = perspective
    state.ledger.record_source('emoji', contact_id='contact-a', session_id='s', messages=[{'role': 'user', 'content': "Don't use emoji."}])
    assert learner.learn_source('emoji') == [('communication_style.emoji', 'off')]
