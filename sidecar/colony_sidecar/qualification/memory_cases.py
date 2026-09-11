"""Finite cases using actual memory formation and scoped lexical recollection.

The candidate serves extraction. The configured judging role is a supporting
consumer dependency, recorded by the runner; no role is silently rebound here.
These are not native conversation or base-Hermes comparison results.
"""
from contextlib import closing
from datetime import datetime
import json

from .records import CaseSpec


def _recollect(projection, query, *, contact, session, now, max_chars):
    from colony_sidecar.beliefs.source_time import interpret_time_query
    from colony_sidecar.intelligence.graph.recall import pack_memory_context
    from colony_sidecar.turns.source_annotations import expand, current_candidates
    ledger = projection.ledger
    hits = ledger.search_sources(query, contact_id=contact, session_id=session, limit=10)
    _, rows = projection.prepare_context([], hits, contact_id=contact, session_id=session,
        time_query=interpret_time_query(query, now=datetime.fromisoformat(now), timezone_name='UTC'))
    rows = expand(ledger, rows, contact_id=contact, session_id=session)
    rows = current_candidates(ledger, rows, contact_id=contact, session_id=session)
    selected, body = pack_memory_context(rows, limit=5, max_chars=max_chars)
    refs = {ref['source_id']: ref for row in selected for ref in row.get('_annotation_source_refs', [])}
    return {'body': body, 'selected': selected, 'citations': list(refs.values())}


async def source_memory(inputs, context):
    """One projection attempt per input, then reopen the same isolated ledger."""
    from colony_sidecar.beliefs.source_projection import SourceClaimProjection
    from colony_sidecar.turns import TurnIdempotencyLedger
    turns = inputs['turns']
    if not 1 <= len(turns) <= 8 or len({t['id'] for t in turns}) != len(turns):
        raise ValueError('Memory qualification needs1..8 distinct declared sources')
    ledger = TurnIdempotencyLedger(context.state_dir/'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    recorded, process_calls = [], 0
    for turn in turns:
        if not isinstance(turn['text'], str) or not 1 <= len(turn['text']) <= 2000:
            raise ValueError('Source input must be bounded text')
        assert ledger.record_source(turn['id'], contact_id=inputs['contact_id'],
            session_id=turn['session_id'], occurred_at=turn['occurred_at'],
            messages=[{'role': 'user', 'content': turn['text']}])
        recorded.append(turn['id'])
        process_calls += 1
        await projection.process_one(context.router)
        job = next(row for row in projection.status(inputs['contact_id']) if row['turn_id'] == turn['id'])
        if job['status'] != 'complete':
            break  # Retain the failed first attempt; never wait/retry its queue.
    refs_before = ledger.source_references(recorded, contact_id=inputs['contact_id'],
                                           session_id=inputs['recall_session'])
    reopened = SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path))
    refs_after = reopened.ledger.source_references(recorded, contact_id=inputs['contact_id'],
                                                  session_id=inputs['recall_session'])
    with closing(reopened.ledger._connect()) as db:
        claims = [dict(json.loads(row['data_json']), **{k: row[k] for k in (
            'id', 'turn_id', 'message_hash', 'retracted_by', 'superseded_by')})
            for row in db.execute('SELECT * FROM source_claims ORDER BY turn_id,id')]
        sources = [{'id': row['turn_id'], 'contact_id': row['contact_id'], 'session_id': row['session_id'],
                    'messages': json.loads(row['messages_json'])}
                   for row in db.execute('SELECT * FROM turn_sources ORDER BY turn_id')]
    recall = _recollect(reopened, inputs['query'], contact=inputs['contact_id'],
        session=inputs['recall_session'], now=inputs['now'], max_chars=inputs['max_chars'])
    effects = {'recorded_source_ids': recorded, 'process_invocations': process_calls,
               'reopened_source_versions_equal': refs_before == refs_after,
               'source_references': refs_after, 'recall_session': inputs['recall_session'],
               'supporting_roles': ['judging'],
               'boundary': 'canonical_source_formation_and_lexical_recollection',
               'native_request_and_answer': 'not_exercised',
               'semantic_embedding_and_reranking': 'not_exercised'}
    context.observe({'boundary': effects['boundary'], 'sources_recorded': len(recorded),
                     'projection_invocations': process_calls, 'supporting_roles': ['judging']})
    return {'output': {'sources': sources, 'claims': claims,
                       'jobs': reopened.status(inputs['contact_id']), 'recall': recall}, 'effects': effects}


