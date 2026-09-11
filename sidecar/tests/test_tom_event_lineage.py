"""Forgotten conversation evidence stops influencing affect and engagement.

Real SQLite, source erasure API, ordinary ingress and context assembly; only
model extraction is controlled. Pre-migration aggregate state stays unlinked.
"""
import asyncio
import json
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.api.routers import host
from apsimo.contacts.comms import CommsLog
from apsimo.contacts.config import ContactsConfig
from apsimo.contacts.store import SQLiteContactStore
from apsimo.intelligence.relationships.profiler import RelationshipProfiler
from apsimo.tom.affect import AffectStore
from apsimo.tom.engagement import EngagementStore, build_guidance
from apsimo.tom.facts import SharedFactsStore
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.idempotency import SourceErased
from test_tom_source_lineage import runtime, ingest, forget
from test_turn_source_evidence import source_app
from test_turn_source_evidence import envelope, recalled


@pytest.fixture
def relations(runtime, monkeypatch, tmp_path):
    affect = AffectStore(tmp_path / 'affect.db', source_ledger=runtime.ledger)
    engagement = EngagementStore(tmp_path / 'engagement.db', source_ledger=runtime.ledger)
    monkeypatch.setattr(host, '_affect_store', affect)
    monkeypatch.setattr(host, '_engagement_store', engagement)

    async def extract_affect(text, contact_id, **kwargs):
        return {'contact_id': contact_id, 'valence': -.8, 'arousal': .9, 'trigger': 'neutral-source-trigger'}

    async def extract_engagement(*args, **kwargs):
        return {'style': {'warmth': .1}, 'topics': ['neutral-source-topic']}

    monkeypatch.setattr(runtime.extractor, 'extract_affect', extract_affect)
    monkeypatch.setattr(runtime.extractor, 'extract_engagement', extract_engagement)
    runtime.affect, runtime.engagement = affect, engagement
    yield runtime
    affect.close()
    engagement._conn.close()


async def engagement_brief(client):
    response = await client.post('/v1/host/context/assemble', json={
        'identity': {'host_id': 'test-host'},
        'context': {'contact_id': 'contact-a', 'session_id': 'another-session'},
        'incoming_message': {'role': 'user', 'content': 'neutral query'},
    })
    assert response.status_code == 200
    return '\n'.join(s['body'] for s in response.json()['sections'] if s['id'] == 'colony-engagement')


@pytest.fixture
async def approach(relations, monkeypatch, tmp_path):
    from apsimo.contacts import store as contact_module
    from apsimo import identity
    monkeypatch.setattr(contact_module, '_gen_id', lambda prefix: 'contact-a')
    monkeypatch.setattr(identity, 'get_owner_contact_id', lambda: 'owner')
    contacts = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / 'contacts.db')))
    await contacts.connect()
    await contacts.create(display_name='Neutral contact', trust_tier='regular')
    for _ in range(6):
        await contacts.record_interaction('contact-a')
    log = CommsLog(db_path=str(tmp_path / 'comms.db'))
    log.log('contact-a', channel='test:thread-a', direction='in')
    profiler = RelationshipProfiler(contacts_store=contacts, comms_log=log,
        affect_store=relations.affect, engagement_store=relations.engagement,
        db_path=str(tmp_path / 'relationships.db'))
    monkeypatch.setattr(host, '_contacts_store', contacts)
    monkeypatch.setattr(host, '_relationship_profiler', profiler)
    relations.profiler, relations.contacts = profiler, contacts
    yield relations
    relations.profiler._conn.close()
    log._conn.close()
    await contacts.close()


async def relationship_context(client):
    response = await client.post('/v1/host/context/assemble', json={
        'identity': {'host_id': 'test-host'},
        'context': {'contact_id': 'contact-a', 'session_id': 'later-session'},
        'incoming_message': {'role': 'user', 'content': 'neutral query'},
    })
    assert response.status_code == 200, response.text
    return {s['id']: s['body'] for s in response.json()['sections']}


