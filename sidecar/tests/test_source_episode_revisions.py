"""Episode correction ancestry and event dates use the existing source ledger."""
from datetime import datetime, timezone
import json

from httpx import ASGITransport, AsyncClient
import jsonschema
import pytest

from pacomind.beliefs.source_claims import claim_response_schema, validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.beliefs.source_time import interpret_time_query
from pacomind.turns import TurnIdempotencyLedger
from test_source_claim_projection import Model, ingest
from test_source_episode_memory import episode
from test_turn_source_evidence import source_app


REPORT = ('On 2026-08-24, the pressure sensor reset twice during a four-hour bench run, '
          'both times after the pump started. The cause was not established.')
CORRECTION = 'Correction: it reset only once, not twice. The first entry was copied twice.'
QUERY = 'What pressure sensor incident happened on 2026-08-24?'


def claims(ledger):
    with ledger._connect() as conn:
        return [dict(row) | json.loads(row['data_json']) for row in conn.execute('SELECT * FROM source_claims')]


def context(projection, query='pressure sensor'):
    hits = projection.ledger.search_sources(query, contact_id='contact-a', session_id='later')
    _, result = projection.prepare_context([], hits, contact_id='contact-a', session_id='later',
        time_query=interpret_time_query(query, now=datetime(2026, 9, 10, tzinfo=timezone.utc)))
    return [row for row in result if row.get('atomic_evidence')]


