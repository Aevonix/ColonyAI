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
    from apsimo.beliefs.source_time import interpret_time_query
    from apsimo.memory.recall import pack_memory_context
    from apsimo.turns.source_annotations import expand, current_candidates
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
    from apsimo.beliefs.source_projection import SourceClaimProjection
    from apsimo.turns import TurnIdempotencyLedger
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


def _same_text(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.strip().casefold() == right.strip().casefold()


def _fact_matches(row, wanted):
    """Only oracle-authored complete alternatives, never inferred synonyms."""
    return (row.get('turn_id') == wanted['source_id']
        and row.get('memory_quality', {}).get('memory_kind') == wanted['memory_kind']
        and any(all(_same_text(row.get(key), form[key])
                    for key in ('subject', 'subject_key', 'predicate', 'value'))
                for form in wanted['representations']))


def memory_outcomes(observed, oracle):
    """Compare effects with independent source facts; never ask a model to grade."""
    output, effects = observed['output'], observed['effects']
    sources = {row['id']: row for row in output['sources']}
    claims = output['claims']
    active = [row for row in claims if not row['retracted_by'] and not row['superseded_by']]
    jobs = output['jobs']
    completed_sources = {row['turn_id'] for row in jobs
                         if row['status'] == 'complete' and row['attempts'] == 1}
    checks = {
        'all_inputs_retained': set(sources) == set(oracle['source_ids']),
        'formation_first_attempts_complete': len(jobs) == len(oracle['source_ids']) and all(
            row['status'] == 'complete' and row['attempts'] == 1 for row in jobs),
        'state_reopened_without_losing_sources': effects['reopened_source_versions_equal'],
        'recall_uses_a_later_session': all(row['session_id'] != effects['recall_session'] for row in sources.values()),
        'promoted_evidence_is_source_grounded': (all(row['turn_id'] in sources and any(
            row['evidence'] in message['content'] for message in sources[row['turn_id']]['messages'])
            for row in claims) if claims else None),
    }
    no_claim_sources = set(oracle.get('no_claim_sources', []))
    if no_claim_sources:
        # Unsubmitted or unfinished sources cannot demonstrate abstention.
        # An observed bad promotion is still a failure even if another source
        # was never reached. Do not silently remove either from the denominator.
        checks['junk_and_fiction_not_promoted'] = (
            False if any(row['turn_id'] in no_claim_sources for row in claims) else
            True if no_claim_sources <= (set(sources) & completed_sources) else None)
    for wanted in oracle['claims']:
        matches = [row for row in active if _fact_matches(row, wanted)]
        checks[wanted['name']] = bool(matches)
        checks[wanted['name']+'_conditions_preserved'] = bool(matches) and all(
            all(term in row['evidence'] for term in wanted.get('evidence_contains', [])) for row in matches)
    selected = output['recall']['selected']
    current_assertions = []
    by_id = {row['id']: row for row in claims}
    selected_fields_valid = True
    for row in selected:
        if row.get('content_format') == 'source_assertions_v1':
            card = json.loads(row['content'])
            for assertion in card.get('assertions', []):
                retained = by_id.get(assertion.get('claim_id'))
                if (retained is None or assertion.get('source') != 'turn:' + retained['turn_id']
                        or assertion.get('value') != retained['value']
                        or card.get('subject') != retained['subject']
                        or card.get('predicate') != retained['predicate']):
                    selected_fields_valid = False
                else:
                    current_assertions.append(retained)
    if oracle.get('correction'):
        corrected = oracle['correction']
        old = [row for row in claims if _fact_matches(row, corrected['old_fact'])]
        new = [row for row in active if _fact_matches(row, corrected['new_fact'])]
        checks['explicit_correction_retracts_prior_claim'] = bool(old and new) and any(
            row.get('operation') == 'correct' and row.get('prior_claim_id') == previous['id']
            and previous['retracted_by'] == row['id'] for row in new for previous in old)
        checks['corrected_value_selected'] = selected_fields_valid and any(
            _fact_matches(row, corrected['new_fact']) and row in active for row in current_assertions)
        checks['obsolete_value_not_selected_as_current'] = (not any(
            _fact_matches(row, corrected['old_fact']) for row in current_assertions)
            if selected_fields_valid else None)
    checks['useful_content_recollected'] = all(term in output['recall']['body'] for term in oracle['recall_contains'])
    checks['recollection_has_canonical_lineage'] = bool(output['recall']['citations']) and all(
        row in effects['source_references'] for row in output['recall']['citations'])
    return checks


# Finite independently authored oracle data. Articles and the two equivalent
# storage predicates are listed here, not guessed from a returned model value.
_LOCATION_REPRESENTATIONS = {
    place: [{'subject': subject, 'subject_key': subject_key, 'predicate': predicate, 'value': value}
            for subject, subject_key in (('spare sensor', 'spare sensor'), ('The spare sensor', 'the spare sensor'))
            for predicate in ('location', 'storage location') for value in values]
    for place, values in {'old': ('cedar drawer', 'the cedar drawer'),
                          'new': ('amber cabinet', 'the amber cabinet')}.items()}
_OLD_LOCATION = {'source_id': 'turn-a', 'memory_kind': 'personal_context',
                 'representations': _LOCATION_REPRESENTATIONS['old']}
_NEW_LOCATION = {'source_id': 'turn-b', 'memory_kind': 'personal_context',
                 'representations': _LOCATION_REPRESENTATIONS['new']}


CASES = [
    CaseSpec(id='memory.formation-quality', version='4', role='extraction', boundary='cognition_consumer',
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
                'memory_kind': 'preference', 'representations': [
                    {'subject': 'I', 'subject_key': 'speaker', 'predicate': 'tea preference', 'value': value}
                    for value in ('decaffeinated', 'decaffeinated tea')],
                'evidence_contains': ['after 18:00', 'caffeine keeps me awake']}],
            'recall_contains': ['decaffeinated tea', 'after 18:00']}),
    CaseSpec(id='memory.corrected-recollection', version='4', role='extraction', boundary='cognition_consumer',
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
            'claims': [{**_NEW_LOCATION, 'name': 'corrected_location_formed',
                'evidence_contains': ['Correction:', 'not the cedar drawer']}],
            'correction': {'old_fact': _OLD_LOCATION, 'new_fact': _NEW_LOCATION},
            'recall_contains': ['amber cabinet']})
]

CONSUMERS = {'source_memory': source_memory}
EVALUATORS = {'memory_outcomes': memory_outcomes}
