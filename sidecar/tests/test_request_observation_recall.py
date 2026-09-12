"""Exact recorded origins add original evidence without promoting rerank scores."""
from copy import deepcopy
import hashlib
import json
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.intelligence.graph.recall import pack_memory_context, source_candidates, calibration_fingerprint
from apsimo.turns import TurnIdempotencyLedger, canonical_turn_digest
from apsimo.turns.idempotency import source_message_hash
from apsimo.turns.source_annotations import expand, current_candidates
from apsimo.turns.tool_observations import (
    ToolObservation, LINKED_INCLUSION_POLICY, expand_selected, identity_id, record,
)
from test_turn_source_evidence import source_app


SCOPE = {'contact_id': 'person', 'session_id': 'later-channel'}
REQUEST = 'Inspect the copper export and its checksum before reporting whether it is ready.'


def instruction(ledger, text=REQUEST, *, turn='native-turn', contact='person', session='native-session'):
    identifier = 'task-instruction:' + hashlib.sha256(json.dumps(
        [contact, session, turn, text], sort_keys=True).encode()).hexdigest()
    ledger.record_source(identifier, contact_id=contact, session_id=session,
        messages=[{'role': 'user', 'content': text}], derive_claims=False)
    return ledger.source_references([identifier], contact_id=contact, session_id=session)[0]


def observation(ledger, origin, text, *, call=1, turn='native-turn', session='native-session',
                contact='person', sources=()):
    native = {'profile_id': 'a'*64, 'session_id': session, 'task_id': 'native-task',
        'turn_id': turn, 'tool_call_id': 'call-'+str(call), 'api_request_id': 'api-'+str(call),
        'tool_name': 'read_file', 'message_id': call, 'timestamp': 1700000000.0 + call,
        'result_sha256': hashlib.sha256(text.encode()).hexdigest()}
    value = ToolObservation(native=native, origin=origin, sources=list(sources),
        content=text, reason='Retain the original inspection evidence for later work.')
    identifier = identity_id(native)
    record(ledger, value, contact_id=contact, session_id=session, source_id=identifier)
    return ledger.source_references([identifier], contact_id=contact, session_id=session)[0]


def selected(ledger, origin):
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        source = dict(conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (origin['source_id'],)).fetchone())
    message = json.loads(source['messages_json'])[0]
    row = source_candidates([{**source, **message,
        'source_message_hash': source_message_hash(source['session_id'], message)}])[0]
    row.update(rerank_score=.99, rerank_status='scored', relevance=.99)
    return expand(ledger, [row], **SCOPE)