async def record(ledger, projection, name, text, proposal):
    ledger.record_source(name, contact_id='contact-a', session_id=name + '-session',
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-09-10T12:00:00+00:00')
    model = Model({text: proposal})
    assert await projection.process_one(model)
    return model


@pytest.mark.parametrize('expression,expected,ignored', [
    ('2026-08-24', '2026-08-24T00:00:00+00:00', 0),
    ('2026-09-10', None, 1),  # Report timestamp absent from the observation.
    ({'date': '2026-08-24'}, None, 1),
    (None, None, 0),
])
def test_optional_episode_date_never_rewrites_the_report(expression, expected, ignored):
    diagnostic = {}
    row, = validated_claims(json.dumps([episode(REPORT) | {'event_at_text': expression}]),
        message=REPORT, prior=[], observed_at='2026-09-10T12:00:00+00:00', diagnostics=diagnostic)
    assert row['evidence'] == row['value'] == REPORT
    assert row['event_at'] == expected
    assert diagnostic['ignored_episode_date_count'] == ignored
    assert diagnostic['accepted_count'] == 1 and diagnostic['rejected_count'] == 0
    if ignored:
        assert row['event_time'] == {'status': 'unknown'}


def test_only_offered_episode_ids_can_be_selected_by_the_decoder():
    prior = [{'id': 'episode-first', 'representation': 'episode'},
             {'id': 'ordinary-fact', 'representation': 'assertion'}]
    schema = claim_response_schema(CORRECTION, prior=prior)['schema']
    proposal = episode(CORRECTION) | {'operation': 'correct', 'prior_claim_id': 'episode-first'}
    proposal = {k: v for k, v in proposal.items() if k not in {'representation', 'memory_kind'}}
    jsonschema.validate([proposal], schema)
    for identifier in ('invented', 'ordinary-fact'):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate([proposal | {'prior_claim_id': identifier}], schema)


@pytest.mark.asyncio
async def test_episode_correction_kind_comes_from_the_selected_stored_record(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    original, = claims(ledger)
    proposal = {k: v for k, v in episode(CORRECTION).items()
                if k not in {'representation', 'memory_kind'}}
    proposal.update(operation='correct', prior_claim_id=original['id'])
    jsonschema.validate([proposal], claim_response_schema(CORRECTION, prior=[original])['schema'])
    await record(ledger, projection, 'correction', CORRECTION, proposal)
    current = next(row for row in claims(ledger) if row['turn_id'] == 'correction')
    assert current['representation'] == 'episode'
    assert current['memory_quality']['memory_kind'] == 'substantive_event'
    assert current['prior_claim_id'] == original['id']
    assert current['evidence'] == current['value'] == CORRECTION


@pytest.mark.asyncio
async def test_structured_branch_cannot_correct_an_episode_or_silently_change_its_type(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    original, = claims(ledger)
    # The actual failed trial chose a structured count despite its episode ID.
    wrong = {'representation': 'assertion', 'subject': 'pressure sensor', 'predicate': 'resets',
        'value': 'once', 'evidence': CORRECTION, 'operation': 'correct',
        'prior_claim_id': original['id'], 'memory_kind': 'personal_context',
        'recall_reason': 'Use the corrected count when reviewing the incident.',
        'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([wrong], claim_response_schema(CORRECTION, prior=[original])['schema'])
    diagnostic = {}
    assert validated_claims(json.dumps([wrong]), message=CORRECTION, prior=[original],
        observed_at=None, diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {'episode_representation_mismatch': 1}
    assert claims(ledger)[0]['retracted_by'] is None


@pytest.mark.asyncio
async def test_exact_event_date_is_recalled_separately_from_report_time(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'sensor', REPORT,
        episode(REPORT) | {'event_at_text': '2026-08-24'})
    selected, = context(projection, QUERY)
    row, = json.loads(selected['content'])['assertions']
    assert row['event_at'] == '2026-08-24T00:00:00+00:00'
    assert row['reported_at'] == '2026-09-10T12:00:00+00:00'
    assert row['event_time']['precision'] == 'calendar_day'
    assert context(projection, QUERY.replace('2026-08-24', '2026-08-25')) == []


@pytest.mark.asyncio
async def test_unknown_episode_time_is_labelled_in_relevant_date_query(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'sensor', REPORT,
        episode(REPORT) | {'event_at_text': '2026-09-10'})
    selected, = context(projection, QUERY)
    assert selected['validity_status'] == 'query_time_unresolved'
    row, = json.loads(selected['content'])['assertions']
    assert row['event_at'] is None and row['quote'] == REPORT
    assert row['event_time'] == {'status': 'unknown'}


@pytest.mark.asyncio
async def test_ordinary_api_preserves_calendar_day_overlap_across_source_and_query_timezones(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        await ingest(client, 'sensor', REPORT, tz='UTC', occurred='2026-09-10T12:00:00+00:00')
        assert await projection.process_one(Model({REPORT: episode(REPORT) | {'event_at_text': '2026-08-24'}}))
        for date, expected in [('2026-08-24', True), ('2026-08-25', False)]:
            response = await client.post('/v1/host/context/assemble', json={
                'identity': {'host_id': 'test-host'},
                'context': {'contact_id': 'contact-a', 'session_id': 'later', 'timezone': 'America/New_York'},
                'incoming_message': {'role': 'user', 'content': QUERY.replace('2026-08-24', date)}})
            assert response.status_code == 200, response.text
            memories = [s for s in response.json()['sections'] if s['id'] == 'pacomind-memory']
            assert bool(memories) is expected
            if memories:
                assert '2026-08-24T00:00:00+00:00' in memories[0]['body']
                assert 'calendar_day' in memories[0]['body']
                assert 'query_time_unresolved' in memories[0]['body']


@pytest.mark.asyncio
async def test_ordinary_correction_retains_qualified_prior_report_context(source_app, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    model = Model({REPORT: episode(REPORT) | {'event_at_text': '2026-08-24'},
        CORRECTION: episode(CORRECTION) | {'operation': 'correct', 'match_prior': True}})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        await ingest(client, 'original', REPORT, occurred='2026-09-10T12:00:00+00:00')
        assert await projection.process_one(model)
        await ingest(client, 'correction', CORRECTION, occurred='2026-09-10T12:01:00+00:00')
        assert await projection.process_one(model)
    stored = claims(ledger)
    old = next(row for row in stored if row['turn_id'] == 'original')
    new = next(row for row in stored if row['turn_id'] == 'correction')
    assert old['retracted_by'] == new['id']
    assert new['subject_key'] == old['subject_key']
    assert new['operation'] == 'correct' and new['subject_basis_claim_id'] == old['id']
    selected, = context(SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path)), QUERY)
    assert selected['validity_status'] == 'query_time_unresolved'
    current, = json.loads(selected['content'])['assertions']
    assert current['value'] == CORRECTION and current['operation'] == 'correct'
    assert current['event_at'] is None  # No date inherited from a different quotation.
    assert current['subject_basis']['evidence'] == REPORT
    basis = current['subject_basis']
    assert basis['disposition'] == 'prior_episode_report'
    assert 'Unchanged details remain attributed earlier context' in basis['value_use']
    assert 'Corrected or withdrawn details are not current' in basis['value_use']
    assert basis['event_at'] == '2026-08-24T00:00:00+00:00'
    assert basis['reported_at'] == '2026-09-10T12:00:00+00:00'
    assert set(selected['source_turn_ids']) == {'original', 'correction'}
    ledger.erase_sources(contact_id='contact-a', turn_ids=['correction'])
    assert context(projection, QUERY) == []  # Erasing the correction never revives its old value.


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['after_commit', 'during_extraction'])
async def test_erasing_episode_basis_prevents_or_withdraws_dependent_correction(tmp_path, when):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    ledger.record_source('correction', contact_id='contact-a', session_id='later',
        messages=[{'role': 'user', 'content': CORRECTION}])
    processor = Model({CORRECTION: episode(CORRECTION) | {'operation': 'correct', 'match_prior': True}})
    complete = processor.complete
    async def complete_then_erase(*args, **kwargs):
        answer = await complete(*args, **kwargs)
        if when == 'during_extraction' and kwargs['context']['task'] == 'source_claim_extraction':
            ledger.erase_sources(contact_id='contact-a', turn_ids=['original'])
        return answer
    processor.complete = complete_then_erase
    assert await projection.process_one(processor)
    if when == 'after_commit':
        assert len(claims(ledger)) == 2
        ledger.erase_sources(contact_id='contact-a', turn_ids=['original'])
    assert claims(ledger) == []
    assert ledger.source_references(['correction'], contact_id='contact-a', session_id='later')


@pytest.mark.asyncio
async def test_non_correction_or_unknown_episode_reference_cannot_retract(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    previous, = claims(ledger)
    for text, identifier in [(CORRECTION, 'not-offered'),
        ('A separate pressure sensor run had a single reset.', previous['id'])]:
        proposal = episode(text) | {'operation': 'correct', 'prior_claim_id': identifier}
        assert validated_claims(json.dumps([proposal]), message=text, prior=[previous], observed_at=None) == []
    assert claims(ledger)[0]['retracted_by'] is None


@pytest.mark.asyncio
async def test_whole_report_withdrawal_does_not_revive_its_details(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    withdrawal = ('Correction: the pressure sensor bench run never happened. '
                  'I invented the entire report and withdraw all of it.')
    await record(ledger, projection, 'withdrawal', withdrawal,
        episode(withdrawal) | {'operation': 'correct', 'match_prior': True})
    selected, = context(projection)
    current, = json.loads(selected['content'])['assertions']
    assert current['value'] == withdrawal
    assert current['subject_basis']['disposition'] == 'prior_episode_report'
    assert 'A withdrawal of the whole report withdraws all its details.' in current['subject_basis']['value_use']
    assert all(row['retracted_by'] for row in claims(ledger) if row['turn_id'] == 'original')
    ledger.erase_sources(contact_id='contact-a', turn_ids=['withdrawal'])
    assert context(projection) == []


@pytest.mark.asyncio
async def test_successive_partial_corrections_require_history_and_keep_read_dependencies(tmp_path):
    from pacomind.turns.source_read import read

    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    await record(ledger, projection, 'count-correction', CORRECTION,
        episode(CORRECTION) | {'operation': 'correct', 'match_prior': True})
    duration = 'Correction: that pressure sensor bench run lasted three hours, not four.'
    await record(ledger, projection, 'duration-correction', duration,
        episode(duration) | {'operation': 'correct', 'match_prior': True})
    selected, = context(projection)
    current, = json.loads(selected['content'])['assertions']
    assert current['value'] == duration
    assert current['subject_basis']['disposition'] == 'episode_history_incomplete'
    assert 'Do not infer current details' in current['subject_basis']['value_use']
    assert current['subject_basis']['evidence'] == REPORT
    assert current['prior_claim_id'] != current['subject_basis']['claim_id']
    # No synthetic count is materialized from the original plus latest text.
    assert CORRECTION not in selected['content']

    reference, = ledger.source_references(['duration-correction'], contact_id='contact-a', session_id='later')
    def history():
        return read(ledger, contact_id='contact-a', session_id='later', **reference,
            view='assertions', claim_id=current['claim_id'])
    before = history()
    history_rows = json.loads(before['content'])['assertions']
    assert [row['evidence'] for row in history_rows] == [REPORT, CORRECTION, duration]
    assert {ref['source_id'] for ref in before['source_refs']} == {
        'original', 'count-correction', 'duration-correction'}

    ledger.erase_sources(contact_id='contact-a', turn_ids=['count-correction'])
    assert ledger.erasure_watermark('contact-a') > before['watermark']
    available = ledger.source_references([ref['source_id'] for ref in before['source_refs']],
        contact_id='contact-a', session_id='later')
    assert any(ref not in available for ref in before['source_refs'])
    after = history()
    assert CORRECTION not in after['content']
    assert after['read_revision'] != before['read_revision']
    opened = json.loads(after['content'])
    assert opened['episode_history'] == 'incomplete_revision_chain'
    assert 'Complete pagination does not close this gap' in opened['guidance']
    remaining = opened['assertions']
    assert current['prior_claim_id'] not in {row['id'] for row in remaining}
    current_after, = json.loads(context(projection)[0]['content'])['assertions']
    assert current_after['subject_basis']['disposition'] == 'episode_history_incomplete'
    assert 'missing or withdrawn revisions cannot be reconstructed' in current_after['subject_basis']['value_use']


@pytest.mark.asyncio
async def test_erased_terminal_correction_leaves_explicit_history_gap(tmp_path):
    from pacomind.turns.source_read import read

    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    original, = claims(ledger)
    await record(ledger, projection, 'correction', CORRECTION,
        episode(CORRECTION) | {'operation': 'correct', 'match_prior': True})
    reference, = ledger.source_references(['original'], contact_id='contact-a', session_id='later')
    def history():
        return read(ledger, contact_id='contact-a', session_id='later', **reference,
            view='assertions', claim_id=original['id'])
    before = history()
    assert 'episode_history' not in json.loads(before['content'])

    ledger.erase_sources(contact_id='contact-a', turn_ids=['correction'])
    after = history()
    opened = json.loads(after['content'])
    assert opened['episode_history'] == 'incomplete_revision_chain'
    assert 'Complete pagination does not close this gap' in opened['guidance']
    remaining, = opened['assertions']
    assert remaining['prior_claim_id'] is None and remaining['retracted_by']
    assert remaining['evidence'] == REPORT
    assert CORRECTION not in after['content']
    assert after['watermark'] > before['watermark']
    assert after['read_revision'] != before['read_revision']
    assert context(projection) == []  # The withdrawn report never becomes current.


@pytest.mark.asyncio
async def test_later_episode_correction_finds_current_revision_through_original_topic(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    await record(ledger, projection, 'count-correction', CORRECTION,
        episode(CORRECTION) | {'operation': 'correct', 'match_prior': True})
    duration = 'I misspoke: the pressure sensor bench run lasted three hours.'
    hits = ledger.search_sources(duration, contact_id='contact-a', session_id='later', limit=10)
    assert [hit['turn_id'] for hit in hits] == ['original']
    # The current revision has no lexical match. The original record supplies
    # the topic, but only the retained current handle may be corrected.
    processor = await record(ledger, projection, 'duration-correction', duration,
        episode(duration) | {'operation': 'correct', 'match_prior': True})
    prior, = processor.calls[0][0]['prior_assertions']
    stored = {row['turn_id']: row for row in claims(ledger)}
    assert prior['id'] == stored['count-correction']['id']
    assert prior['evidence'] == CORRECTION
    assert prior['subject_basis']['evidence'] == REPORT
    assert prior['subject_basis']['disposition'] == 'prior_episode_report'
    assert stored['duration-correction']['prior_claim_id'] == prior['id']
    current, = json.loads(context(projection)[0]['content'])['assertions']
    assert current['subject_basis']['disposition'] == 'episode_history_incomplete'


@pytest.mark.asyncio
@pytest.mark.parametrize('removal', ['erase-current', 'erase-root', 'reattribute-current'])
async def test_episode_topic_lookup_cannot_resurrect_an_unavailable_current_revision(tmp_path, removal):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode(REPORT))
    await record(ledger, projection, 'count-correction', CORRECTION,
        episode(CORRECTION) | {'operation': 'correct', 'match_prior': True})
    if removal == 'reattribute-current':
        from pacomind.turns.source_attribution import correct
        correct(ledger, operation_id='move-revision', performed_by='operator',
            old_contact_id='contact-a', contact_id='contact-b', source_ids=['count-correction'],
            evidence_refs=['operator:correction'])
    else:
        ledger.erase_sources(contact_id='contact-a',
            turn_ids=['original' if removal == 'erase-root' else 'count-correction'])
    assert projection.prior({'turn_id': 'later', 'contact_id': 'contact-a', 'session_id': 'later'},
        {'role': 'user', 'content': 'I misspoke: the pressure sensor bench run lasted three hours.'}) == []
