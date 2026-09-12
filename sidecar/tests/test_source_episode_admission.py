"""Whole reports avoid a second model while interpretations retain review."""
import json
from types import SimpleNamespace

import pytest

from pacomind.beliefs.source_claims import admission_metadata, extract_claims, validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from test_source_claim_projection import Model, claim
from test_source_episode_memory import REPORT, episode
from test_source_episode_revisions import CORRECTION, claims, context, record


@pytest.mark.asyncio
@pytest.mark.parametrize('padding', ['', '\n  '])
async def test_whole_report_commits_without_judging_endpoint_and_marker_is_honest(tmp_path, padding):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    text = padding + REPORT + padding
    ledger.record_source('report', contact_id='contact-a', session_id='first',
        messages=[{'role': 'user', 'content': text}])
    model = Model({text: episode(text)})
    original = model.complete
    async def no_review(*args, **kwargs):
        assert kwargs['context']['task'] == 'source_claim_extraction'
        return await original(*args, **kwargs)
    model.complete = no_review
    assert await projection.process_one(model)
    row, = claims(ledger)
    assert row['value'] == row['evidence'] == text
    assert row['source_admission']['basis'] == 'whole_source_quote_unverified'
    assert 'admission_review' not in row
    assert row['memory_quality']['basis'] == 'model_judgment_unverified'
    status, = projection.status('contact-a')
    assert status['status'] == 'complete' and status['attempts'] == 1
    assert status['diagnostics']['whole_source_episode_count'] == 1
    assert status['diagnostics']['reviewed_count'] == 0
    assert status['diagnostics']['last_review_provenance'] is None