def retained_legacy_affect(runtime, turn='turn-a'):
    """Retained pre-migration evidence remains erasable, without new inference."""
    lineage, _ = runtime.facts.source_input(turn, 'contact-a')
    return runtime.affect.create_event(contact_id='contact-a', valence=-.8,
        arousal=.9, source='inferred', trigger='neutral-source-trigger',
        session_id='session-a', source_lineage=lineage)


@pytest.mark.asyncio
async def test_cached_approach_forget_reopen_changes_next_ordinary_context(approach, monkeypatch, tmp_path):
    r = approach
    r.engagement.update_from_observation('contact-a', style={'warmth': .9}, topics=['independent topic'])
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        await ingest(client, r)
        retained_legacy_affect(r)
        brief = await r.profiler.profile('contact-a')
        before = await relationship_context(client)
        assert 'neutral-source-topic' not in before['colony-approach']
        assert 'mood is negative' not in before['colony-approach']
        assert 'Recent mood:' not in before['colony-approach']
        assert 'mood is negative' in brief.render()  # Explicit inspection remains.
        cached = json.loads(r.profiler._conn.execute('SELECT brief_json FROM relationship_briefs').fetchone()[0])
        assert not {'affect_valence', 'affect_trend', 'psyche_guidance', 'psyche_motivators'} & cached.keys()
        assert 'recent mood is negative; lead carefully' not in cached['cautions']
        # An actual old-format cache carries copied advice. Its reopened read
        # must obey erasure even with no new interactions or refresh phase.
        with r.profiler._conn:
            r.profiler._conn.execute('UPDATE relationship_briefs SET brief_json=?', (json.dumps(brief.to_dict()),))
        count = (await r.contacts.get('contact-a')).interaction_count
        result = await forget(client)
        assert result['affect_cleanup'] == result['engagement_cleanup'] == 'complete'
        r.profiler._conn.close()
        r.profiler = RelationshipProfiler(contacts_store=r.contacts,
            affect_store=r.affect, engagement_store=r.engagement,
            db_path=str(tmp_path / 'relationships.db'))
        monkeypatch.setattr(host, '_relationship_profiler', r.profiler)
        after = await relationship_context(client)
        assert 'neutral-source-topic' not in '\n'.join(after.values())
        assert 'mood is negative' not in after['colony-approach']
        assert 'independent topic' not in after['colony-approach']
        assert 'independent topic' in r.engagement.get_profile('contact-a')['legacy_profile']['qual']['topics']
        assert 'mostly via test:thread-a' in after['colony-approach']
        assert (await r.contacts.get('contact-a')).interaction_count == count
        assert (await r.profiler.refresh_due())['profiled'] == 0


@pytest.mark.asyncio
async def test_ordinary_ingress_no_longer_produces_legacy_numeric_observations(approach, monkeypatch):
    r = approach
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        await ingest(client, r)
        await r.profiler.profile('contact-a')
        assert 'later-source-topic' not in (await relationship_context(client))['colony-approach']
        async def changed(*args, **kwargs):
            return {'topics': ['later-source-topic']}
        monkeypatch.setattr(r.extractor, 'extract_engagement', changed)
        await ingest(client, r, turn_id='turn-b', session='session-b')
        assert (await r.profiler.refresh_due())['profiled'] == 0
        assert 'later-source-topic' not in (await relationship_context(client))['colony-approach']
        assert 'later-source-topic' not in r.engagement.get_profile('contact-a')['legacy_profile']['qual'].get('topics', [])
        assert r.engagement._conn.execute('SELECT count(*) FROM engagement_observations WHERE source_lineage_json IS NOT NULL').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['missing', 'failed_read'])
