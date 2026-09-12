"""Scoped complete-source opening, stable paging, and honest oversized conflicts."""
from datetime import datetime, timezone
import hashlib
import json

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.middleware import ApiKeyMiddleware
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.beliefs.source_time import interpret_time_query
from pacomind.memory.recall import pack_memory_context
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.source_read import read
from test_scoped_api_authority import _principal, _write_keyring
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


def ref(ledger, identifier='source', **kwargs):
    return ledger.source_references([identifier], contact_id='person', session_id='later', **kwargs)[0]


def opened(ledger, identifier='source', **kwargs):
    return read(ledger, contact_id='person', session_id='later', **ref(ledger, identifier), **kwargs)


@pytest.mark.parametrize('reported_at', [None, '2024-02-03T04:05:06+00:00'])
def test_long_procedure_opens_every_step_and_scope_or_revision_never_widens(tmp_path, reported_at):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    text = 'Pump procedure: isolate pressure. ' + 'Check the seal; ' * 700 + 'Only then reconnect power.'
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'user', 'content': text}], occurred_at=reported_at)
    with ledger._connect() as conn:
        row = conn.execute("SELECT ingested_at,messages_json FROM turn_sources WHERE turn_id='source'").fetchone()
    recorded_at = row['ingested_at']
    original = json.dumps({'messages': json.loads(row['messages_json']),
        'reported_at': reported_at, 'recorded_at': recorded_at,
        'event_time': 'unknown unless explicitly stated in each source'}, ensure_ascii=False)
    first = opened(ledger)
    assert not first['complete'] and len(first['content']) == 4096
    assert 'reported_at' not in first['content']
    assert first['read_revision'] == hashlib.sha256(original.encode()).hexdigest()
    pages, page = [first['content']], first
    while True:
        assert page['reported_at'] == reported_at and page['recorded_at'] == recorded_at
        assert page['evidence_basis'] == 'retained_record'
        if page['complete']:
            break
        page = opened(ledger, offset=page['next_offset'], read_revision=page['read_revision'])
        pages.append(page['content'])
    assert ''.join(pages) == original
    assert opened(ledger) == first
    assert json.loads(''.join(pages))['messages'] == [{'role': 'user', 'content': text}]
    assert first['source_refs'] == [ref(ledger)]
    for scope in ({'contact_id': 'other', 'session_id': 'later'},):
        with pytest.raises(ValueError, match='unavailable'):
            read(ledger, **scope, **ref(ledger))
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', source_id='source', source_version='0'*64)
    saved = ref(ledger)
    ledger.erase_sources(contact_id='person', turn_ids=['source'])
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **saved)


def test_checkpoint_opening_stays_in_its_session_and_correction_changes_continuation(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    ledger.record_source('private-session', contact_id='person', session_id='original', scope='session',
                         messages=[{'role': 'user', 'content': 'Session-scoped appendix.'}])
    private_ref = ledger.source_references(['private-session'], contact_id='person', session_id='original')[0]
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **private_ref)
    text = 'Archive digest matched. ' + 'Recorded details. ' * 500
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'assistant', 'content': text}])
    first = opened(ledger)
    correction = 'The digest was not compared; that claim is unsupported.'
    ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='note',
        **ref(ledger), excerpt='Archive digest matched.', correction=correction, author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, offset=first['next_offset'], read_revision=first['read_revision'])
    content, page = '', opened(ledger)
    while True:
        content += page['content']
        if page['complete']:
            break
        page = opened(ledger, offset=page['next_offset'], read_revision=page['read_revision'])
    assert correction in content and 'attributed_correction' in content
    assert len(page['source_refs']) == 2


