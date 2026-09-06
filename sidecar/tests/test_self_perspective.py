"""Owner source → durable correction → ranking; runtime history remains scoped."""
import json
import sqlite3
import time
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
@pytest.mark.parametrize('runtime_outcome', ['completed_wrong_count', 'timeout'])
async def test_actual_runtime_outcomes_do_not_change_priority_or_claim_quality(perspective, source_app, tmp_path, runtime_outcome):
    from colony_sidecar.initiatives.store import InitiativeStore
    from colony_sidecar.reasoning.loop import ReasoningResult
    from colony_sidecar.services.initiative_executor import InitiativeExecutorService
    state, learner, sm = perspective
    store = InitiativeStore(tmp_path / 'initiatives')
    executor = InitiativeExecutorService(store, None, None, self_model=sm)
    executor._find_recent_completion = AsyncMock(return_value=None)
    executor._maybe_distill = AsyncMock()  # Skill updates are a separate loop.
    model = {'model_role': 'planning', 'model_id': 'fixture-model-a', 'model_revision': 'v1'}
    result = (ReasoningResult(status='completed', message={'role': 'assistant', 'content': 'The inventory has 17 entries.'}, model_provenance=model)
              if runtime_outcome == 'completed_wrong_count' else
              ReasoningResult(status='error', error='TimeoutError: timed out', model_provenance=model))
    executor._run_turn_resilient = AsyncMock(return_value=result)
    original = candidates()
    before = [(item.id, item.priority) for item in await phase(sm, original)]
    for i in range(3):
        item = store.create(type='research', description=f'Count all {42+i} neutral inventory entries', dedup_key=f'work-{i}')
        item = store.assign(item.id, 'colony-executor')
        await executor._execute_one(item)
    events = sm.store.events('research')
    assert len(events) == 3 and all(e['source_ref'] and e['event_key'] for e in events)
    assert all(e['evidence']['meaning'].endswith('semantic_success_unverified') for e in events)
    assert all(e['evidence']['model_id'] == 'fixture-model-a' for e in events)
    assert {e['outcome'] for e in events} == ({'success'} if runtime_outcome == 'completed_wrong_count' else {'timeout'})
    assert [(item.id, item.priority) for item in await phase(sm, original)] == before
    assert state.opinions() == []
    # History remains readable, but cannot become a competence claim about a replacement model.
    brief = sm.brief()
    assert 'Recorded runtime outcomes:' in brief
    assert 'do not verify output quality' in brief and "current model's ability" in brief
    assert 'You reliably complete' not in brief and 'You often fail at' not in brief
    reopened = SelfPerspective(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a')
    assert [(item.id, item.priority) for item in reopened.rank(original)] == before
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, 'Deprioritize research initiatives.', 'override')
        ranked = await phase(sm, original)
        assert [i.id for i in ranked] == ['urgent', 'follow-up', 'research-next']
        assert ranked[-1].priority == .64
        trace = state.status()['attention']
        assert trace['authority_changed'] is False
        assert trace['decisions'][0]['basis'] == 'owner_correction'
        assert 'turn:override' in await context(client, 'model-swapped')
        erased = await client.post('/v1/host/memory/sources/forget', headers={'Authorization': 'Bearer owner-key'},
            json={'contact_id': 'contact-a', 'source_ids': ['override']})
        assert erased.status_code == 200
        assert [(item.id, item.priority) for item in await phase(sm, original)] == before
        assert len(sm.store.events('research')) == 3


def test_legacy_opinions_and_evidence_corrections_remain_inspectable_but_inactive(perspective):
    state, _, sm = perspective
    sm.store.record('research', 'success', source='worker', source_ref='work', event_key='attempt',
                    outcome_contract='runtime/v1', evidence={'model_id': 'old-model'})
    event = sm.store.events('research')[0]
    old_basis = json.dumps([{'event_id': event['id'], 'fingerprint': event['fingerprint'], 'model_id': 'old-model'}])
    with sqlite3.connect(state.ledger.db_path) as conn:
        for weight in (.95, 1.2):
            conn.execute('INSERT INTO self_opinion_revisions(domain,weight,basis_json,basis_digest,reason,updated_at,version) VALUES (?,?,?,?,?,?,?)',
                ('research', weight, old_basis, 'old-digest', 'LEGACY_ABILITY_CLAIM', 1, 'operational-perspective-v1'))
        conn.execute('INSERT OR REPLACE INTO self_attention VALUES (1,?,?)',
            (json.dumps({'version':'operational-perspective-v1','ordered_ids':['legacy-ranked'],'decisions':[]}), 1))
        original_rows = conn.execute('SELECT * FROM self_opinion_revisions ORDER BY id').fetchall()
    assert state.status()['attention']['historical_only'] is True
    assert 'legacy-ranked' not in state.brief() and 'LEGACY_ABILITY_CLAIM' not in state.brief()
    original = candidates()
    state.rank(original, competence=sm.store)
    assert next(item for item in state.rank(original) if item.id == 'research-next').priority == .8
    status = state.status()
    assert status['automatic_weighting'] == 'retired'
    assert len(status['opinion_history']) == 2
    assert all(row['status'] == 'legacy_non_governing' and row['governing'] is False for row in status['opinion_history'])
    assert status['opinion_history'][0]['basis'][0]['model_id'] == 'old-model'
    sm.store.apply_reconciliation({'schema':'colony.competence-reconciliation/v1','created_by':'test-reviewer',
        'reason':'Recorded output failed its count check','provenance':{'criterion':'exact inventory count'},
        'event_corrections':[{'event_id':event['id'],'target_fingerprint':event['fingerprint'],'disposition':'invalidate'}]})
    assert sm.store.events('research') == [] and len(sm.store.reconciliation_ledger()) == 1
    assert [(i.id,i.priority) for i in state.rank(original)] == [(i.id,i.priority) for i in sorted(original,key=lambda i:-i.priority)]
    with sqlite3.connect(state.ledger.db_path) as conn:
        assert conn.execute('SELECT * FROM self_opinion_revisions ORDER BY id').fetchall() == original_rows
    assert sm.store.inspect_events('research', 0, time.time()+1)[0]['recorded_outcome'] == 'success'


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