@pytest.fixture
def saved(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    return ledger, instruction(ledger)


def annotate(ledger, ref, text, note, identifier='correction'):
    return ledger.append_source_annotation(**SCOPE, **ref, excerpt=text,
        correction=note, annotation_id=identifier, author_principal='owner')


def test_full_long_original_is_added_without_changing_selected_evidence_or_score(saved):
    ledger, origin = saved
    text = json.dumps({'content': '# Example engine\nRepository: https://example.invalid/engine\n'
        + ('Stable installation documentation.\n' * 85), 'file_truncated': True})
    retained = observation(ledger, origin, text)
    seed = selected(ledger, origin)
    before = deepcopy(seed)
    rows, body = expand_selected(ledger, seed, **SCOPE)
    assert seed == before and rows[:1] == before
    assert len(rows) == 2 and rows[1]['content'] == text
    assert not rows[1].get('excerpt_truncated') and len(body) <= 6000
    assert rows[1]['linked_inclusion_policy'] == LINKED_INCLUSION_POLICY
    assert LINKED_INCLUSION_POLICY not in body and 'linked_inclusion_policy' not in body
    assert rows[1]['linked_observation_of'] == origin['source_id']
    assert rows[1]['role'] == 'tool' and rows[1]['epistemic_state'] == 'quotation'
    assert 'rerank_score' not in rows[1] and rows[1].get('rerank_status') != 'scored'
    assert retained in rows[1]['_annotation_source_refs'] and origin in rows[1]['_annotation_source_refs']


def test_request_only_negative_has_no_observation_or_invented_outcome(saved):
    ledger, origin = saved
    seed = selected(ledger, origin)
    assert expand_selected(ledger, seed, **SCOPE) == pack_memory_context(seed)


@pytest.mark.parametrize('coverage', ['complete_original', 'truncated_original', 'dependency_only'])
def test_ranked_observation_coverage_preserves_selected_rows_and_adds_missing_complete_set(saved, coverage):
    ledger, origin = saved
    failure = 'Copper digest mismatch. Exit 1. Nothing was modified.'
    success = 'Copper digest now matches. Exit 0. Only the index was repaired.'
    first = observation(ledger, origin, failure)
    second = observation(ledger, origin, success, call=2)
    ranked = selected(ledger, first)[0]
    if coverage == 'truncated_original':
        ranked.update(content=failure[:20], excerpt_truncated=True)
    elif coverage == 'dependency_only':
        ledger.record_source('assistant-retelling', contact_id='person', session_id='native-session',
            messages=[{'role': 'assistant', 'content': 'I considered the earlier inspection.',
                       '_supplied_sources': [first]}], derive_claims=False)
        ref = ledger.source_references(['assistant-retelling'], **SCOPE)[0]
        ranked = selected(ledger, ref)[0]
        assert first in ranked['_annotation_source_refs']
    seed = [*selected(ledger, origin), ranked]
    before = deepcopy(seed)
    rows, body = expand_selected(ledger, seed, **SCOPE,
        limit=4 if coverage == 'dependency_only' else 3, max_chars=12000)
    assert seed == before and rows[:2] == before
    if coverage == 'truncated_original':
        assert len(rows) == 3 and rows[2]['epistemic_state'] == 'incomplete_linked_evidence'
        assert success not in body and 'partially shown' in body
    else:
        added = rows[2:]
        assert {row['source_turn_id'] for row in added} == (
            {second['source_id']} if coverage == 'complete_original'
            else {first['source_id'], second['source_id']})
        assert all({origin['source_id'], first['source_id'], second['source_id']}
            <= {r['source_id'] for r in row['_annotation_source_refs']} for row in added)
        assert failure in body and success in body
    assert LINKED_INCLUSION_POLICY not in body


def test_failed_and_successful_checks_are_kept_together_with_both_corrections(saved):
    ledger, origin = saved
    failure = 'Copper digest mismatch. Exit 1. Nothing was modified.'
    success = 'Copper digest now matches. Exit 0. Only the index was repaired.'
    first = observation(ledger, origin, failure)
    second = observation(ledger, origin, success, call=2)
    a = annotate(ledger, origin, REQUEST, 'Readiness also requires the separate owner review.')
    b = annotate(ledger, second, success, 'This check covered the index, not the complete export.', 'second')
    seed = selected(ledger, origin)
    rows, body = expand_selected(ledger, seed, **SCOPE, max_chars=12000)
    assert rows[:len(seed)] == seed and len(rows) == len(seed)+2
    assert failure in body and success in body
    assert 'separate owner review' in body and 'not the complete export' in body
    for row in rows[len(seed):]:
        assert {r['source_id'] for r in row['_annotation_source_refs']} == {
            origin['source_id'], first['source_id'], second['source_id'], a['source_id'], b['source_id']}
    # A change to one member invalidates the complete added set, not just that row.
    annotate(ledger, first, failure, 'The failing check used an outdated manifest.', 'third')
    assert current_candidates(ledger, rows, **SCOPE) == seed


@pytest.mark.parametrize('count,large', [(5, False), (2, True)])
def test_complete_set_overflow_is_an_incomplete_opening_notice_not_a_chosen_result(saved, count, large):
    ledger, origin = saved
    for call in range(1, count+1):
        observation(ledger, origin, 'Result marker '+str(call)+(' data'*1800 if large and call == 2 else ''), call=call)
    seed = selected(ledger, origin)
    rows, body = expand_selected(ledger, seed, **SCOPE)
    assert rows[:1] == seed and len(rows) == 2 and len(body) <= 6000
    assert rows[1]['epistemic_state'] == 'incomplete_linked_evidence'
    assert 'Incomplete retained-observation context' in body
    assert 'Result marker' not in body and rows[1]['source_anchors']
    refs = rows[1]['_annotation_source_refs']
    assert all(any(ref['source_id'] == anchor['source_id'] for ref in refs)
        for anchor in rows[1]['source_anchors'])
    no_space, original = expand_selected(ledger, seed, **SCOPE, max_chars=len(pack_memory_context(seed)[1]))
    assert no_space == seed and original == pack_memory_context(seed)[1]


def test_even_unrelated_exact_observation_never_displaces_independently_selected_answer(saved):
    ledger, origin = saved
    observation(ledger, origin, 'Unrelated retained weather reading: 18 degrees.')
    seed = selected(ledger, origin)
    answer = {'id': 'independent-answer', 'kind': 'belief', 'content': 'Copper export needs a signature.',
              'rerank_score': .97, 'relevance': .97}
    base = [*seed, answer]
    rows, _ = expand_selected(ledger, base, **SCOPE)
    assert rows[:2] == base and len(rows) == 3
    assert rows[-1]['history_scope'] == 'retained_observations_only'
    assert expand_selected(ledger, base, **SCOPE, limit=2) == pack_memory_context(base, limit=2)


def test_old_supplied_dependency_and_neighboring_turn_are_not_origin(saved):
    ledger, origin = saved
    other = instruction(ledger, 'Read the unrelated weather station.', turn='another-turn')
    observation(ledger, other, 'Neighboring weather reading.', turn='another-turn', sources=[origin])
    same_words = instruction(ledger, REQUEST, turn='repeated-turn')
    observation(ledger, same_words, 'Same words, different exact request.', call=2, turn='repeated-turn')
    seed = selected(ledger, origin)
    assert expand_selected(ledger, seed, **SCOPE) == pack_memory_context(seed)


@pytest.mark.parametrize('change', ['content', 'call_identity', 'origin_revision', 'native_turn'])
def test_tampered_canonical_observation_cannot_establish_a_link(saved, change):
    ledger, origin = saved
    ref = observation(ledger, origin, 'The export check returned exit 1.')
    with sqlite3.connect(ledger.db_path) as conn:
        messages = json.loads(conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (ref['source_id'],)).fetchone()[0])
        if change == 'content':
            messages[0]['content'] = 'Forged successful result.'
        elif change == 'call_identity':
            messages[0]['provenance']['native']['tool_call_id'] = 'different-call'
        elif change == 'origin_revision':
            messages[0]['_observation_sources'][0]['source_version'] = 'f'*64
        else:
            messages[0]['provenance']['native']['turn_id'] = 'different-turn'
        conn.execute('UPDATE turn_sources SET messages_json=? WHERE turn_id=?', (json.dumps(messages), ref['source_id']))
    seed = selected(ledger, origin)
    assert expand_selected(ledger, seed, **SCOPE) == pack_memory_context(seed)


