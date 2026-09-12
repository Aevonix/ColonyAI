"""Automatic knowledge inferences require uncorrected current source support."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from pacomind.api.routers import host
from pacomind.tom.tom2 import Tom2Store
from pacomind.turns.source_annotations import append as annotate
from test_contact_fact_recall import contact_context
from test_turn_source_evidence import source_app
from test_tom2_wiring import world, _arm_level2, _req, OWNER, READER, FACT_TEXT


def correct(facts, fact, identifier='correction'):
    ledger = facts._ledger()
    source = fact['source_lineage']['turn_id']
    reference = ledger.source_references([source], contact_id=fact['contact_id'], session_id='s1')[0]
    return annotate(ledger, contact_id=fact['contact_id'], session_id='s1',
        annotation_id=identifier, source_id=source, source_version=reference['source_version'],
        excerpt=fact['fact'], correction='The earlier interpretation is disputed; do not assume shared knowledge.',
        author_principal='test-operator')


@pytest.mark.asyncio
@pytest.mark.parametrize('p8_enabled', [False, True])
@pytest.mark.parametrize('change', ['unlinked', 'correction', 'erased_note', 'changed_source', 'corrected_evidence'])
async def test_owner_api_omits_unsupported_inference_without_uuid_fallback(
        contact_context, monkeypatch, p8_enabled, change):
    runtime = contact_context
    monkeypatch.setenv('PACOMIND_TOM2_CONTEXT', '1')
    if not p8_enabled:
        monkeypatch.setattr(host, '_p8_runtime', None)
    fact = runtime.add('The hydrofoil gate is violet.', source_linked=change != 'unlinked')
    evidence = runtime.add('The recipient was given the hydrofoil gate detail.')
    tom2 = Tom2Store()
    monkeypatch.setattr(host, '_tom2_store', tom2)
    tom2.record_inference(contact_id='contact-b', kind='unaware_of', fact_ref=fact['id'],
        evidence_refs=[evidence['id']], confidence=.4)
    payload = {'identity': {'host_id': 'native-fixture'},
        'context': {'contact_id': 'contact-a', 'session_id': 's1'},
        'incoming_message': {'role': 'user', 'content': 'hello'}}
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        async def sections():
            response = await client.post('/v1/host/context/assemble',
                headers={'Authorization': 'Bearer owner-key'}, json=payload)
            assert response.status_code == 200, response.text
            return [s for s in response.json()['sections'] if s['id'] == 'pacomind-tom2']
        if change != 'unlinked':
            before = await sections()
            assert len(before) == 1 and fact['fact'] in before[0]['body']
        if change in {'correction', 'erased_note', 'corrected_evidence'}:
            annotation = correct(runtime.facts, evidence if change == 'corrected_evidence' else fact)
            if change == 'erased_note':
                runtime.ledger.erase_sources(contact_id='contact-a', turn_ids=[annotation['source_id']])
        elif change == 'changed_source':
            with runtime.ledger._connect() as conn:
                conn.execute('UPDATE turn_sources SET messages_json=? WHERE turn_id=?',
                    (json.dumps([{'role': 'user', 'content': 'The gate information changed.'}]),
                     fact['source_lineage']['turn_id']))
        assert await sections() == []
    assert runtime.facts._conn.execute('SELECT fact FROM shared_facts WHERE id=?', (fact['id'],)).fetchone()[0] == fact['fact']
    assert len(tom2.list_inferences()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reader', [OWNER, READER])
@pytest.mark.parametrize('when', ['before', 'after_render', 'erased_note'])
async def test_all_automatic_tom2_sections_omit_corrected_knowledge(world, monkeypatch, reader, when):
    monkeypatch.setenv('PACOMIND_TOM2_CONTEXT', '1')
    _arm_level2(monkeypatch)
    before = await host.context_assemble(_req(reader))
    expected = {'pacomind-tom2'} if reader == OWNER else {'pacomind-tom2-l1', 'pacomind-tom2-l2'}
    assert {s.id for s in before.sections if s.id in expected} == expected
    if when == 'after_render':
        class Telemetry:
            async def touch(self, _):
                correct(world.facts, world.fact)
        monkeypatch.setattr(host, '_telemetry', Telemetry())
    else:
        annotation = correct(world.facts, world.fact)
        if when == 'erased_note':
            world.facts._ledger().erase_sources(contact_id=READER, turn_ids=[annotation['source_id']])
    after = await host.context_assemble(_req(reader))
    assert not any(s.id in expected for s in after.sections)
    assert world.facts.get_fact(world.fact['id'])['fact'] == FACT_TEXT


@pytest.mark.asyncio
async def test_unlinked_self_unawareness_does_not_create_content_free_prior(world, monkeypatch):
    _arm_level2(monkeypatch)
    world.tom2 = Tom2Store()
    monkeypatch.setattr(host, '_tom2_store', world.tom2)
    old = world.facts.create_fact(contact_id=READER, fact='Unlinked historical queue update.')
    world.tom2.record_inference(contact_id=READER, kind='unaware_of', fact_ref=old['id'], confidence=.4)
    response = await host.context_assemble(_req(READER))
    assert not any(s.id == 'pacomind-tom2-l1' for s in response.sections)
    assert world.facts.get_fact(old['id']) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['erase', 'replace', 'append'])
async def test_p8_cached_tom2_fact_does_not_survive_source_change_during_final_await(contact_context, monkeypatch, change):
    runtime = contact_context
    monkeypatch.setenv('PACOMIND_TOM2_CONTEXT', '1')
    fact = runtime.add('The hydrofoil gate is violet.')
    tom2 = Tom2Store()
    monkeypatch.setattr(host, '_tom2_store', tom2)
    tom2.record_inference(contact_id='contact-b', kind='unaware_of', fact_ref=fact['id'], confidence=.4)
    class Telemetry:
        async def touch(self, _):
            source = fact['source_lineage']['turn_id']
            if change == 'erase':
                runtime.ledger.erase_sources(contact_id='contact-a', turn_ids=[source])
            else:
                with runtime.ledger._connect() as conn:
                    conn.execute('UPDATE turn_sources SET messages_json=? WHERE turn_id=?',
                        (json.dumps(([{'role':'user','content':fact['fact']}] if change == 'append' else [])
                            + [{'role':'user','content':'The gate is amber.'}]), source))
    monkeypatch.setattr(host, '_telemetry', Telemetry())
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', headers={'Authorization':'Bearer owner-key'}, json={
            'identity':{'host_id':'native-fixture'}, 'context':{'contact_id':'contact-a','session_id':'s1'},
            'incoming_message':{'role':'user','content':'hello'}})
    assert response.status_code == 200, response.text
    assert not any(section['id'] == 'pacomind-tom2' for section in response.json()['sections'])