@pytest.mark.asyncio
async def test_long_selected_excerpt_still_receives_context_review():
    text = REPORT + ' The following account is a fictional exercise.' + ' Extra context.' * 30
    model = Model({text: episode()})
    original = model.complete
    async def reject_excerpt(*args, **kwargs):
        response = await original(*args, **kwargs)
        if kwargs['context']['task'] == 'source_claim_review':
            return SimpleNamespace(content=json.dumps({'0': {'keep': False,
                'reason': 'The selected passage omits the fictional qualification.'}}), model_id='fixture-review')
        return response
    model.complete = reject_excerpt
    rows, _ = await extract_claims(model, {'occurred_at': None},
        {'role': 'user', 'content': text}, [])
    assert rows == [] and len(model.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate_report', [False, True])
async def test_mixed_batch_reviews_only_generated_interpretation_preserving_order(duplicate_report):
    text = 'I left the keys in Lake. The unlocked cabinet opened during the inspection.'
    report = episode(text)
    assertion = claim(text, 'Lake')
    model = Model({})
    original = model.complete
    async def mixed(*args, **kwargs):
        if kwargs['context']['task'] == 'source_claim_extraction':
            proposals = [report, assertion, report] if duplicate_report else [report, assertion]
            return SimpleNamespace(content=json.dumps(proposals), model_id='fixture-extraction')
        return await original(*args, **kwargs)
    model.complete = mixed
    rows, _ = await extract_claims(model, {'occurred_at': None}, {'role': 'user', 'content': text}, [])
    assert len(rows) == 2 and rows[0]['representation'] == 'episode'
    assert 'source_admission' in rows[0] and 'admission_review' not in rows[0]
    assert 'admission_review' in rows[1] and 'source_admission' not in rows[1]
    proposals = model.calls[0][0]['proposals']
    assert len(proposals) == 1 and proposals[0]['claim']['value'] == 'Lake'


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_two_event_whole_message_is_one_report_without_arbitrary_event_date(tmp_path, reverse):
    text = ('On 2026-08-24, the pressure sensor reset during the pump run. '
            'On 2026-08-27, the pressure sensor ran for four hours without resetting.')
    reports = [episode(text) | {'event_at_text': date} for date in ('2026-08-24', '2026-08-27')]
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    ledger.record_source('two-events', contact_id='contact-a', session_id='first',
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-09-10T12:00:00+00:00')
    model = Model({})
    async def two_events(*args, **kwargs):
        assert kwargs['context']['task'] == 'source_claim_extraction'
        return SimpleNamespace(content=json.dumps(reports[::-1] if reverse else reports),
            model_id='fixture-extraction')
    model.complete = two_events
    assert await projection.process_one(model)
    retained, = claims(ledger)
    assert retained['value'] == retained['evidence'] == text
    assert retained['event_at'] is None and retained['event_time'] == {'status': 'unknown'}
    assert retained['source_admission']['basis'] == 'whole_source_quote_unverified'
    status, = projection.status('contact-a')
    assert status['diagnostics']['coalesced_episode_count'] == 1
    assert status['diagnostics']['whole_source_episode_count'] == 1
    assert status['diagnostics']['reviewed_count'] == 0
    # Both dated questions retain the complete report without assigning it to
    # whichever event the extractor happened to list first.
    for date in ('2026-08-24', '2026-08-27'):
        selected, = context(projection, f'What pressure sensor incident happened on {date}?')
        assert selected['validity_status'] == 'query_time_unresolved'
        row, = json.loads(selected['content'])['assertions']
        assert row['quote'] == text and row['event_at'] is None


@pytest.mark.asyncio
async def test_contradictory_reports_remain_attributed_without_retracting_one(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    reports = ['Mira reports the relay ran for four hours without interruption. I have not verified her log.',
               'Leon reports the relay failed after two hours in that run. I have not verified his log.']
    for index, text in enumerate(reports):
        await record(ledger, projection, f'report-{index}', text, episode(text))
    rows = claims(ledger)
    assert len(rows) == 2 and all(row['operation'] == 'assert' and row['retracted_by'] is None for row in rows)
    retained = [json.loads(row['content']) for row in context(projection, 'relay run reports')]
    assert len(retained) == 2
    assert {row['quote'] for packet in retained for row in packet['assertions']} == set(reports)
    assert all(row['role'] == 'user' for packet in retained for row in packet['assertions'])


@pytest.mark.asyncio
async def test_unadmitted_predecessor_cannot_become_a_correction(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    await record(ledger, projection, 'original', REPORT, episode())
    old, = claims(ledger)
    old.pop('source_admission')
    proposed = episode(CORRECTION) | {'operation': 'correct', 'prior_claim_id': old['id']}
    assert validated_claims(json.dumps([proposed]), message=CORRECTION, prior=[old], observed_at=None) == []
    assert validated_claims(json.dumps([proposed]), message=CORRECTION, prior=[], observed_at=None) == []


@pytest.mark.asyncio
async def test_forged_whole_source_marker_cannot_bypass_commit_for_clipped_source(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'episode.db')
    projection = SourceClaimProjection(ledger)
    text = REPORT + ' This passage is fictional.'
    message = {'role': 'user', 'content': text}
    ledger.record_source('report', contact_id='contact-a', session_id='first', messages=[message])
    job = projection.claim_job()
    row, = validated_claims(json.dumps([episode()]), message=text, prior=[], observed_at=None)
    row['source_admission'] = {'version': 'source-episode-admission-v1', 'basis': 'whole_source_quote_unverified'}
    assert projection.commit(job, message, [row], model='fixture', lease_token=job['lease_token']) == 0
    assert claims(ledger) == []
    row['representation'] = 'assertion'
    assert admission_metadata(row) is None


@pytest.mark.parametrize('update', [
    {'memory_kind': 'routine_status'}, {'recall_reason': 'ok'},
    {'evidence': 'Invented report.'}, {'evidence': 'The API key is secret.'},
])
def test_existing_quality_and_quote_checks_remain_before_admission(update):
    assert validated_claims(json.dumps([episode() | update]), message=REPORT, prior=[], observed_at=None) == []
