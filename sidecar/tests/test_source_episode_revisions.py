"""Episode correction ancestry and event dates use the existing source ledger."""
from datetime import datetime, timezone
import json

from httpx import ASGITransport, AsyncClient
import jsonschema
import pytest

from colony_sidecar.beliefs.source_claims import claim_response_schema, validated_claims
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.beliefs.source_time import interpret_time_query
from colony_sidecar.turns import TurnIdempotencyLedger
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
    jsonschema.validate([proposal], schema)
    for identifier in ('invented', 'ordinary-fact'):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate([proposal | {'prior_claim_id': identifier}], schema)


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
async def test_ordinary_correction_retracts_prior_episode_and_retains_identity_basis(source_app, tmp_path):
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
    assert current['subject_basis']['disposition'] == 'episode_identity_only'
    ledger.erase_sources(contact_id='contact-a', turn_ids=['correction'])
    assert context(projection, QUERY) == []  # Erasing the correction never revives its old value.


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['after_commit', 'during_review'])
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
        if when == 'during_review' and kwargs['context']['task'] == 'source_claim_review':
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