def test_legacy_origin_missing_native_provenance_and_other_viewer_do_not_expand(saved):
    ledger, origin = saved
    ledger.record_source('legacy', contact_id='person', session_id='native-session', derive_claims=False,
        messages=[{'role': 'tool', 'content': 'Legacy bytes without exact native identity.',
            '_native_tool_observation': 'native-tool-observation-v1', '_observation_sources': [origin]}])
    seed = selected(ledger, origin)
    assert expand_selected(ledger, seed, **SCOPE) == pack_memory_context(seed)
    assert expand_selected(ledger, seed, contact_id='stranger', session_id='later-channel')[0] == seed
    # This helper preserves its caller's already-authorized selected evidence;
    # it never grants another viewer any linked records. The host owns baseline authorization.


@pytest.mark.parametrize('operation', ['erase_origin', 'erase_result', 'reassign_origin', 'withdraw_annotation'])
def test_current_source_changes_remove_linked_evidence_and_its_descendants(saved, operation):
    ledger, origin = saved
    ref = observation(ledger, origin, 'Copper inspection failed with exit 1.')
    seed = selected(ledger, origin)
    if operation == 'erase_origin':
        assert ref['source_id'] in ledger.erase_sources(contact_id='person', turn_ids=[origin['source_id']])['affected_source_ids']
    elif operation == 'erase_result':
        ledger.erase_sources(contact_id='person', turn_ids=[ref['source_id']])
    elif operation == 'reassign_origin':
        from apsimo.turns.source_attribution import correct
        changed = correct(ledger, operation_id='identity-correction', performed_by='owner',
            old_contact_id='person', contact_id='another-person', source_ids=[origin['source_id']],
            evidence_refs=['owner-confirmation'])
        assert ref['source_id'] in changed['invalidated_source_ids']
        assert not ledger.source_references([ref['source_id']], **SCOPE)
    else:
        note = annotate(ledger, origin, REQUEST, 'This was only a simulated inspection.')
        ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    rows, _ = expand_selected(ledger, seed, **SCOPE)
    assert all('linked_inclusion_policy' not in row for row in rows)


def test_native_observation_read_inherits_original_request_correction(saved):
    from apsimo.turns.source_read import read
    ledger, origin = saved
    ref = observation(ledger, origin, 'Copper check completed.')
    note = annotate(ledger, origin, REQUEST, 'The request described a fictional exercise.')
    opened = read(ledger, **SCOPE, **ref)
    assert 'fictional exercise' in opened['content']
    assert note['source_id'] in {r['source_id'] for r in opened['source_refs']}


