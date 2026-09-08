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
    assert {r['status'] for r in view(state, history=True)['records']} == {'settled', 'current'}
    state.test_clock.value += module.APPRAISAL_LIFETIME + 1
    reopened = module.AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path), owner_id='owner', clock=lambda: state.test_clock.value)
    assert reopened.view('person', viewer_contact_id='owner')['records'] == []


@pytest.mark.asyncio
async def test_private_views_stay_private_preference_has_attribution_and_values_are_not_grants(state, monkeypatch):
    monkeypatch.setenv('COLONY_AGENT_VALUES', json.dumps(['Be candid', 'Respect promises']))
    source(state, 'incident', 'The export has failed again after the same retry.')
    processor = Processor(); await state.process_one(processor)
    assert processor.requests[0]['chosen_values'] == ['Be candid', 'Respect promises']
    assert state.view('person', viewer_contact_id='person')['records'] == []
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