def memory_outcomes(observed, oracle):
    """Compare effects with independent source facts; never ask a model to grade."""
    output, effects = observed['output'], observed['effects']
    sources = {row['id']: row for row in output['sources']}
    claims = output['claims']
    active = [row for row in claims if not row['retracted_by'] and not row['superseded_by']]
    jobs = output['jobs']
    checks = {
        'all_inputs_retained': set(sources) == set(oracle['source_ids']),
        'formation_first_attempts_complete': len(jobs) == len(oracle['source_ids']) and all(
            row['status'] == 'complete' and row['attempts'] == 1 for row in jobs),
        'state_reopened_without_losing_sources': effects['reopened_source_versions_equal'],
        'recall_uses_a_later_session': all(row['session_id'] != effects['recall_session'] for row in sources.values()),
        'promoted_evidence_is_source_grounded': all(row['turn_id'] in sources and any(
            row['evidence'] in message['content'] for message in sources[row['turn_id']]['messages']) for row in claims),
        'junk_and_fiction_not_promoted': not any(row['turn_id'] in oracle.get('no_claim_sources', []) for row in claims),
    }
    for wanted in oracle['claims']:
        matches = [row for row in active if row['turn_id'] == wanted['source_id']
                   and wanted['value_contains'].casefold() in str(row.get('value', '')).casefold()
                   and row.get('memory_quality', {}).get('memory_kind') == wanted['memory_kind']]
        checks[wanted['name']] = bool(matches)
        checks[wanted['name']+'_conditions_preserved'] = bool(matches) and all(
            all(term in row['evidence'] for term in wanted.get('evidence_contains', [])) for row in matches)
    selected = output['recall']['selected']
    current_assertions = []
    for row in selected:
        if row.get('content_format') == 'source_assertions_v1':
            card = json.loads(row['content'])
            current_assertions.extend(card.get('assertions', []))
    if oracle.get('correction'):
        corrected = oracle['correction']
        old = [row for row in claims if row['turn_id'] == corrected['old_source']]
        new = [row for row in active if row['turn_id'] == corrected['new_source']]
        checks['explicit_correction_retracts_prior_claim'] = bool(old and new) and any(
            row.get('operation') == 'correct' and row.get('prior_claim_id') == previous['id']
            and previous['retracted_by'] == row['id'] for row in new for previous in old)
        checks['corrected_value_selected'] = any(corrected['new_value'].casefold() in str(
            row.get('value', '')).casefold() for row in current_assertions)
        checks['obsolete_value_not_selected_as_current'] = not any(corrected['old_value'].casefold() in str(
            row.get('value', '')).casefold() for row in current_assertions)
    checks['useful_content_recollected'] = all(term in output['recall']['body'] for term in oracle['recall_contains'])
    checks['recollection_has_canonical_lineage'] = bool(output['recall']['citations']) and all(
        row in effects['source_references'] for row in output['recall']['citations'])
    return checks


CASES = [
    CaseSpec(id='memory.formation-quality', version='2', role='extraction', boundary='cognition_consumer',
        target_tasks=('source_claim_extraction',),
        consumer='source_memory', evaluator='memory_outcomes', timeout_seconds=240,
        inputs={'contact_id': 'person', 'recall_session': 'later-conversation',
            'query': 'tea preference after evening caffeine', 'now': '2026-04-10T12:00:00+00:00', 'max_chars': 6000,
            'turns': [
                {'id': 'turn-a', 'session_id': 'conversation-a', 'occurred_at': '2026-04-01T12:00:00+00:00',
                 'text': 'I prefer decaffeinated tea after 18:00 because caffeine keeps me awake.'},
                {'id': 'turn-b', 'session_id': 'conversation-b', 'occurred_at': '2026-04-02T12:00:00+00:00',
                 'text': 'Thanks, okay.'},
                {'id': 'turn-c', 'session_id': 'conversation-c', 'occurred_at': '2026-04-03T12:00:00+00:00',
                 'text': 'In a story I am drafting, I own a silver moonboat.'}]},
        oracle={'source_ids': ['turn-a', 'turn-b', 'turn-c'], 'no_claim_sources': ['turn-b', 'turn-c'],
            'claims': [{'name': 'useful_conditional_preference_formed', 'source_id': 'turn-a',
                'value_contains': 'decaffeinated tea', 'memory_kind': 'preference',
                'evidence_contains': ['after 18:00', 'caffeine keeps me awake']}],
            'recall_contains': ['decaffeinated tea', 'after 18:00']}),
    CaseSpec(id='memory.corrected-recollection', version='2', role='extraction', boundary='cognition_consumer',
        target_tasks=('source_claim_extraction',),
        consumer='source_memory', evaluator='memory_outcomes', timeout_seconds=180,
        inputs={'contact_id': 'person', 'recall_session': 'later-conversation',
            'query': 'Where is the spare sensor stored?', 'now': '2026-04-10T12:00:00+00:00', 'max_chars': 6000,
            'turns': [
                {'id': 'turn-a', 'session_id': 'conversation-a', 'occurred_at': '2026-04-01T12:00:00+00:00',
                 'text': 'The spare sensor is stored in the cedar drawer.'},
                {'id': 'turn-b', 'session_id': 'conversation-b', 'occurred_at': '2026-04-02T12:00:00+00:00',
                 'text': 'Correction: the spare sensor is stored in the amber cabinet, not the cedar drawer.'}]},
        oracle={'source_ids': ['turn-a', 'turn-b'],
            'claims': [{'name': 'corrected_location_formed', 'source_id': 'turn-b',
                'value_contains': 'amber cabinet', 'memory_kind': 'personal_context',
                'evidence_contains': ['Correction:', 'not the cedar drawer']}],
            'correction': {'old_source': 'turn-a', 'new_source': 'turn-b',
                'old_value': 'cedar drawer', 'new_value': 'amber cabinet'},
            'recall_contains': ['amber cabinet']})
]

CONSUMERS = {'source_memory': source_memory}
EVALUATORS = {'memory_outcomes': memory_outcomes}
