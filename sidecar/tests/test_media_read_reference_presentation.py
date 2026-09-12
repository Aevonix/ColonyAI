"""Copyable canonical media references survive real context packing and reading."""
import base64
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from apsimo.memory.recall import pack_memory_context, render_memory_context
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.media import SourceMedia
from apsimo.turns.source_annotations import expand, current_candidates
from apsimo.turns.source_read import read
from test_native_request_erasure import runtime, freshness_response
from test_recall_source_presentation import rendered_rows
from test_source_media import Vision, image_bytes, message
from test_turn_source_evidence import source_app


@pytest_asyncio.fixture
async def media_source(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('drawing-source', contact_id='person', session_id='earlier-text',
                         messages=[message()], derive_claims=False)
    media = SourceMedia(ledger)
    assert await media.process_one(Vision())
    ref, = ledger.source_references(['drawing-source'], contact_id='person', session_id='later')
    return source_app, ledger, media, ref


def annotate(ledger, ref):
    return ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='drawing-note',
        **ref, excerpt='Please retain this reference image.',
        correction='The drawing illustrates shapes only; it is not a camera observation.', author_principal='operator')


@pytest.mark.asyncio
@pytest.mark.parametrize('corrected', [False, True])
async def test_real_context_pair_opens_original_through_native_reader(media_source, runtime, corrected):
    app, ledger, media, ref = media_source
    if corrected:
        note = annotate(ledger, ref)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as api:
        response = await api.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'test'}, 'context': {'contact_id': 'person', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': 'Inspect the original blue circle reference image.'}})
    assert response.status_code == 200, response.text
    section = next(s for s in response.json()['sections'] if s['id'] == 'colony-memory')
    row, = [r for r in rendered_rows(section['body']) if r.get('kind') == 'media_description']
    copied = {key: row[key] for key in ('source_id', 'source_version')}
    assert copied == ref and copied in section['citations']
    assert row['source_id'] != row['id'] and row['source_id'] == row['source_turn_id']
    if corrected:
        assert 'not a camera observation' in row['content']
        assert note['source_id'] in {r['source_id'] for r in section['citations']}
    asset = row['asset_id'].removeprefix('sha256:')
    opens = []
    def get(path, **kwargs):
        return httpx.Response(200, json=ledger.erasure_feed('person', kwargs['params']['after']),
                              request=httpx.Request('GET', 'http://fixture'+path))
    def post(path, **kwargs):
        if path.endswith('/sources/erasures'):
            return freshness_response(ledger, path, kwargs['json'])
        body = kwargs['json']; opens.append(body)
        result = read(ledger, contact_id=body['person_id'], session_id=body['session_id'],
            source_id=body['source_id'], source_version=body['source_version'], view=body['source_view'],
            asset_hash=body['asset_hash'], read_revision=body.get('read_revision'))
        return httpx.Response(200, json={'source': result}, request=httpx.Request('POST', 'http://fixture'+path))
    client = SimpleNamespace(get=get, post=post)
    boundary = runtime.module.RequestMemory(client, runtime.outbox)
    scope = SimpleNamespace(contact_id='person', session_id='later', task_id='task', turn_id='turn',
                            valid_participant=True, authority_lane='guest')
    current = {'role': 'user', 'content': 'Inspect the original reference image.'}
    boundary.observe(scope, [current], user_message=current['content'])
    stamp = json.dumps({'contact_id': 'person', 'watermark': ledger.erasure_watermark('person'),
                        'sources': section['citations']})
    current['api_content'] = current['content'] + '\n\n<memory-context>\n[colony-recall-v1 ' + stamp + ']\n' + section['body'] + '\n[/colony-recall-v1]\n</memory-context>'
    wire = {'role': 'user', 'content': current['api_content']}
    boundary({'messages': [wire]}, scope)
    helper = importlib.import_module(runtime.module.__package__ + '.source_read')
    args = {**copied, 'view': 'image', 'asset_hash': asset}
    denied = json.loads(helper.handle(dict(args, source_id='media:'+asset), scope, client, boundary,
                                      {'tool_call_id': 'wrong-id'}))
    assert denied['matching_supplied_sources'] == [ref] and denied['error']
    assert 'No source was opened' in denied['guidance'] and opens == []
    # The caller explicitly copies the canonical pair; the tool does not rebind it.
    result = helper.handle(args, scope, client, boundary, {'tool_call_id': 'actual-image'})
    assert result['_multimodal'] is True and len(opens) == 1
    assert base64.b64decode(result['content'][1]['image_url']['url'].split(',', 1)[1]) == image_bytes()
    if corrected:
        assert 'not a camera observation' in result['content'][0]['text']
    later = SimpleNamespace(**(vars(scope) | {'turn_id': 'different-turn'}))
    denied = json.loads(helper.handle(args, later, client, boundary, {'tool_call_id': 'other-turn'}))
    assert denied == {'error': 'The source revision must have been supplied to this participant and turn'}
    assert len(opens) == 1


@pytest.mark.asyncio
async def test_media_pair_is_charged_to_budget_and_corrections_remain_indivisible(media_source):
    _, ledger, media, ref = media_source
    candidates = media.search('blue circle', contact_id='person', session_id='later')
    rows = expand(ledger, candidates, contact_id='person', session_id='later')
    before = deepcopy(rows)
    selected, complete = pack_memory_context(rows)
    assert rows == before and len(selected) == 1
    assert json.dumps(ref['source_version']) in complete
    assert pack_memory_context(rows, max_chars=len(complete))[1] == complete
    selected, shortened = pack_memory_context(rows, max_chars=len(complete)-1)
    # For this short caption, adding the existing truncation notice would leave
    # fewer than the minimum useful80 characters. Omit it, never drop the pair
    # from budget accounting to make the old metadata-only size appear to fit.
    assert (selected, shortened) == ([], '')
    note = annotate(ledger, ref)
    rows = expand(ledger, candidates, contact_id='person', session_id='later')
    _, complete = pack_memory_context(rows)
    assert 'not a camera observation' in complete
    assert pack_memory_context(rows, max_chars=len(complete)-1) == ([], '')
    ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    assert current_candidates(ledger, rows, contact_id='person', session_id='later') == []


def test_unchecked_or_unrelated_media_metadata_does_not_invent_canonical_pair():
    row = {'id': 'media:example', 'kind': 'media_description', 'source_turn_id': 'drawing',
           'content': 'A neutral drawing.'}
    for refs in ([], [{'source_id': 'different-source', 'source_version': '1'*64}]):
        rendered, = rendered_rows(render_memory_context([dict(row, _annotation_source_refs=refs)]))
        assert 'source_id' not in rendered and 'source_version' not in rendered


def test_wrong_id_diagnostic_is_bounded_to_supplied_matching_versions(runtime):
    helper = importlib.import_module(runtime.module.__package__ + '.source_read')
    refs = [{'source_id': f'available-{n}', 'source_version': '1'*64} for n in range(5)]
    boundary = SimpleNamespace(supplied_snapshot=lambda scope: refs+[
        {'source_id': 'different-version', 'source_version': '2'*64}])
    scope = SimpleNamespace(valid_participant=True, task_id='task', turn_id='turn')
    # No client methods exist: diagnostics must never open or search sources.
    result = json.loads(helper.handle({'source_id': 'wrong-id', 'source_version': '1'*64},
        scope, object(), boundary, {'tool_call_id': 'wrong-id'}))
    assert result['error'] and result['matching_supplied_sources'] == refs[:4]
    assert 'different-version' not in json.dumps(result)
    unknown = json.loads(helper.handle({'source_id': 'wrong-id', 'source_version': '3'*64},
        scope, object(), boundary, {'tool_call_id': 'unknown'}))
    assert unknown == {'error': 'The source revision must have been supplied to this participant and turn'}
