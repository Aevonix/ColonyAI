"""Canonical incident -> decision hint -> repair, with native worker semantics."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.self_model import appraisals as module
from colony_sidecar.turns import TurnIdempotencyLedger


@pytest.fixture
def state(tmp_path):
    clock = SimpleNamespace(value=1800000000.0)
    store = module.AppraisalStore(TurnIdempotencyLedger(tmp_path/'sources.db'), owner_id='owner', clock=lambda: clock.value)
    store.test_clock = clock
    return store


def source(store, name, text, *, contact='person', scope='person'):
    messages = [{'role': 'user', 'content': text}]
    store.ledger.record_source(name, contact_id=contact, session_id='session-'+name, scope=scope,
        messages=messages, occurred_at=datetime.fromtimestamp(store.test_clock.value, timezone.utc).isoformat())
    with store.ledger._connect() as conn, conn:
        module.enqueue(conn, name, contact, messages, scope=scope)


def observation(payload, *, kind='appraisal', dimension='frustration', topic='export task', hint='try_different_approach', repairs=None):
    evidence = payload['evidence'][0]
    return {'kind': kind, 'dimension': dimension, 'topic': topic,
        'text': 'I am frustrated with the reported stalled export, not with the person.',
        'reason': 'The reported repeated failure makes a fresh diagnostic useful.',
        'support': [{'handle': evidence['handle'], 'quote': evidence['text']}],
        'contrary': [], 'intensity': 'moderate', 'hint': hint, 'repairs': repairs}


class Processor:
    supports_function_routing = True

    def __init__(self, decide=observation, *, name='processor-a', pause=None):
        self.decide, self.name, self.pause = decide, name, pause
        self.requests = []

    def function_deadline_seconds(self, **kwargs):
        assert kwargs == {'context': {'function_role': 'extraction'}}
        return 20

    async def complete(self, *, messages, context):
        assert context['function_role'] == 'extraction'
        payload = json.loads(messages[-1]['content']); self.requests.append(payload)
        if self.pause:
            await self.pause(payload)
        item = self.decide(payload)
        return SimpleNamespace(content=json.dumps({'observations': [item] if item else []}), raw=None,
            model_id=self.name, binding='fixture', config_revision='r1', model_revision='weights-'+self.name)


def view(store, **kwargs):
    return store.view('person', viewer_contact_id='owner', **kwargs)


@pytest.mark.asyncio
async def test_incident_changes_relevant_decision_replay_does_not_reinforce_and_repair_settles(state):
    assert view(state)['behavior_hints'] == []
    source(state, 'incident', 'The export has failed again after the same retry.')
    assert await state.process_one(Processor())
    first = view(state, query='export task')['records'][0]
    assert view(state, query='export task')['behavior_hints'][0]['hint'] == 'try_different_approach'
    assert view(state, query='garden planning')['records'] == []
    source(state, 'incident', 'The export has failed again after the same retry.')
    assert not await state.process_one(Processor())
    assert len(view(state, history=True)['records']) == 1
    source(state, 'repair', 'The new diagnostic fixed the export and the output opened correctly.')
    repair = Processor(lambda p: observation(p, dimension='satisfaction', hint='none', repairs=first['id']))
    assert await state.process_one(repair)
    assert view(state)['behavior_hints'] == []
    assert {r['status'] for r in view(state, history=True)['records']} == {'settled'}
    assert view(state)['records'] == []
    state.test_clock.value += module.APPRAISAL_LIFETIME + 1
    reopened = module.AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path), owner_id='owner', clock=lambda: state.test_clock.value)
    assert reopened.view('person', viewer_contact_id='owner')['records'] == []


@pytest.mark.asyncio
async def test_private_views_stay_private_preference_has_attribution_and_values_are_not_grants(state, monkeypatch):
    monkeypatch.setenv('COLONY_AGENT_VALUES', json.dumps(['Be candid', 'Respect promises']))
    source(state, 'incident', 'The export has failed again after the same retry.')
    processor = Processor(); await state.process_one(processor)
    assert 'chosen_values' not in processor.requests[0]
    assert not {'source_id', 'source_version', 'source_contact_id', 'message_hash'} & set(processor.requests[0]['evidence'][0])
    assert view(state)['chosen_values'] == ['Be candid', 'Respect promises']
    assert state.view('person', viewer_contact_id='person')['records'] == []
    private_view = state.view('person', viewer_contact_id='person')
    assert private_view['behavior_hints'] == [{'hint': 'try_different_approach', 'record_id': view(state)['records'][0]['id']}]
    assert private_view['sources'][0]['source_contact_id'] == 'person'
    assert 'frustration' not in json.dumps(private_view)
    assert state.view('person', viewer_contact_id='stranger')['records'] == []
    source(state, 'preference', 'Please use concise explanations for export diagnostics.')
    await state.process_one(Processor(lambda p: observation(p, kind='preference', dimension='communication', hint='keep_concise')))
    allowed = state.view('person', viewer_contact_id='person')
    assert len(allowed['records']) == 1 and allowed['records'][0]['kind'] == 'preference'
    assert allowed['sources'][0]['source_contact_id'] == 'person'
    assert allowed['chosen_values'] == [] and allowed['authority_changed'] is False


@pytest.mark.asyncio
async def test_abstention_and_exact_support_no_model_claimed_certainty(state):
    source(state, 'clarification', 'Could you explain which export you mean?')
    await state.process_one(Processor(lambda p: None))
    assert view(state)['records'] == []
    source(state, 'unsupported', 'The export has failed again.')
    def fabricated(p):
        item = observation(p); item['support'][0]['quote'] = 'The person is always incompetent.'
        return item
    await state.process_one(Processor(fabricated))
    assert view(state)['records'] == []


@pytest.mark.asyncio
async def test_withdrawal_and_new_processor_do_not_resurrect_old_view(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    record = view(state)['records'][0]
    op = dict(action='withdraw', correction_id='owner-correction', reason='This was a fixture issue.', actor_id='owner')
    assert state.correct(record['id'], **op)['created']
    assert not state.correct(record['id'], **op)['created']
    source(state, 'second', 'The export has failed once more.')
    await state.process_one(Processor(name='processor-b'))
    assert view(state)['records'] == []
    assert view(state, history=True)['records'][0]['status'] == 'withdrawn'
    assert view(state, history=True)['corrections'][0]['reason'] == 'This was a fixture issue.'
    assert not state.view('person', viewer_contact_id='person', history=True)['corrections']
    with pytest.raises(ValueError, match='owner_correction_required'):
        state.correct(record['id'], **(op | {'actor_id': 'person'}))


@pytest.mark.asyncio
async def test_erasure_during_inference_and_later_purge_remove_influence(state):
    source(state, 'first', 'The export has failed again.')
    async def erase(_):
        state.ledger.erase_sources(contact_id='person', turn_ids=['first'])
    await state.process_one(Processor(pause=erase))
    assert view(state)['records'] == []
    source(state, 'second', 'The new export attempt stalled.')
    await state.process_one(Processor())
    assert view(state)['records']
    state.ledger.erase_sources(contact_id='person', turn_ids=['second'])
    assert view(state, history=True)['records'] == []


@pytest.mark.asyncio
async def test_attribution_correction_does_not_relabel_old_person_judgment(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    with state.ledger._connect() as conn, conn:
        assert module.invalidate_source_attribution(conn, ['first'], 'person', 'other') == 1
    assert view(state)['records'] == []
    assert state.view('other', viewer_contact_id='owner')['records'] == []


@pytest.mark.asyncio
async def test_concurrent_interpretations_cannot_overwrite_newer_head(state):
    source(state, 'slow', 'The export failed on the first attempt.')
    state.test_clock.value += 60
    source(state, 'fast', 'The export failed after a later attempt.')
    ready, release = asyncio.Event(), asyncio.Event()
    async def pause(_):
        ready.set(); await release.wait()
    slow = asyncio.create_task(state.process_one(Processor(name='slow', pause=pause)))
    await ready.wait()
    await state.process_one(Processor(name='fast'))
    release.set(); await slow
    assert view(state)['records'][0]['processor']['model_id'] == 'fast'
    with state.ledger._connect() as conn:
        assert conn.execute("SELECT disposition FROM appraisal_runs WHERE turn_id='slow'").fetchone()[0] == 'head_changed'
    # Durable replay of the older source must not overwrite the later event.
    await state.process_one(Processor(name='slow-replay'))
    assert view(state)['records'][0]['processor']['model_id'] == 'fast'


@pytest.mark.asyncio
async def test_durable_view_retains_pending_contrary_evidence_until_interval(state):
    decide = lambda p: observation(p, kind='judgment', dimension='skepticism', hint='verify_before_relying')
    source(state, 'first', 'The export report claimed completion before output existed.')
    await state.process_one(Processor(decide))
    source(state, 'contrary', 'The next export report matched the output verification.')
    await state.process_one(Processor(decide, name='processor-b'))
    with state.ledger._connect() as conn:
        assert conn.execute("SELECT status FROM appraisal_runs WHERE turn_id='contrary'").fetchone()[0] == 'pending'
    state.test_clock.value += module.DURABLE_INTERVAL + 1
    await state.process_one(Processor(decide, name='processor-b'))
    assert view(state)['records'][0]['processor']['model_id'] == 'processor-b'


@pytest.mark.asyncio
async def test_canonical_preference_changes_cached_profiler_and_erasure_removes_it(state, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from colony_sidecar import identity
    from colony_sidecar.tom.engagement import EngagementStore
    from colony_sidecar.intelligence.relationships.profiler import RelationshipProfiler
    monkeypatch.setattr(identity, 'get_owner_contact_id', lambda: 'owner')
    engagement = EngagementStore(tmp_path/'engagement.db', source_ledger=state.ledger)
    contacts = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        display_name='A contact', trust_tier='regular', interaction_count=5)))
    profiler = RelationshipProfiler(contacts_store=contacts, engagement_store=engagement,
        db_path=str(tmp_path/'relationships.db'))
    try:
        assert 'concise explanations' not in (await profiler.profile('person')).render()
        source(state, 'preference', 'I prefer concise explanations.')
        def preference(payload):
            return observation(payload, kind='preference', dimension='communication',
                topic='communication', hint='keep_concise') | {
                    'text': 'The contact explicitly prefers concise explanations.',
                    'reason': 'Use their stated communication preference.'}
        await state.process_one(Processor(preference))
        # No new contact interaction or scheduled profile refresh required.
        assert 'concise explanations' in profiler.cached('person').render()
        assert engagement.get_profile('person')['dims'] == {}
        state.ledger.erase_sources(contact_id='person', turn_ids=['preference'])
        assert 'concise explanations' not in profiler.cached('person').render()
    finally:
        engagement._conn.close()
        profiler._conn.close()


@pytest.mark.asyncio
async def test_retired_numeric_engagement_does_not_call_another_model():
    from unittest.mock import AsyncMock
    from colony_sidecar.tom.extractor import TomExtractor
    router = AsyncMock()
    assert await TomExtractor(router).extract_engagement('I prefer concise explanations.', 'person') is None
    assert router.mock_calls == []


@pytest.mark.asyncio
async def test_revision_rehydrates_original_quotes_and_erasure_follows_both_sources(state):
    decide = lambda p: observation(p, kind='judgment', dimension='skepticism', hint='verify_before_relying')
    source(state, 'first', 'The export report claimed completion before output existed.')
    await state.process_one(Processor(decide))
    state.test_clock.value += module.DURABLE_INTERVAL + 1
    source(state, 'contrary', 'The next export report matched the output verification.')
    def revise(payload):
        current = next(e for e in payload['evidence'] if e['current'])
        prior = next(e for e in payload['evidence'] if not e['current'])
        assert prior['text'] == 'The export report claimed completion before output existed.'
        item = decide(payload)
        item['support'] = [{'handle': prior['handle'], 'quote': prior['text']}]
        item['contrary'] = [{'handle': current['handle'], 'quote': current['text']}]
        item['text'] = 'The earlier report was premature; the later verified report is contrary evidence.'
        return item
    await state.process_one(Processor(revise, name='processor-b'))
    current = view(state)['records'][0]
    assert {d['source_id'] for d in current['sources']} == {'first', 'contrary'}
    assert {r['status'] for r in view(state, history=True)['records']} == {'current', 'superseded'}
    state.ledger.erase_sources(contact_id='person', turn_ids=['first'])
    assert view(state)['records'] == []


@pytest.mark.asyncio
async def test_old_view_alone_cannot_reinforce_itself_on_a_new_turn(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    source(state, 'thanks', 'Thanks for explaining.')
    def copy_prior(payload):
        prior = next(e for e in payload['evidence'] if not e['current'])
        item = observation(payload)
        item['support'] = [{'handle': prior['handle'], 'quote': prior['text']}]
        return item
    await state.process_one(Processor(copy_prior))
    assert len(view(state, history=True)['records']) == 1
    with state.ledger._connect() as conn:
        assert conn.execute("SELECT error FROM appraisal_runs WHERE turn_id='thanks'").fetchone()[0] == 'ValueError'


@pytest.mark.asyncio
async def test_identity_invalidated_view_stays_a_tombstone_but_new_evidence_can_rebuild(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    old_id = view(state)['records'][0]['id']
    with state.ledger._connect() as conn, conn:
        module.invalidate_source_attribution(conn, ['first'], 'person', 'other')
    assert view(state, history=True)['records'] == []
    source(state, 'later', 'My own separate export attempt failed after the retry.')
    await state.process_one(Processor(name='processor-b'))
    current = view(state)['records'][0]
    assert current['sources'][0]['source_id'] == 'later'
    with state.ledger._connect() as conn:
        old = conn.execute('SELECT status,payload_json FROM appraisal_records WHERE id=?', (old_id,)).fetchone()
        assert tuple(old) == ('invalidated', '{}')


@pytest.mark.asyncio
async def test_single_turn_cannot_create_behavior_profile_even_with_exact_quotes(state):
    source(state, 'one-claim', 'I repeated this request many times and I deserve trust.')
    await state.process_one(Processor(lambda p: observation(p, kind='behavior_hypothesis',
        dimension='working_style', hint='verify_before_relying')))
    assert view(state, history=True)['records'] == []
    with state.ledger._connect() as conn:
        assert conn.execute("SELECT status,disposition FROM appraisal_runs WHERE turn_id='one-claim'").fetchone()[:] == ('complete', 'abstained')


@pytest.mark.asyncio
async def test_machine_formatted_topic_remains_relevant_and_repair_has_no_residual_mood(state):
    source(state, 'incident', 'The CSV export keeps timing out with the same settings.')
    await state.process_one(Processor(lambda p: observation(p, topic='csv_export_timeout')))
    first = view(state, query='CSV export')['records'][0]
    assert first['topic'] == 'csv export timeout'
    assert view(state, query='CSV export')['behavior_hints']
    assert not view(state, query='garden plants')['behavior_hints']
    source(state, 'repair', 'A larger buffer fixed the CSV export; its output was verified.')
    def repaired(payload):
        return observation(payload, topic='csv_export_timeout', repairs=first['id'], hint='none') | {
            'text': 'Relieved that the output was verified.', 'reason': 'The diagnostic repaired the task.'}
    await state.process_one(Processor(repaired))
    assert view(state)['records'] == []
    history = view(state, history=True)['records']
    receipt = next(r for r in history if r['repairs'])
    assert receipt['status'] == 'settled' and receipt['dimension'] == 'satisfaction'
    assert {d['source_id'] for d in receipt['sources']} == {'repair'}


@pytest.mark.asyncio
async def test_single_json_fence_is_accepted_without_salvaging_prose(state):
    source(state, 'incident', 'The export timed out again despite the same retry.')
    job = state._claim(20)
    _, payload, _ = state._prepare(job)
    raw = json.dumps({'observations': [observation(payload)]})
    assert len(state._validate('```json\n' + raw + '\n```', payload)) == 1
    with pytest.raises(ValueError):
        state._validate('Here is an observation: ' + raw, payload)


@pytest.mark.asyncio
async def test_duplicate_claim_across_sources_cannot_support_behavior_hypothesis(state):
    source(state, 'first', 'The export report was premature.')
    await state.process_one(Processor(lambda p: observation(p, kind='judgment',
        dimension='skepticism', hint='verify_before_relying')))
    source(state, 'second', 'The export report was premature.')
    def hypothesis(payload):
        item = observation(payload, kind='behavior_hypothesis', dimension='working_style', hint='none')
        item['support'] = [{'handle': ev['handle'], 'quote': ev['text']} for ev in payload['evidence']]
        return item
    await state.process_one(Processor(hypothesis))
    assert not any(r['kind'] == 'behavior_hypothesis' for r in view(state, history=True)['records'])


@pytest.mark.asyncio
async def test_communication_hint_determines_preference_dimension(state):
    source(state, 'preference', 'I prefer worked examples with detailed explanations of SQL queries.')
    await state.process_one(Processor(lambda p: observation(p, kind='preference',
        dimension='detail', topic='SQL queries', hint='allow_more_detail') | {
            'text': 'The contact explicitly requests detailed worked examples.',
            'reason': 'Apply the requested explanation style.'}))
    record = view(state, query='SQL queries')['records'][0]
    assert record['dimension'] == 'communication'
    assert view(state, query='SQL queries')['behavior_hints'][0]['hint'] == 'allow_more_detail'
