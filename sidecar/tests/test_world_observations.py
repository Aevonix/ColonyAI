"""Qualified operational state through real SQLite storage and public world APIs."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json

import pytest

from colony_sidecar.world_model.config import WorldModelConfig
from colony_sidecar.world_model.entities import BaseEntity
from colony_sidecar.world_model.store import WorldModelStore
from colony_sidecar.world_model.observations import compact_situation
from colony_sidecar.self_model.situation import SituationFactV1, SituationSnapshotV1

SCOPE = dict(subject_person_id='cid-owner', viewer_scope='owner', shareability='owner_private')
BASE = dict(entity_id='we-service', property_key='model', kind='observed', producer='service:router', **SCOPE)


@pytest.fixture
async def store(tmp_path):
    value = WorldModelStore(WorldModelConfig(backend='sqlite', sqlite_path=str(tmp_path / 'world.db')))
    await value.connect()
    await value.upsert_entity(BaseEntity(id='we-service', name='Local inference', entity_type='product',
                                        properties={'model': 'unqualified-legacy', '_conf_model': .99}))
    yield value
    await value.close()


async def write(store, oid, value, observed='2026-01-01T01:00:00Z', fresh='2026-01-01T04:00:00Z', **kwargs):
    return await store.record_property_observation(**(BASE | dict(observation_id=oid, value=value,
        observed_at=observed, fresh_until=fresh, evidence_refs=[f'receipt:{oid}']) | kwargs))


async def state(store, at='2026-01-01T03:00:00Z', **kwargs):
    return await store.property_state('we-service', 'model', as_of=at, **(SCOPE | kwargs))


async def test_current_historical_stale_and_delayed_arrival_are_distinct(store):
    assert (await state(store))['state'] == 'unknown'  # high legacy confidence is not telemetry
    await write(store, 'new', 'model-b', observed='2026-01-01T02:00:00Z')
    await write(store, 'old', 'model-a', observed='2026-01-01T01:00:00Z')
    now = await state(store)
    assert now['state'] == 'current' and now['value'] == 'model-b'
    assert now['evidence_refs'] == ['receipt:new'] and now['kind'] == 'observed'
    assert (await state(store, '2026-01-01T01:30:00Z'))['value'] == 'model-a'
    expired = await state(store, '2026-01-01T04:00:00Z')
    assert expired['state'] == 'stale' and expired['value'] is None
    assert expired['observations'][0]['value'] == 'model-b'
    await write(store, 'recovery', 'model-c', observed='2026-01-01T04:30:00Z', fresh='2026-01-01T05:00:00Z')
    assert (await state(store, '2026-01-01T04:45:00Z'))['value'] == 'model-c'


async def test_conflicting_reports_are_attributed_and_observed_fact_does_not_hide_disagreement(store):
    await write(store, 'report-a', 'model-a', kind='reported', producer='contact:one')
    await write(store, 'report-b', 'model-b', kind='reported', producer='contact:two')
    conflict = await state(store)
    assert conflict['state'] == 'conflicted' and conflict['value'] is None
    assert {r['producer'] for r in conflict['observations']} == {'contact:one', 'contact:two'}
    await write(store, 'probe', 'model-b')
    observed = await state(store)
    assert observed['state'] == 'current' and observed['kind'] == 'observed'
    assert observed['value'] == 'model-b' and observed['has_disagreement']
    assert observed['authority_granted'] is False
    # Two equally timed observations from one producer also remain a conflict.
    await write(store, 'probe-conflict', 'model-c')
    assert (await state(store))['state'] == 'conflicted'


async def test_repetition_cannot_renew_one_receipts_freshness_or_raise_certainty(store):
    initial = await write(store, 'one', 'model-a', fresh='2026-01-01T02:00:00Z')
    assert await write(store, 'one', 'model-a', fresh='2026-01-01T02:00:00Z') == initial
    for index in range(8):
        await write(store, f'copy-{index}', 'model-a', observed='2026-01-01T02:30:00Z',
                    evidence_refs=['receipt:one'])
    result = await state(store)
    assert result['state'] == 'stale' and result['value'] is None
    assert len(result['observations']) == 1 and 'confidence' not in result['observations'][0]
    with pytest.raises(ValueError, match='id_conflict'):
        await write(store, 'one', 'model-other')


async def test_exact_scope_and_erasure_preserve_invalidated_head_and_raw_history(store):
    await write(store, 'first', 'model-a')
    await write(store, 'second', 'model-private', observed='2026-01-01T02:00:00Z',
                evidence_refs=['source:message-2', 'media:image-2'])
    await store.add_observation('we-service', None, 'Unstructured historical note', 'legacy')
    assert (await state(store, viewer_scope='public', shareability='public'))['state'] == 'unknown'
    assert await store.erase_property_evidence(['source:message-2'], subject_person_id='cid-unrelated') == []
    assert await store.erase_property_evidence(['source:message-2'], subject_person_id='cid-owner') == ['second']
    result = await state(store)
    assert result['state'] == 'unknown' and result['value'] is None
    assert result['observations'][0]['invalidated'] is True
    assert result['observations'][0]['value'] is None
    async with store._backend._db.execute("SELECT observation FROM wm_observations WHERE id='second'") as cur:
        assert 'model-private' not in (await cur.fetchone())['observation']
    with pytest.raises(ValueError, match='id_conflict'):
        await write(store, 'second', 'model-private', observed='2026-01-01T02:00:00Z',
                    evidence_refs=['source:message-2', 'media:image-2'])


async def test_a_long_telemetry_history_still_produces_one_current_fact(store):
    from colony_sidecar.world_model.observations import observation, canonical, SOURCE_PREFIX
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(2100):
        when = base + timedelta(seconds=index)
        item = observation(**BASE, observation_id=f'tick-{index}', value=f'model-{index}',
            observed_at=when.isoformat(), fresh_until=(when + timedelta(hours=1)).isoformat(),
            evidence_refs=[f'receipt:tick-{index}'])
        rows.append((item['observation_id'], item['entity_id'], canonical(item), SOURCE_PREFIX + 'model'))
    await store._backend._db.executemany('INSERT INTO wm_observations(id,entity_id,observation,source) VALUES (?,?,?,?)', rows)
    await store._backend._db.commit()
    result = await state(store, (base + timedelta(seconds=2101)).isoformat())
    assert result['state'] == 'current' and result['value'] == 'model-2099'
    assert not result['coverage_limited'] and len(result['observations']) == 1


def test_compact_situation_never_presents_stale_hardware_as_current():
    fact = SituationFactV1('probe', 'service', 'service:inference', 'healthy', True, 1000, 1120, 'fresh',
                          ('receipt:probe',), 'cid-owner', 'owner', 'owner_private')
    snapshot = SituationSnapshotV1('snapshot', 'digest', 'cid-owner', 'owner', 'owner_private', 1100,
                                  (fact,), (), (), ('receipt:probe',))
    assert compact_situation(snapshot)['facts'][0]['state'] == 'healthy'
    stale = compact_situation(replace(snapshot, facts=(replace(fact, freshness='stale'),), as_of=1200))
    assert stale['facts'] == [] and stale['stale'][0]['state'] == 'unknown'
    assert stale['stale'][0]['last_observed_state'] == 'healthy'
    assert compact_situation(None)['state'] == 'unknown'


async def test_existing_batch_job_writes_only_quoted_canonical_reports_and_rechecks_identity(store, tmp_path, monkeypatch):
    from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
    from colony_sidecar.turns.source_attribution import correct
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    monkeypatch.setenv('COLONY_WORLD_LLM_EXTRACT', 'live')
    monkeypatch.setenv('COLONY_CAUSAL_EXTRACT', 'off')
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    quote = 'Nimbus Router runs model-blue.'
    ledger.record_source('source-real', contact_id='cid-owner', session_id='text',
        messages=[{'role': 'user', 'content': quote}, {'role': 'assistant', 'content': 'Nimbus Router runs model-red.'}])
    ledger.record_source('source-import', contact_id='cid-import', session_id='import', derive_claims=False,
        messages=[{'role': 'user', 'content': 'Imported material.'}])
    seen = []
    async def extract(texts):
        seen.extend(texts)
        return {'entities': [{'name': 'Nimbus Router', 'type': 'product', 'confidence': .8}],
            'observations': [{'entity': 'Nimbus Router', 'property': 'model', 'value': 'model-blue', 'evidence': quote, 'temporal_status': 'current'},
                {'entity': 'Nimbus Router', 'property': 'model', 'value': 'invented', 'evidence': quote}]}
    worker = WorldLLMExtractor(store, source_ledger=ledger)
    worker._llm_batch = extract
    report = await worker.run()
    assert seen == [quote] and report['property_skipped'] == 1
    assert len(report['property_observations']) == 1
    entity_id = report['property_observations'][0]['entity_id']
    scope = dict(subject_person_id='cid-owner', viewer_scope='person:cid-owner', shareability='subject_private')
    view = await store.property_state(entity_id, 'model', **scope, source_ledger=ledger)
    assert view['value'] == 'model-blue' and view['kind'] == 'reported'
    assert view['observations'][0]['source_refs'][0]['source_id'] == 'source-real'
    # A consumer without access to canonical validation does not promote a
    # stored report into current truth.
    assert (await store.property_state(entity_id, 'model', **scope))['state'] == 'unknown'
    correct(ledger, operation_id='fix-person', performed_by='owner-test', old_contact_id='cid-owner',
            contact_id='cid-other', source_ids=['source-real'], evidence_refs=['source:owner-confirm'])
    assert (await store.property_state(entity_id, 'model', **scope, source_ledger=ledger))['state'] == 'unknown'
    await store.erase_property_evidence(['source:source-real'], subject_person_id='cid-owner')
    report = await worker.run()
    new_scope = dict(subject_person_id='cid-other', viewer_scope='person:cid-other', shareability='subject_private')
    new_view = await store.property_state(entity_id, 'model', **new_scope, source_ledger=ledger)
    assert new_view['value'] == 'model-blue'
    ledger.erase_sources(contact_id='cid-other', turn_ids=['source-real'])
    assert (await store.property_state(entity_id, 'model', **new_scope, source_ledger=ledger))['state'] == 'unknown'


async def test_legacy_text_batch_has_no_typed_attribution_and_optional_backends_are_explicit(store, monkeypatch):
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    monkeypatch.setenv('COLONY_WORLD_LLM_EXTRACT', 'live')
    async def extract(texts):
        return {'entities': [{'name': 'Nimbus Router', 'type': 'product', 'confidence': .8}],
                'observations': [{'entity': 'Nimbus Router', 'property': 'model', 'value': 'blue',
                                  'evidence': 'Nimbus Router runs blue.'}]}
    worker = WorldLLMExtractor(store)
    worker._llm_batch = extract
    report = await worker.run(texts=['Nimbus Router runs blue.'])
    assert not report.get('property_observations')
    saved = store._backend
    try:
        store._backend = object()
        with pytest.raises(NotImplementedError, match='require_sqlite'):
            await state(store)
    finally:
        store._backend = saved


@pytest.mark.parametrize('text', [
    'Nimbus Router will be offline next Friday.',
    'Nimbus Router was offline last month.',
    'Yesterday: Nimbus Router is offline.',
    'Nimbus Router is offline until 2026-09-15.',
])
async def test_dated_report_cannot_become_current_from_receipt_time(store, tmp_path, monkeypatch, text):
    from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
    from colony_sidecar.world_model.llm_extract import WorldLLMExtractor
    monkeypatch.setenv('COLONY_WORLD_LLM_EXTRACT', 'live')
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ledger.record_source('dated', contact_id='cid-owner', session_id='text',
                         messages=[{'role': 'user', 'content': text}])
    async def extract(texts):
        # Even a misclassified or clipped model answer cannot bypass source
        # tense/date checks and use today's ingestion as claim validity.
        return {'entities': [{'name': 'Nimbus Router', 'type': 'product', 'confidence': .8}],
                'observations': [{'entity': 'Nimbus Router', 'property': 'status', 'value': 'offline',
                                  'temporal_status': 'current', 'evidence': text.split(': ')[-1]}]}
    worker = WorldLLMExtractor(store, source_ledger=ledger)
    worker._llm_batch = extract
    result = await worker.run()
    assert not result['property_observations'] and result['property_skipped'] == 1
    assert ledger.search_sources('offline', contact_id='cid-owner', session_id='text')