@pytest.mark.parametrize('change', ['correct_origin', 'correct_result', 'erase_result'])
def test_mutation_during_added_packet_packing_cannot_publish_a_stale_partial_set(saved, monkeypatch, change):
    from apsimo.intelligence.graph import recall
    ledger, origin = saved
    first = observation(ledger, origin, 'Copper check failed.')
    observation(ledger, origin, 'Copper check subsequently passed.', call=2)
    seed = selected(ledger, origin)
    original_pack = recall.pack_memory_context
    changed = False
    def change_after_pack(rows, **kwargs):
        nonlocal changed
        result = original_pack(rows, **kwargs)
        if not changed and len(rows) == 3:
            changed = True
            if change == 'correct_origin':
                annotate(ledger, origin, REQUEST, 'This request was a simulated exercise.')
            elif change == 'correct_result':
                annotate(ledger, first, 'Copper check failed.', 'This used an outdated input manifest.')
            else:
                ledger.erase_sources(contact_id='person', turn_ids=[first['source_id']])
        return result
    monkeypatch.setattr(recall, 'pack_memory_context', change_after_pack)
    rows, _ = expand_selected(ledger, seed, **SCOPE)
    assert changed
    published = current_candidates(ledger, rows, **SCOPE)
    assert not any('linked_inclusion_policy' in row for row in published)
    assert published == ([] if change == 'correct_origin' else seed)


def test_origin_lookup_has_bounded_index_order(saved):
    ledger, origin = saved
    with sqlite3.connect(ledger.db_path) as conn:
        plan = conn.execute('''EXPLAIN QUERY PLAN SELECT turn_id FROM turn_sources
            WHERE contact_id=? AND json_extract(messages_json, '$[0]._observation_sources[0].source_id')=?
            AND json_extract(messages_json, '$[0]._native_tool_observation')='native-tool-observation-v1'
            AND (scope='person' OR session_id=?)
            AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=turn_sources.turn_id)
            ORDER BY turn_id LIMIT 5''', ('person', origin['source_id'], SCOPE['session_id'])).fetchall()
    details = ' '.join(row[-1] for row in plan)
    assert 'native_observation_origin' in details and 'TEMP B-TREE' not in details


@pytest.mark.asyncio
@pytest.mark.parametrize('optional_failure', [False, True])
async def test_host_recall_injects_linked_low_scored_original_and_real_citations(source_app, tmp_path, monkeypatch, optional_failure):
    from apsimo.api.routers import host
    from apsimo.intelligence.graph.selection import RecallSelector
    from apsimo.turns.source_vectors import SourceVectors
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    origin = instruction(ledger)
    text = 'Copper checksum mismatch. Exit 1. No repair was performed.'
    ref = observation(ledger, origin, text)
    calls = []
    metadata = {'fixture': 'fixed-ranking'}
    async def rerank(query, docs, top_k):
        calls.append(list(docs))
        return [{'index': i, 'score': .99 if doc == REQUEST else .1} for i, doc in enumerate(docs)]
    async def no_semantic(*args, **kwargs):
        return [], []
    monkeypatch.setattr(SourceVectors, 'search', no_semantic)
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'on')
    monkeypatch.setenv('COLONY_RECALL_RERANK_MIN_SCORE', '.7')
    monkeypatch.setenv('COLONY_RECALL_RERANK_CALIBRATION', calibration_fingerprint(metadata))
    selector = RecallSelector(rerank, calibration_metadata=lambda: metadata)
    monkeypatch.setattr(host, '_memory_context_selector', lambda: selector)
    if optional_failure:
        from apsimo.turns import tool_observations
        def unavailable(*args, **kwargs):
            raise RuntimeError('optional linked lookup unavailable')
        monkeypatch.setattr(tool_observations, 'expand_selected', unavailable)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'}, 'context': SCOPE,
            'incoming_message': {'role': 'user', 'content': 'What happened with the copper checksum?'}})
    assert response.status_code == 200, response.text
    packet = next(s for s in response.json()['sections'] if s['id'] == 'colony-memory')
    assert REQUEST in packet['body']
    assert (text in packet['body']) is (not optional_failure)
    assert LINKED_INCLUSION_POLICY not in packet['body']
    assert ('linked_observation_of' in packet['body']) is (not optional_failure)
    assert {r['source_id'] for r in packet['citations']} == ({origin['source_id']} if optional_failure
        else {origin['source_id'], ref['source_id']})
    assert len(calls) == 1 and text in calls[0]