def test_corrected_old_document_keeps_record_times_distinct_from_its_contents_and_correction(tmp_path, monkeypatch):
    from pacomind.turns import source_annotations
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    observed_at = '2026-09-12T08:00:00+00:00'
    text = 'Service handbook, 2021 edition: cancellation requires a telephone call.'
    ledger.record_source('source', contact_id='person', session_id='original',
        messages=[{'role': 'tool', 'content': text}], occurred_at=observed_at, derive_claims=False)
    original = opened(ledger)
    assert json.loads(original['content'])['messages'][0]['content'] == text
    assert original['reported_at'] == observed_at
    assert original['recorded_at'] != observed_at
    assert 'supported historical or stable facts' in original['guidance']
    assert 'does not re-inspect its underlying subject' in original['guidance']

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 13, 9, tzinfo=timezone.utc)

    monkeypatch.setattr(source_annotations, 'datetime', LaterClock)
    assert opened(ledger) == original
    correction = 'As of 2026-09-13, the service also accepts cancellation through its portal.'
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='policy',
        **ref(ledger), excerpt='cancellation requires a telephone call',
        correction=correction, author_principal='operator')
    corrected = opened(ledger)
    assert corrected['reported_at'] == original['reported_at']
    assert corrected['recorded_at'] == original['recorded_at']
    assert corrected['source_version'] == original['source_version']
    assert corrected['read_revision'] != original['read_revision']
    assert text in corrected['content'] and correction in corrected['content']
    assert '2026-09-13T09:00:00+00:00' in corrected['content']
    assert 'operator' in corrected['content']
    assert note['source_id'] in {r['source_id'] for r in corrected['source_refs']}


@pytest.mark.asyncio
async def test_oversized_conflict_is_discoverable_and_history_pages_current_sources(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    projection = SourceClaimProjection(ledger)
    for index in range(10):
        text = f'My office is in District{index}.'
        ledger.record_source(f's{index}', contact_id='person', session_id='original',
            messages=[{'role': 'user', 'content': text}])
        await projection.process_one(Model({text: claim(text, f'District{index}')}))
    _, rows = projection.prepare_context([], ledger.search_sources('office', contact_id='person', session_id='later'),
        contact_id='person', session_id='later', time_query=interpret_time_query('office', now=datetime.now(timezone.utc)))
    selected, packet = pack_memory_context(rows)
    assert 'incomplete_assertion_history' in packet and 'history_anchor' in packet
    assert not any(f'District{i}' in packet for i in range(10))
    anchor = selected[0]['history_anchor']
    first = opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'])
    second = opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'],
                    offset=first['next_offset'], read_revision=first['read_revision'])
    assert first['total'] == 10 and not first['complete'] and second['complete']
    assertions = json.loads(first['content'])['assertions'] + json.loads(second['content'])['assertions']
    assert {c['value'] for c in assertions} == {f'District{i}' for i in range(10)}
    assert all(c['superseded_by'] is None and c['retracted_by'] is None for c in assertions)
    victim = next(c['turn_id'] for c in assertions if c['turn_id'] != anchor['source_id'])
    ledger.erase_sources(contact_id='person', turn_ids=[victim])
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'],
               offset=first['next_offset'], read_revision=first['read_revision'])
    assert opened(ledger, anchor['source_id'], view='assertions', claim_id=anchor['claim_id'])['total'] == 9


