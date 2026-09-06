"""Forgotten conversation evidence stops influencing affect and engagement.

Real SQLite, source erasure API, ordinary ingress and context assembly; only
model extraction is controlled. Pre-migration aggregate state stays unlinked.
"""
import asyncio
import json
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.tom.affect import AffectStore
from colony_sidecar.tom.engagement import EngagementStore, build_guidance
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.idempotency import SourceErased
from test_tom_source_lineage import runtime, ingest, forget
from test_turn_source_evidence import source_app


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


@pytest.mark.asyncio
async def test_ordinary_ingress_forget_removes_text_and_numeric_influence(relations):
    r = relations
    explicit = r.affect.create_event(contact_id='contact-a', valence=.4, arousal=.3, trigger='independent explicit signal')
    r.engagement.update_from_observation('contact-a', style={'warmth': .9}, topics=['independent topic'])
    before = r.engagement.get_profile('contact-a')
    async with AsyncClient(transport=ASGITransport(app=r.app), base_url='http://test') as client:
        await ingest(client, r)
        linked = [e for e in r.affect.list_events() if e['source_lineage']][0]
        assert linked['source_lineage']['turn_id'] == 'turn-a'
        assert len(linked['source_lineage']['message_hashes']) == 2
        assert linked['session_id'] == 'session-a'
        history = await client.get('/v1/host/affect/history/contact-a')
        assert any(e.get('evidence_basis') == 'canonical_source' and e.get('source_lineage') for e in history.json()['events'])
        assert r.affect.get_state('contact-a')['current_valence'] < .1
        assert r.engagement.get_profile('contact-a')['dims']['warmth']['value'] == .5
        assert 'neutral-source-topic' in await engagement_brief(client)
        result = await forget(client)
        assert result['affect_cleanup'] == result['engagement_cleanup'] == 'complete'
        assert r.affect.get_event(linked['id']) is None
        assert r.affect.get_event(explicit['id'])['source_lineage'] is None
        assert r.affect.get_state('contact-a')['current_valence'] == .4
        after = r.engagement.get_profile('contact-a')
        assert after['dims'] == before['dims'] and after['qual'] == before['qual']
        assert after['observation_count'] == before['observation_count']
        assert 'neutral-source-topic' not in await engagement_brief(client)
        assert r.engagement._conn.execute('SELECT count(*) FROM engagement_observations WHERE source_lineage_json IS NOT NULL').fetchone()[0] == 0
        with pytest.raises(SourceErased):
            r.affect.create_event(contact_id='contact-a', valence=-.8, source_lineage=linked['source_lineage'])
        with pytest.raises(SourceErased):
            r.engagement.update_from_observation('contact-a', topics=['neutral-source-topic'], source_lineage=linked['source_lineage'])


@pytest.mark.asyncio
async def test_forget_while_engagement_model_is_running_drops_late_result(relations, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return {'style': {'warmth': .1}, 'topics': ['neutral-late-topic']}
    monkeypatch.setattr(relations.extractor, 'extract_engagement', blocked)
    async with AsyncClient(transport=ASGITransport(app=relations.app), base_url='http://test') as client:
        await ingest(client, relations, wait=False)
        await asyncio.wait_for(started.wait(), 3)
        assert relations.affect.count_events() == 1
        await forget(client)
        release.set()
        await asyncio.wait_for(asyncio.gather(*relations.tasks), 3)
        assert relations.affect.count_events() == 0
        assert relations.affect.get_state('contact-a')['event_count'] == 0
        assert relations.engagement.get_profile('contact-a')['observation_count'] == 0
        assert await engagement_brief(client) == ''


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
        assert actual['dims'] == expected['dims'] and actual['qual'] == expected['qual']
        assert actual['legacy_unlinked_observations'] == 4 and actual['observation_count'] == 5
        assert 'erased topic' not in build_guidance(actual)
        assert store._conn.execute('SELECT evidence_basis FROM engagement_baselines').fetchone()[0] == 'legacy_unlinked'
        assert store._conn.execute('SELECT count(*) FROM engagement_observations').fetchone()[0] == 1
        ledger.erase_sources(contact_id='person', turn_ids=['b'])
        actual = store.get_profile('person')
        assert actual['dims']['warmth']['value'] == .8 and actual['observation_count'] == 4
        assert actual['qual'] == {'topics': ['legacy topic']}
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
        read = lambda: store.get_profile('person')['observation_count']
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
