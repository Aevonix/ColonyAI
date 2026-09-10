"""Actual source jobs and readers with controlled extraction/review responses."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.audio import source_text
from colony_sidecar.turns.idempotency import source_message_hash
from test_source_audio import message, retained
from test_source_claim_projection import claim, Model, prepared


def audio(text, *, prefix=None):
    original = message()
    original['content'][1]['segments'][0]['text'] = text
    if prefix:
        original['content'].insert(0, {'type': 'text', 'text': prefix})
    return original


def record(ledger, text, turn='audio', **kwargs):
    original = audio(text, **kwargs)
    ledger.record_source(turn, contact_id='person', session_id='voice', messages=[original],
                         occurred_at='2026-09-09T12:00:00+00:00')
    return original, source_text(retained(ledger, turn)['content'])


def claims(ledger):
    with sqlite3.connect(ledger.db_path) as conn:
        return [json.loads(r[0]) for r in conn.execute('SELECT data_json FROM source_claims')]


class AudioModel(Model):
    def __init__(self, outputs, *, keep=True, before_review=None):
        super().__init__(outputs)
        self.keep, self.before_review = keep, before_review

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        if kwargs['context']['task'] != 'source_claim_review':
            return await super().complete(messages, **kwargs)
        self.calls.append((deepcopy(payload), kwargs))
        if self.before_review:
            self.before_review()
        if isinstance(self.keep, Exception):
            raise self.keep
        return SimpleNamespace(model_id='fixture-review', content=json.dumps({str(p['index']): {
            'keep': self.keep, 'reason': 'Controlled scope decision on the supplied transcript.'}
            for p in payload['proposals']}))


@pytest.mark.asyncio
async def test_retained_audio_forms_reviewed_derived_claim_with_exact_original_lineage(tmp_path):
    from colony_sidecar.intelligence.graph.recall import pack_memory_context
    from colony_sidecar.turns.source_read import read
    from test_procedure_source_context import candidates
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    text = 'My office is in River.'
    original, rendered = record(ledger, text)
    projection = SourceClaimProjection(ledger)
    model = AudioModel({rendered: claim(text, 'River')})
    assert await projection.process_one(model)
    rows = claims(ledger)
    assert len(rows) == 1  # The predecessor silently completed this job with zero claims.
    row = rows[0]
    assert row['evidence'] == text and row['value'] == 'River'
    assert rendered[row['span_start']:row['span_end']] == text
    basis = row['evidence_basis']; segment = basis['segment']
    assert basis['source_message_hash'] == source_message_hash('voice', original)
    assert basis['epistemic_state'] == row['epistemic_state'] == 'derived_unverified'
    assert segment['asset_id'] == retained(ledger)['content'][0]['asset_id']
    assert segment['segment_index'] == 0 and segment['block_index'] == 1
    assert segment['evidence_start'] == 0 and segment['evidence_end'] == len(text)
    assert segment['start_ms'] == 0 and segment['end_ms'] == 100
    assert segment['captured_at'] is None and segment['confidence'] is None
    assert segment['recognizer']['model_id'] == 'fixture-asr'
    assert row['event_at'] is None and row['event_time']['status'] == 'unknown'
    assert row['admission_review']['model_provenance']['model_id'] == 'fixture-review'
    assert len(model.calls) == 2 and model.calls[0][0]['source_evidence'] == model.calls[1][0]['source_evidence']
    schema = model.calls[0][1]['context']['response_schema']['schema']
    assert schema['items']['anyOf'][0]['properties']['evidence']['enum'] == [text]
    packet = prepared(projection, contact='person')[0]['assertions'][0]
    assert packet['evidence_basis'] == basis and packet['reported_at'] != packet['event_at']
    # The injected reader packet must retain lineage, not merely the stored row.
    selected, body = pack_memory_context(candidates(projection, query='office'))
    assert any(r.get('content_format') == 'source_assertions_v1' for r in selected)
    entries = [json.JSONDecoder().raw_decode(line[2:])[0]
               for line in body.splitlines() if line.startswith('- ')]
    card, = [e for e in entries if isinstance(e.get('content'), dict)]
    recalled, = card['content']['assertions']
    passage, = [e for e in entries if e.get('evidence_ref') == recalled['evidence_ref']]
    assert recalled['evidence_basis'] == basis
    assert card['state'] == recalled['epistemic_state'] == 'derived_unverified'
    assert card['source_modality'] == recalled['source_modality'] == 'audio_transcript'
    assert recalled['event_at'] is None and recalled['event_time']['status'] == 'unknown'
    assert passage['quote'] == text and passage['source_message_hash'] == basis['source_message_hash']
    assert card['history_anchor'] == {'source_id': 'audio', 'claim_id': packet['claim_id']}
    refs = ledger.source_references(['audio'], contact_id='person', session_id='later')
    assert basis['source_version_at_formation'] == refs[0]['source_version']
    opened = read(ledger, contact_id='person', session_id='later', **refs[0],
                  view='assertions', claim_id=packet['claim_id'])
    assert json.loads(opened['content'])['assertions'][0]['evidence_basis'] == basis
    ledger.erase_sources(contact_id='person', turn_ids=['audio'])
    assert claims(ledger) == [] and not prepared(projection, contact='person')


@pytest.mark.asyncio
async def test_audio_procedure_keeps_unclaimed_condition_and_exact_segment_basis(tmp_path):
    from colony_sidecar.intelligence.graph.recall import pack_memory_context
    from test_procedure_source_context import candidates
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    step = 'For the pump inspection, record the inlet reading.'
    condition = 'Only inspect the pump when its motor is disconnected.'
    original = audio(step)
    segments = original['content'][1]['segments']
    segments[0]['end_ms'] = 40
    segments.append({'start_ms': 50, 'end_ms': 100, 'text': condition})
    ledger.record_source('pump-audio', contact_id='person', session_id='voice', messages=[original])
    rendered = source_text(retained(ledger, 'pump-audio')['content'])
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(AudioModel({rendered: claim(step, 'record the inlet reading',
        subject='pump inspection', predicate='inspection steps', memory_kind='procedure')}))
    stored, = claims(ledger)
    row, = candidates(projection, query='pump inspection')
    assert row['procedure_context'] == 'complete_source_message_text'
    assert row['content'] == rendered and step in rendered and condition in rendered
    selected, body = pack_memory_context([row])
    assert selected == [row]
    line, = [line[2:] for line in body.splitlines() if line.startswith('- ')]
    metadata, end = json.JSONDecoder().raw_decode(line)
    assert json.loads(line[end:].strip()) == rendered
    assert metadata['state'] == 'derived_unverified' and metadata['source_modality'] == 'audio_transcript'
    basis, = metadata['source_evidence_bases']
    assert basis == {'claim_id': row['history_anchor']['claim_id'], 'evidence_basis': stored['evidence_basis']}
    segment = basis['evidence_basis']['segment']
    assert segment['segment_index'] == 0 and segment['start_ms'] == 0 and segment['end_ms'] == 40
    assert segment['evidence_start'] == 0 and segment['evidence_end'] == len(step)
    assert segment['recognizer']['model_id'] == 'fixture-asr'
    assert basis['evidence_basis']['source_message_hash'] == row['source_message_hash']
    # A budget notice opens the complete source, not the extracted step alone.
    bounded, short = pack_memory_context([row], max_chars=len(body)-1)
    notice, = bounded
    assert notice['procedure_context'] == 'full_source_required'
    assert 'source_evidence_bases' not in notice
    assert step not in short and condition not in short
    assert notice['history_anchor'] == row['history_anchor']
    assert notice['source_anchors'] == [{'source_id': 'pump-audio'}]
    assert row['source_evidence_bases'] == metadata['source_evidence_bases']


@pytest.mark.asyncio
@pytest.mark.parametrize('outside', ['label', 'surrounding_text'])
async def test_audio_metadata_and_other_blocks_cannot_become_asr_claims(tmp_path, outside):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    prefix = 'The narrator says my office is in River in this fictional scene.'
    text = 'My office is in Lake.'
    _, rendered = record(ledger, text, prefix=prefix)
    evidence = rendered if outside == 'label' else prefix
    model = AudioModel({rendered: claim(evidence, 'River' if outside == 'surrounding_text' else 'Lake')})
    projection = SourceClaimProjection(ledger)
    await projection.process_one(model)
    assert len(model.calls) == 1 and not claims(ledger)
    assert prefix in model.calls[0][0]['message']  # Narrative scope is not discarded.
    assert projection.status('person')[0]['diagnostics']['rejection_counts']['audio_segment_grounding'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('keep,status,calls_count', [(False, 'complete', 2), (RuntimeError('unavailable'), 'pending', 2)])
async def test_audio_admission_rejection_and_unavailable_review_are_distinct(tmp_path, keep, status, calls_count):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    text = 'My office is in River.'; _, rendered = record(ledger, text)
    model = AudioModel({rendered: claim(text, 'River')}, keep=keep)
    projection = SourceClaimProjection(ledger)
    await projection.process_one(model)
    assert not claims(ledger) and len(model.calls) == calls_count
    assert projection.status('person')[0]['status'] == status
    assert ledger.search_sources('River', contact_id='person', session_id='later')


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['erase', 'reclaim', 'invalidated', 'unlinked'])
async def test_audio_review_cannot_commit_erased_invalidated_or_reclaimed_source(tmp_path, race):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    text = 'My office is in River.'; _, rendered = record(ledger, text)
    projection = SourceClaimProjection(ledger)
    def mutate():
        if race == 'erase':
            ledger.erase_sources(contact_id='person', turn_ids=['audio'])
        else:
            with sqlite3.connect(ledger.db_path) as conn:
                if race == 'reclaim':
                    conn.execute('UPDATE source_claim_jobs SET lease_until=0')
                elif race == 'unlinked':
                    conn.execute("DELETE FROM source_media_links WHERE turn_id='audio'")
                else:
                    conn.execute("INSERT INTO source_attribution_invalidations VALUES ('audio','fixture-review',0)")
            if race == 'reclaim':
                assert SourceClaimProjection(ledger).claim_job()
    await projection.process_one(AudioModel({rendered: claim(text, 'River')}, before_review=mutate))
    assert not claims(ledger)


@pytest.mark.asyncio
async def test_asr_corrected_by_text_keeps_spans_and_does_not_revive_erased_correction(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    old = 'My office is in River.'; _, rendered = record(ledger, old)
    await projection.process_one(AudioModel({rendered: claim(old, 'River')}))
    new = 'Correction: My office is in Lake, not River.'
    ledger.record_source('corrected', contact_id='person', session_id='text', messages=[{'role': 'user', 'content': new}])
    model = Model({new: claim(new, 'Lake', operation='correct', match_prior=True)}, model='other-processor')
    await projection.process_one(model)
    assert model.calls[0][0]['prior_assertions'][0]['evidence_basis']['epistemic_state'] == 'derived_unverified'
    assert 'source_evidence' not in model.calls[0][0]
    packet = prepared(projection, contact='person')[0]
    assert [c['value'] for c in packet['assertions']] == ['Lake']
    assert packet['assertions'][0]['event_at'] is None
    assert 'evidence_basis' not in packet['assertions'][0]
    ledger.erase_sources(contact_id='person', turn_ids=['corrected'])
    assert not prepared(projection, contact='person')


@pytest.mark.asyncio
async def test_asr_annotation_matches_original_revision_and_fences_recalled_claim(tmp_path):
    from colony_sidecar.turns.source_annotations import expand, current_candidates
    from colony_sidecar.intelligence.graph.recall import source_candidates
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'My office is in River.'; _, rendered = record(ledger, text)
    await projection.process_one(AudioModel({rendered: claim(text, 'River', memory_kind='preference')}))
    ref = ledger.source_references(['audio'], contact_id='person', session_id='later')[0]
    result = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='asr-fix',
        **ref, excerpt='River', correction='The recognizer heard River; the speaker corrects it to Lake.', author_principal='fixture-owner')
    scope = {'contact_id': 'person', 'session_id': 'later'}
    candidates = source_candidates(ledger.search_sources('River', **scope))
    packet = current_candidates(ledger, expand(ledger, candidates, **scope), **scope)
    assert 'speaker corrects it to Lake' in json.dumps(packet)
    assert projection.preferences('person', 'later') == []
    ledger.erase_sources(contact_id='person', turn_ids=[result['source_id']])
    candidates = source_candidates(ledger.search_sources('River', **scope))
    assert current_candidates(ledger, expand(ledger, candidates, **scope), **scope) == []


@pytest.mark.asyncio
async def test_only_transcript_preference_read_retains_provenance_without_affect_or_judgment(tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_SELF_JUDGMENTS_ENABLED', '1')
    from colony_sidecar.self_model.appraisals import AppraisalStore
    from colony_sidecar.self_model.judgments import SelfJudgments
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'I prefer jasmine tea without sugar.'; _, rendered = record(ledger, text)
    await projection.process_one(AudioModel({rendered: claim(text, 'jasmine tea without sugar',
        predicate='tea_preference', memory_kind='preference')}))
    appraisals = AppraisalStore(ledger, owner_id='person')
    row = appraisals.view('person', viewer_contact_id='person')['records'][0]
    assert row['epistemic_state'] == 'derived_unverified' and row['evidence_basis']['segment']['recognizer']
    class NoEmotionalInference:
        supports_function_routing = True
        def function_deadline_seconds(self, **kwargs): return 5
        async def complete(self, **kwargs): pytest.fail('ASR became ordinary emotional or self-judgment evidence')
    await appraisals.process_one(NoEmotionalInference())
    await SelfJudgments(ledger, owner_id='person').process_one(NoEmotionalInference())
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM appraisal_records').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM self_judgment_revisions').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_commit_rebuilds_asr_view_instead_of_trusting_preserved_original_hash(tmp_path):
    from colony_sidecar.turns.audio import claim_message
    from colony_sidecar.beliefs.source_claims import extract_claims
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'My office is in River.'; record(ledger, text)
    job = projection.claim_job(); view = claim_message(retained(ledger))
    forged = deepcopy(view); forged['content'] = forged['content'].replace('River', 'Lake!')
    rows, model = await extract_claims(AudioModel({forged['content']: claim(text.replace('River', 'Lake!'), 'Lake!')}),
        job, forged, [])
    assert rows and projection.commit(job, forged, rows, model=model, lease_token=job['lease_token']) == 0
    rows, model = await extract_claims(AudioModel({view['content']: claim(text, 'River')}), job, view, [])
    rows[0].pop('admission_review')
    assert projection.commit(job, view, rows, model=model, lease_token=job['lease_token']) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('captured,required,expected', [
    (None, False, 'unresolved'), ('2026-09-07T08:00:00+00:00', False, 'resolved'),
    (None, True, 'rejected')])
async def test_asr_relative_event_dates_use_capture_not_receipt_and_keep_required_conditions(tmp_path, captured, required, expected):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'Today I spotted a parcel.' if not required else 'Starting Today my office is in River.'
    original = audio(text); original['content'][1]['captured_at'] = captured
    ledger.record_source('audio', contact_id='person', session_id='voice', messages=[original],
                         occurred_at='2026-09-09T12:00:00+00:00')
    rendered = source_text(retained(ledger)['content'])
    output = claim(text, 'River' if required else 'parcel',
                   **({'valid_from_text': 'Today'} if required else {'event_at_text': 'Today'}))
    model = AudioModel({rendered: output})
    await projection.process_one(model)
    rows = claims(ledger)
    assert model.calls[0][0]['source_evidence']['relative_date_anchor'] == captured
    if required:
        assert rows == [] and len(model.calls) == 1
        assert projection.status('person')[0]['diagnostics']['rejection_counts']['invalid_date'] == 1
    else:
        assert len(rows) == 1
        if captured:
            assert rows[0]['event_at'].startswith('2026-09-07')
        else:
            assert rows[0]['event_at'] is None and rows[0]['event_time']['status'] == expected


@pytest.mark.asyncio
async def test_exact_second_segment_offsets_shared_asset_sources_and_current_revision(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'My office is in River.'
    original = audio('The earlier remark is unrelated.')
    segments = original['content'][1]['segments']
    segments[0]['end_ms'] = 40
    segments.append({'start_ms': 50, 'end_ms': 100, 'text': text})
    ledger.record_source('audio', contact_id='person', session_id='voice', messages=[original])
    rendered = source_text(retained(ledger)['content'])
    await projection.process_one(AudioModel({rendered: claim(text, 'River')}))
    row = claims(ledger)[0]
    assert row['evidence_basis']['segment']['segment_index'] == 1
    assert rendered[row['span_start']:row['span_end']] == text
    other = deepcopy(original); other['content'][1]['segments'][1]['text'] = 'My office is in Lake.'
    ledger.record_source('other', contact_id='other', session_id='voice', messages=[other])
    other_rendered = source_text(retained(ledger, 'other')['content'])
    await projection.process_one(AudioModel({other_rendered: claim('My office is in Lake.', 'Lake')}))
    assert prepared(projection, contact='person')[0]['assertions'][0]['value'] == 'River'
    assert prepared(projection, contact='other')[0]['assertions'][0]['value'] == 'Lake'
    ledger.erase_sources(contact_id='person', turn_ids=['audio'])
    assert not prepared(projection, contact='person')
    assert prepared(projection, contact='other')[0]['assertions'][0]['value'] == 'Lake'


@pytest.mark.asyncio
async def test_repeated_transcript_words_bind_to_audio_not_identical_adjacent_text(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db'); projection = SourceClaimProjection(ledger)
    text = 'My office is in River.'
    _, rendered = record(ledger, text, prefix=text)
    await projection.process_one(AudioModel({rendered: claim(text, 'River')}))
    row = claims(ledger)[0]
    assert row['span_start'] == rendered.rindex(text) and row['span_start'] > 0
    assert row['evidence_basis']['segment']['evidence_start'] == 0