@pytest.mark.asyncio
async def test_memory_read_canonical_mode_enforces_principal_without_graph(source_app, tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('source', contact_id='person', session_id='original',
                         messages=[{'role': 'user', 'content': 'Useful procedure.'}])
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='reader', secret='read', viewer='person', scopes=['memory:read']),
                            _principal(principal='other', secret='other', viewer='other', scopes=['memory:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    body = {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later', **ref(ledger)}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        good = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer read'})
        assert good.status_code == 200 and good.json()['source']['complete'], good.text
        denied = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer other'})
        assert denied.status_code == 403
        missing_cursor = await client.post('/v1/host/memory/read', json={**body, 'offset': 1}, headers={'Authorization': 'Bearer read'})
        assert missing_cursor.status_code == 422


def retain_call(ledger, origin, number, content, *, sources=(), reason='Useful inspection evidence.'):
    import hashlib
    from pacomind.turns.tool_observations import ToolObservation, identity_id, record
    native = dict(profile_id='a'*64, session_id='original', task_id='task', turn_id='turn',
        tool_call_id=f'call-{number}', api_request_id='request', tool_name='terminal',
        message_id=number, timestamp=1234567890.0 + number,
        result_sha256=hashlib.sha256(content.encode()).hexdigest())
    sid = identity_id(native)
    record(ledger, ToolObservation(native=native, content=content, origin=origin,
        sources=list(sources), reason=reason), contact_id='person', session_id='original', source_id=sid)
    return ref(ledger, sid)


def observation_origin(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    ledger.record_source('source', contact_id='person', session_id='original',
        messages=[{'role': 'user', 'content': 'Inspect the Corvus export and its checksum.'}], derive_claims=False)
    return ledger, ref(ledger)


def test_observation_directory_uses_index_and_exact_first_origin_not_nomination(tmp_path):
    from pacomind.turns.tool_observations import ORIGIN_QUERY
    ledger, origin = observation_origin(tmp_path)
    empty = opened(ledger, view='observations')
    assert json.loads(empty['content'])['observations'] == [] and empty['complete']
    weather = retain_call(ledger, origin, 1, 'Weather station: light rain.', reason='The checksum is repaired.')
    outcome = retain_call(ledger, origin, 2, 'Checksum mismatch; exit 1; files modified 0.')
    ledger.record_source('unrelated', contact_id='person', session_id='original',
        messages=[{'role':'user', 'content':'Inspect the weather file.'}], derive_claims=False)
    misleading = retain_call(ledger, ref(ledger, 'unrelated'), 3, 'Unrelated command result.', sources=[origin])
    result = opened(ledger, view='observations')
    entries = json.loads(result['content'])['observations']
    assert {entry['source_id'] for entry in entries} == {weather['source_id'], outcome['source_id']}
    assert misleading not in result['source_refs']
    assert 'Weather station' not in result['content'] and 'Checksum mismatch' not in result['content']
    assert next(e for e in entries if e['source_id'] == weather['source_id'])['selection_reason'] == {
        'author':'model', 'reason':'The checksum is repaired.'}
    assert result['reported_at'] is None  # Outer times belong to the instruction.
    for entry, number in ((next(e for e in entries if e['source_id'] == weather['source_id']), 1),
                          (next(e for e in entries if e['source_id'] == outcome['source_id']), 2)):
        assert entry['observed_at'] == datetime.fromtimestamp(1234567890.0 + number, timezone.utc).isoformat()
        assert entry['observed_at'] != entry['recorded_at']
        source = opened(ledger, entry['source_id'])
        assert source['reported_at'] == entry['observed_at']
        assert source['recorded_at'] == entry['recorded_at']
    assert 'light rain' in opened(ledger, weather['source_id'])['content']
    assert 'files modified 0' in opened(ledger, outcome['source_id'])['content']
    with ledger._connect() as conn:
        plan = [row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN ' + ORIGIN_QUERY,
            ('person', 'source', 'later', 65))]
    assert any('SEARCH turn_sources USING INDEX source_observation_origin' in detail for detail in plan), plan
    assert not any('TEMP B-TREE' in detail for detail in plan), plan


def test_observation_pagination_pins_membership_and_nonpage_corrections_and_bounds_cohort(tmp_path):
    ledger, origin = observation_origin(tmp_path)
    observations = [retain_call(ledger, origin, i, f'Inspection result {i}.') for i in range(1, 6)]
    first = opened(ledger, view='observations')
    assert first['offset_unit'] == 'observations' and first['total'] == 5 and first['next_offset'] == 4
    second = opened(ledger, view='observations', offset=4, read_revision=first['read_revision'])
    assert second['complete'] and len(json.loads(second['content'])['observations']) == 1
    selected_ids = {entry['source_id'] for entry in json.loads(first['content'])['observations']}
    other = next(r for r in observations if r['source_id'] not in selected_ids)
    ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='other-note',
        **other, excerpt='Inspection result', correction='The result is provisional.', author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, view='observations', offset=4, read_revision=first['read_revision'])
    fresh = opened(ledger, view='observations')
    retain_call(ledger, origin, 6, 'Inspection result 6.')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(ledger, view='observations', offset=4, read_revision=fresh['read_revision'])
    for i in range(7, 66):
        retain_call(ledger, origin, i, f'Inspection result {i}.')
    bounded = opened(ledger, view='observations')
    assert json.loads(bounded['content'])['status'] == 'cohort_limit_exceeded'
    assert bounded['complete'] is False and bounded['total'] is None and bounded['next_offset'] is None
    assert bounded['source_refs'] == [origin]


