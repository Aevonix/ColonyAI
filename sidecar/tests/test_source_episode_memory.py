"""Attributed episodes use the existing source, admission and opinion lifecycle."""
import json

from httpx import ASGITransport, AsyncClient
import jsonschema
import pytest

from colony_sidecar.beliefs.source_claims import claim_response_schema, validated_claims
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.self_model.judgments import SelfJudgments
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns import source_read
from test_self_judgments import Processor
from test_self_perspective import perspective, tell
from test_source_claim_projection import Model
from test_turn_source_evidence import source_app


REPORT = ('In a controlled checkpoint comparison, six short local tasks took '
          'one second without checkpoints and three seconds with checkpoints. '
          'Neither run lost progress. These are batch times, not per-task times; '
          'the comparison does not establish recovery after interruption.')


def episode(text=REPORT):
    return {'representation': 'episode', 'memory_kind': 'substantive_event',
            'evidence': text, 'recall_reason': 'Use the observed overhead when choosing checkpoint frequency.',
            'operation': 'assert', 'prior_claim_id': None, 'event_at_text': None}


def test_episode_preserves_complete_report_without_synthesized_fact_fields():
    proposal = episode()
    jsonschema.validate([proposal], claim_response_schema(REPORT)['schema'])
    record, = validated_claims(json.dumps([proposal]), message=REPORT, prior=[], observed_at=None)
    assert record['value'] == record['evidence'] == REPORT
    assert record['representation'] == 'episode'
    assert record['subject_key'].startswith('episode:')
    assert record['operation'] == 'assert' and record['event_at'] is None
    assert record['subject'] == 'Reported episode'


@pytest.mark.parametrize('update', [
    {'evidence': REPORT.replace('three seconds', 'nine seconds')},
    {'subject': 'all local processors'},
    {'memory_kind': 'preference'},
])
def test_episode_cannot_smuggle_a_rewritten_quote_or_other_memory_kind(update):
    assert validated_claims(json.dumps([episode() | update]), message=REPORT, prior=[], observed_at=None) == []


@pytest.mark.asyncio
async def test_reported_episode_keeps_its_representation_in_history_and_next_extraction(tmp_path):
    report = ('Mira reports that moving the microphone away from the fan made the words '
              'easier to hear in her single recording. I have not listened to it or '
              'measured noise; this is her comparison, not verified audio quality.')
    later = 'The microphone comparison needs another recording with the fan running.'
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    ledger.record_source('microphone-report', contact_id='contact-a', session_id='earlier',
        messages=[{'role': 'user', 'content': report}])
    assert await projection.process_one(Model({report: episode(report)}))
    with ledger._connect() as conn:
        claim_id = conn.execute('SELECT id FROM source_claims').fetchone()[0]
    ref, = ledger.source_references(['microphone-report'], contact_id='contact-a', session_id='later')
    history = source_read.read(ledger, contact_id='contact-a', session_id='later',
        source_id='microphone-report', source_version=ref['source_version'],
        view='assertions', claim_id=claim_id)
    retained, = json.loads(history['content'])['assertions']
    assert retained['representation'] == 'episode' and retained['evidence'] == report
    assert retained['event_time'] == {'status': 'unknown'}
    ledger.record_source('follow-up', contact_id='contact-a', session_id='later',
        messages=[{'role': 'user', 'content': later}])
    processor = Model({later: episode(later)})
    assert await projection.process_one(processor)
    prior, = processor.calls[0][0]['prior_assertions']
    assert prior['representation'] == 'episode' and prior['evidence'] == report
    assert prior['subject'] == 'Reported episode'


@pytest.mark.asyncio
async def test_ordinary_episode_reaches_deliberation_and_erasure_withdraws_it(source_app, perspective):
    state, _, _ = perspective
    ledger = state.ledger
    projection = SourceClaimProjection(ledger)
    judgments = state.judgments
    model = Model({REPORT: episode()})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, REPORT, 'checkpoint-episode')
        assert await projection.process_one(model)
    assert len(model.calls) == 1  # Exact whole-source reports need no second review.
    thinker = Processor()
    assert await judgments.process_one(thinker)
    assert len(thinker.requests) == 1
    premise, = thinker.requests[0]['evidence'][0]['admitted_premises']
    assert premise['representation'] == 'episode' and premise['evidence'] == REPORT
    assert len(judgments.revisions()) == 1
    reopened = SelfJudgments(TurnIdempotencyLedger(ledger.db_path), owner_id='contact-a')
    assert len(reopened.revisions()) == 1
    ledger.erase_sources(contact_id='contact-a', turn_ids=['checkpoint-episode'])
    assert reopened.revisions() == []
    with ledger._connect() as conn:
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 0