async def test_old_cached_advice_is_omitted_when_current_store_unavailable(approach, unavailable, monkeypatch):
    r = approach
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        await ingest(client, r)
        brief = await r.profiler.profile('contact-a')
        brief.cautions.append('no contact in 40 days')
        with r.profiler._conn:
            r.profiler._conn.execute('UPDATE relationship_briefs SET brief_json=?', (json.dumps(brief.to_dict()),))
        if unavailable == 'missing':
            r.profiler._affect = r.profiler._engagement = None
        else:
            def failed(*args, **kwargs):
                raise OSError('controlled source projection unavailable')
            monkeypatch.setattr(r.affect, 'get_state', failed)
            monkeypatch.setattr(r.engagement, 'get_profile', failed)
        after = (await relationship_context(client))['colony-approach']
        assert 'neutral-source-topic' not in after and 'mood is negative' not in after
        assert 'Recent mood:' not in after
        assert 'mostly via test:thread-a' in after and 'no contact in 40 days' in after


@pytest.mark.asyncio
async def test_retained_legacy_affect_forget_removes_text_and_numeric_influence(relations):
    r = relations
    explicit = r.affect.create_event(contact_id='contact-a', valence=.4, arousal=.3, trigger='independent explicit signal')
    r.engagement.update_from_observation('contact-a', style={'warmth': .9}, topics=['independent topic'])
    before = r.engagement.get_profile('contact-a')
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        await ingest(client, r)
        retained_legacy_affect(r)
        linked = [e for e in r.affect.list_events() if e['source_lineage']][0]
        assert linked['source_lineage']['turn_id'] == 'turn-a'
        assert len(linked['source_lineage']['message_hashes']) == 2
        assert linked['session_id'] == 'session-a'
        history = await client.get('/v1/host/affect/history/contact-a')
        assert any(e.get('evidence_basis') == 'canonical_source' and e.get('source_lineage') for e in history.json()['events'])
        assert r.affect.get_state('contact-a')['current_valence'] < .1
        assert r.engagement.get_profile('contact-a')['legacy_profile']['dims']['warmth']['v'] == .9
        assert r.engagement.get_profile('contact-a')['dims'] == {}
        assert 'neutral-source-topic' not in await engagement_brief(client)
        result = await forget(client)
        assert result['affect_cleanup'] == result['engagement_cleanup'] == 'complete'
        assert r.affect.get_event(linked['id']) is None
        assert r.affect.get_event(explicit['id'])['source_lineage'] is None
        assert r.affect.get_state('contact-a')['current_valence'] == .4
        after = r.engagement.get_profile('contact-a')
        assert after['legacy_profile'] == before['legacy_profile']
        assert after['observation_count'] == before['observation_count']
        assert 'neutral-source-topic' not in await engagement_brief(client)
        assert r.engagement._conn.execute('SELECT count(*) FROM engagement_observations WHERE source_lineage_json IS NOT NULL').fetchone()[0] == 0
        with pytest.raises(SourceErased):
            r.affect.create_event(contact_id='contact-a', valence=-.8, source_lineage=linked['source_lineage'])
        with pytest.raises(SourceErased):
            r.engagement.update_from_observation('contact-a', topics=['neutral-source-topic'], source_lineage=linked['source_lineage'])


@pytest.mark.asyncio
async def test_ingress_does_not_run_retired_engagement_extractor(relations, monkeypatch):
    async def retired(*args, **kwargs):
        raise AssertionError('Numeric engagement extraction is retired')
    monkeypatch.setattr(relations.extractor, 'extract_engagement', retired)
    async with AsyncClient(transport=ASGITransport(app=relations.app), base_url='http://test') as client:
        await ingest(client, relations)
        assert relations.affect.count_events() == 0
        await forget(client)
        assert relations.affect.count_events() == 0
        assert relations.engagement.get_profile('contact-a')['observation_count'] == 0
        assert await engagement_brief(client) == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('judgments_enabled', [False, True])