def test_observation_open_carries_parent_and_result_corrections_and_rejects_attribution_or_erase(tmp_path):
    from pacomind.turns.source_attribution import correct
    ledger, origin = observation_origin(tmp_path)
    observation = retain_call(ledger, origin, 1, 'Checksum mismatch; files modified 0.')
    notes = []
    for number, target, excerpt, correction in (
        (1, origin, 'Corvus export', 'The request meant the staging export, not production.'),
        (2, observation, 'Checksum mismatch', 'The expected checksum was supplied manually.')):
        notes.append(ledger.append_source_annotation(contact_id='person', session_id='later',
            annotation_id=f'note-{number}', **target, excerpt=excerpt, correction=correction, author_principal='operator'))
    for result in (opened(ledger, view='observations'), opened(ledger, observation['source_id'])):
        assert 'staging export' in result['content'] and 'supplied manually' in result['content']
        assert all({key:note[key] for key in ('source_id','source_version')} in result['source_refs'] for note in notes)
    moved = correct(ledger, operation_id='correct-person', performed_by='operator',
        old_contact_id='person', contact_id='actual-person', source_ids=['source'], evidence_refs=['owner-confirmation'])
    assert observation['source_id'] in moved['invalidated_source_ids']
    assert not ledger.source_references([observation['source_id']], contact_id='person', session_id='later')
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **observation)
    # A separate current origin exercises existing erasure traversal as well.
    ledger.record_source('erase-origin', contact_id='person', session_id='original',
        messages=[{'role':'user','content':'Inspect a separate archive.'}], derive_claims=False)
    erased = retain_call(ledger, ref(ledger, 'erase-origin'), 2, 'Separate archive failed.')
    ledger.erase_sources(contact_id='person', turn_ids=['erase-origin'])
    with pytest.raises(ValueError, match='unavailable'):
        read(ledger, contact_id='person', session_id='later', **erased)


def test_observation_directory_rechecks_correction_race_and_original_digest(tmp_path, monkeypatch):
    import pacomind.turns.source_read as reader
    ledger, origin = observation_origin(tmp_path)
    observation = retain_call(ledger, origin, 1, 'Checksum mismatch.')
    original_expand = reader.expand
    calls = 0
    def raced(*args, **kwargs):
        nonlocal calls
        result = original_expand(*args, **kwargs)
        calls += 1
        if calls == 2:
            ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='race',
                **origin, excerpt='Corvus export', correction='The staging copy was intended.', author_principal='operator')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(reader, 'expand', raced)
        with pytest.raises(ValueError, match='correction_unavailable_or_changed|changed_during_read'):
            opened(ledger, view='observations')
    with ledger._connect() as conn:
        row = conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (observation['source_id'],)).fetchone()
        messages = json.loads(row[0]); messages[0]['content'] = 'Unsupported replacement result.'
        conn.execute('UPDATE turn_sources SET messages_json=? WHERE turn_id=?',
            (json.dumps(messages), observation['source_id']))
    result = opened(ledger, view='observations')
    assert observation['source_id'] not in result['content'] and result['total'] == 0