async def test_badge_turn_keeps_source_learning_without_mood_inference_or_injection(approach, monkeypatch, judgments_enabled):
    r = approach
    if judgments_enabled:
        monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    else:
        monkeypatch.delenv('COLONY_SELF_JUDGMENTS_ENABLED', raising=False)
    monkeypatch.setattr('apsimo.identity.get_owner_contact_id', lambda: 'contact-a')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Ordinary badge recall must not infer a mood')
    monkeypatch.setattr(r.extractor, 'extract_affect', forbidden)
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        body = envelope('badge-turn')
        body['user_message']['content'] = 'Please remember that my orchard badge is cobalt-716.'
        response = await client.put('/v2/host/turns/badge-turn', json=body)
        assert response.status_code == 201 and response.json()['source_recorded']
        assert r.tasks == [] and r.affect.count_events() == 0
        with r.ledger._connect() as conn:
            for table in ('source_claim_jobs', 'appraisal_runs'):
                row = conn.execute('SELECT status FROM '+table+' WHERE turn_id=?', ('badge-turn',)).fetchone()
                assert row and row['status'] == 'pending'
            judgment = conn.execute('SELECT status FROM self_judgment_runs WHERE turn_id=?', ('badge-turn',)).fetchone()
            if judgments_enabled:
                assert judgment and judgment['status'] == 'pending'
            else:
                assert judgment is None
        assert 'cobalt-716' in await recalled(client, session='new-session', query='orchard badge')
        # Historical/explicit mood data still exists. Neither ordinary context
        # surface may promote that numeric interpretation into current guidance.
        explicit = r.affect.create_event(contact_id='contact-a', valence=-.9, arousal=.9,
            source='inferred', trigger='old independent estimate')
        common = {'identity': {'host_id':'test-host'},
            'context': {'contact_id':'contact-a','session_id':'new-session'}}
        for path, extra in [
            ('assemble', {'incoming_message': {'role':'user','content':'orchard badge'}}),
            ('enriched', {'message':'orchard badge','features':{'affect':True}}),
        ]:
            response = await client.post('/v1/host/context/'+path, json=common | extra)
            assert response.status_code == 200, response.text
            assert 'colony-affect' not in {section['id'] for section in response.json()['sections']}
            assert 'valence' not in '\n'.join(section['body'] for section in response.json()['sections'])
        history = await client.get('/v1/host/affect/history/contact-a')
        assert history.status_code == 200
        assert any(event['id'] == explicit['id'] for event in history.json()['events'])
        assert r.affect.count_events() == 1


def source(ledger, facts, turn):
    ledger.record_source(turn, contact_id='person', session_id=turn, messages=[{'role': 'user', 'content': 'Neutral '+turn}], derive_claims=False)
    return facts.source_input(turn, 'person')[0]


def old_profile(path):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE engagement_profiles (contact_id TEXT PRIMARY KEY,dims_json TEXT,qual_json TEXT,observation_count INTEGER,updated_at TEXT)')
        conn.execute('INSERT INTO engagement_profiles VALUES (?,?,?,?,?)', ('person', json.dumps({'warmth': {'v': .8, 'n': 4}}), json.dumps({'topics': ['legacy topic']}), 4, '2026-01-01T00:00:00+00:00'))


def test_legacy_baseline_survives_and_only_remaining_observations_recompute(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    facts = SharedFactsStore(tmp_path / 'facts.db', source_ledger=ledger)
    a, b = source(ledger, facts, 'a'), source(ledger, facts, 'b')
    path, reference_path = tmp_path / 'engagement.db', tmp_path / 'reference.db'
    old_profile(path); old_profile(reference_path)
    store = EngagementStore(path, source_ledger=ledger)
    reference = EngagementStore(reference_path, source_ledger=ledger)
    try:
        store.update_from_observation('person', style={'warmth': 0}, topics=['erased topic'], source_lineage=a)
        store.update_from_observation('person', style={'warmth': .2}, topics=['surviving topic'], source_lineage=b)
        reference.update_from_observation('person', style={'warmth': .2}, topics=['surviving topic'], source_lineage=b)
        ledger.erase_sources(contact_id='person', turn_ids=['a'])
        # No API cleanup: reopening and reading must reconcile derived state.
        store._conn.close(); store = EngagementStore(path, source_ledger=ledger)
        actual, expected = store.get_profile('person'), reference.get_profile('person')
        assert actual['legacy_profile'] == expected['legacy_profile']
        assert actual['dims'] == {} and actual['qual'] == {}
        assert actual['legacy_unlinked_observations'] == 4 and actual['legacy_profile']['observation_count'] == 5 and actual['observation_count'] == 0
        assert 'erased topic' not in build_guidance(actual)
        assert store._conn.execute('SELECT evidence_basis FROM engagement_baselines').fetchone()[0] == 'legacy_unlinked'
        assert store._conn.execute('SELECT count(*) FROM engagement_observations').fetchone()[0] == 1
        ledger.erase_sources(contact_id='person', turn_ids=['b'])
        actual = store.get_profile('person')
        assert actual['legacy_profile']['dims']['warmth']['v'] == .8 and actual['legacy_profile']['observation_count'] == 4
        assert actual['legacy_profile']['qual'] == {'topics': ['legacy topic']}
        assert build_guidance(actual) == ''
    finally:
        store._conn.close(); reference._conn.close(); facts.close()


@pytest.mark.parametrize('kind', ['affect', 'engagement'])
def test_cleanup_and_recomputation_are_atomic_and_read_retries(tmp_path, monkeypatch, kind):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    facts = SharedFactsStore(tmp_path / 'facts.db', source_ledger=ledger)
    lineage = source(ledger, facts, 'a')
    if kind == 'affect':
        store = AffectStore(tmp_path / 'affect.db', source_ledger=ledger)
        store.create_event(contact_id='person', valence=-.9, source_lineage=lineage)
        method, table = '_recompute_state', 'affect_events'
        read = lambda: store.get_state('person')['event_count']
    else:
        store = EngagementStore(tmp_path / 'engagement.db', source_ledger=ledger)
        store.update_from_observation('person', style={'warmth': .1}, topics=['neutral marker'], source_lineage=lineage)
        method, table = '_recompute_profile', 'engagement_observations'
        read = lambda: store.get_profile('person')['legacy_profile']['observation_count']
    original = getattr(store, method)
    def fail(*args, **kwargs):
        raise OSError('controlled interrupted projection')
    try:
        ledger.erase_sources(contact_id='person', turn_ids=['a'])
        monkeypatch.setattr(store, method, fail)
        with pytest.raises(OSError):
            store.purge_erased_sources()
        assert store._conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 1
        with pytest.raises(OSError):
            read()  # Never return the stale derived state after failed cleanup.
        monkeypatch.setattr(store, method, original)
        assert read() == 0
        assert store._conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
    finally:
        store._conn.close(); facts.close()


def test_batched_reads_check_all_sources_with_one_canonical_connection(tmp_path, monkeypatch):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    facts = SharedFactsStore(tmp_path / 'facts.db', source_ledger=ledger)
    affect = AffectStore(tmp_path / 'affect.db', source_ledger=ledger)
    events = []
    for index in range(405):
        turn = f'event-{index}'
        lineage = source(ledger, facts, turn)
        if index == 201:
            lineage['message_hashes'] = ['incorrect-old-writer-hash']
        events.append((turn, 'person', .2, .3, 'inferred', 'neutral trigger', '2026-09-01T00:00:00+00:00', turn, json.dumps(lineage)))
    with affect._conn:
        affect._conn.executemany('INSERT INTO affect_events(id,contact_id,valence,arousal,source,trigger,timestamp,session_id,source_lineage_json) VALUES(?,?,?,?,?,?,?,?,?)', events)
        affect._recompute_state('person', commit=False)
    ledger.erase_sources(contact_id='person', turn_ids=['event-0', 'event-404'])
    opened, connect = [], ledger._connect
    def tracked():
        opened.append(True)
        return connect()
    monkeypatch.setattr(ledger, '_connect', tracked)
    try:
        state = affect.get_state('person')
        assert len(opened) == 1
        assert state['event_count'] == 402
        assert affect._conn.execute("SELECT count(*) FROM affect_events WHERE id IN ('event-0','event-201','event-404')").fetchone()[0] == 0
    finally:
        affect.close(); facts.close()
