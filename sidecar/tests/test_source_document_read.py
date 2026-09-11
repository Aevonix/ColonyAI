"""PDF readback uses actual normalized ownership and stored, fallible pages."""
import base64
import copy
import hashlib
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from apsimo.api.middleware import ApiKeyMiddleware
from apsimo.api.schemas.host import MemoryReadRequest
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.documents import disposition
from apsimo.turns.idempotency import source_message_hash
from apsimo.turns.source_read import read
from test_native_request_erasure import runtime, freshness_response
from test_scoped_api_authority import _principal, _write_keyring
from test_source_documents import pdf_bytes
from test_turn_source_evidence import source_app


CAPTION = 'Retain this PDF reference for the workshop.'
PAGE_TEXT = 'Page one: isolate the pump. ' + 'Inspect the seal; ' * 600 + 'Then restore power.'
PDF = pdf_bytes((PAGE_TEXT, '', 'The original third page.'))
ASSET = hashlib.sha256(PDF).hexdigest()


def message():
    return {'role': 'user', 'content': [{'type': 'text', 'text': CAPTION},
        {'type': 'input_document', 'input_document': {
            'mime_type': 'application/pdf', 'data': base64.b64encode(PDF).decode()}}]}


def retained(ledger, *, identifier='document', contact='person', session='original', scope='person'):
    original = message()
    ledger.record_source(identifier, contact_id=contact, session_id=session,
                         messages=[original], scope=scope, derive_claims=False)
    with ledger._connect() as conn:
        canonical = json.loads(conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                            (identifier,)).fetchone()[0])[0]
    assert canonical['content'][1] == {'type': 'document', 'asset_id': 'sha256:' + ASSET,
                                      'mime_type': 'application/pdf'}
    assert source_message_hash(session, canonical) == source_message_hash(session, original)
    return ledger.source_references([identifier], contact_id=contact, session_id=session)[0]


def derivative(ledger, *, status='partial', reason='no_extractable_text_on_some_pages', pages=None,
               page_count=3, parser_version='fixture-parser-a'):
    if pages is None:
        pages = [{'page': 1, 'text': PAGE_TEXT, 'status': 'text'},
                 {'page': 2, 'text': '', 'status': 'no_extractable_text'},
                 {'page': 3, 'text': 'The original third page.', 'status': 'text'}]
    result = disposition(status, reason, parser_version=parser_version, page_count=page_count, pages=pages)
    row_status = 'complete' if status in {'complete', 'partial'} else 'document_' + status
    with ledger._connect() as conn, conn:
        conn.execute('UPDATE source_media SET status=?,media_metadata_json=? WHERE asset_hash=?',
                     (row_status, json.dumps({'document': result}), ASSET))


@pytest.fixture
def original(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ref = retained(ledger)
    derivative(ledger)
    return ledger, ref


def opened(original, **changes):
    ledger, ref = original
    return read(ledger, **({'contact_id': 'person', 'session_id': 'later', **ref,
                           'view': 'document', 'asset_hash': ASSET, 'page': 1} | changes))


def all_content(original, **changes):
    result = opened(original, **changes)
    parts = [result['content']]
    while not result['complete']:
        result = opened(original, **changes, offset=result['next_offset'], read_revision=result['read_revision'])
        assert len(result['content']) <= 4096
        parts.append(result['content'])
    return ''.join(parts), result


def test_page_chunks_keep_original_page_numbers_without_parsing_or_returning_original_bytes(original, monkeypatch):
    from apsimo.turns.media import SourceMedia
    from apsimo.turns import documents
    def forbidden(*args, **kwargs):
        raise AssertionError('readback must not invoke a parser or generic asset read')
    monkeypatch.setattr(SourceMedia, 'read', forbidden)
    monkeypatch.setattr(documents, 'extract_document', forbidden)
    first = opened(original)
    assert len(first['content']) == 4096 and not first['complete']
    assert first['offset_unit'] == 'characters' and first['document']['page_count'] == 3
    text, result = all_content(original)
    assert json.loads(text)['document']['text'] == PAGE_TEXT
    assert result['source_refs'] == [original[1]]
    blank = json.loads(opened(original, page=2)['content'])['document']
    assert blank['page'] == 2 and blank['page_status'] == 'no_extractable_text' and blank['text'] == ''
    third = json.loads(opened(original, page=3)['content'])['document']
    assert third['page'] == 3 and third['text'] == 'The original third page.'
    with pytest.raises(ValueError, match='page_unavailable'):
        opened(original, page=4)
    assert base64.b64encode(PDF).decode() not in text and 'data_url' not in text


@pytest.mark.parametrize('status,page_count', [('pending', None), ('unsupported', 3),
                                              ('unsupported', 0), ('failed', None)])
def test_unavailable_extraction_has_honest_disposition_and_no_stale_page_text(original, status, page_count):
    derivative(original[0], status=status, page_count=page_count, reason='fixture_' + status)
    result = opened(original)
    document = json.loads(result['content'])['document']
    assert document['status'] == status and document['reason'] == 'fixture_' + status
    assert document['text'] == '' and document['ocr_performed'] is False
    assert 'isolate the pump' not in result['content']


def test_image_only_pdf_page_reports_no_extractable_text(original):
    derivative(original[0], status='unsupported', reason='no_extractable_text')
    document = json.loads(opened(original, page=2)['content'])['document']
    assert document['page_status'] == 'no_extractable_text' and document['text'] == ''
    assert document['epistemic_state'] == 'derived_unverified'


def test_derivative_and_attributed_message_corrections_fence_every_continuation(original):
    ledger, ref = original
    first = opened(original)
    derivative(ledger, parser_version='fixture-parser-b')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, offset=first['next_offset'], read_revision=first['read_revision'])
    first = opened(original)
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='correction',
        **ref, excerpt=CAPTION, correction='The workshop PDF is a draft; confirm the procedure before relying on it.',
        author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, offset=first['next_offset'], read_revision=first['read_revision'])
    text, corrected = all_content(original)
    assert 'attributed_correction' in text and 'PDF is a draft' in text
    assert note['source_id'] in {item['source_id'] for item in corrected['source_refs']}
    # The existing annotation API anchors canonical messages, not fallible
    # extracted page wording. A companion caption qualifies all selected pages.
    with pytest.raises(ValueError, match='excerpt_mismatch'):
        ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='page-wording',
            **ref, excerpt='Page one: isolate the pump.', correction='Unverified.', author_principal='operator')
    assert 'PDF is a draft' in opened(original, page=3)['content']
    ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original)


def test_canonical_handle_cannot_borrow_another_owner_and_erase_respects_shared_bytes(original):
    ledger, ref = original
    foreign = retained(ledger, identifier='foreign', contact='other')
    private = retained(ledger, identifier='private', scope='session', session='private')
    ledger.record_source('unlinked', contact_id='person', session_id='original', messages=[{
        'role': 'user', 'content': [{'type': 'document', 'asset_id': 'sha256:' + ASSET,
                                    'mime_type': 'application/pdf'}]}], derive_claims=False)
    unlinked = ledger.source_references(['unlinked'], contact_id='person', session_id='later')[0]
    for changes in ({'contact_id': 'stranger'}, {'source_version': '0'*64},
                    {'asset_hash': '0'*64}, private, foreign, unlinked):
        with pytest.raises(ValueError, match='unavailable'):
            opened(original, **changes)
    assert opened(original, **private, session_id='private')['document']['page'] == 1
    ledger.erase_sources(contact_id='person', turn_ids=[ref['source_id']])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original)
    assert opened(original, **foreign, contact_id='other')['document']['status'] == 'partial'


def test_derivative_change_racing_readback_is_withheld(original, monkeypatch):
    module = importlib.import_module('apsimo.turns.source_read')
    previous = module._document_page
    changed = False
    def racing(*args, **kwargs):
        nonlocal changed
        result = previous(*args, **kwargs)
        if not changed:
            changed = True
            derivative(original[0], parser_version='changed-during-read')
        return result
    monkeypatch.setattr(module, '_document_page', racing)
    with pytest.raises(ValueError, match='changed_during_read'):
        opened(original)


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'oversized'])
def test_missing_or_corrupt_original_never_serves_normal_document_evidence(original, damage):
    from apsimo.turns.documents import MAX_DOCUMENT_BYTES
    from apsimo.turns.media import SourceMedia
    first = opened(original, page=3)
    path = SourceMedia(original[0]).store._original_path(ASSET, 'application/pdf')
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'x' * (MAX_DOCUMENT_BYTES + 1) if damage == 'oversized' else b'%PDF-corrupt')
    for revision in (None, first['read_revision']):
        with pytest.raises(ValueError, match='source_document_original'):
            opened(original, page=3, read_revision=revision)


@pytest.mark.asyncio
async def test_document_api_scope_and_strict_page_selector(source_app, original, tmp_path):
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='reader', secret='read', viewer='person', scopes=['memory:read']),
                            _principal(principal='other', secret='other', viewer='other', scopes=['memory:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    body = {'identity': {'host_id': 'fixture'}, 'person_id': 'person', 'session_id': 'later',
            **original[1], 'source_view': 'document', 'asset_hash': ASSET, 'page': 3}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = await client.post('/v1/host/memory/read', json=body, headers={'Authorization': 'Bearer read'})
        assert result.status_code == 200, result.text
        assert result.json()['source']['document']['page'] == 3
        assert (await client.post('/v1/host/memory/read', json=body,
                                  headers={'Authorization': 'Bearer other'})).status_code == 403
        for changes in ({'asset_hash': None}, {'asset_hash': '/tmp/reference.pdf'}, {'page': None},
                        {'page': 0}, {'page': True}, {'page': '1'}, {'page': 1.0}, {'offset': 1},
                        {'source_view': 'source'}, {'claim_id': 'claim'}):
            invalid = await client.post('/v1/host/memory/read', json=body | changes,
                                         headers={'Authorization': 'Bearer read'})
            assert invalid.status_code == 422, invalid.text
    with pytest.raises(ValidationError):
        MemoryReadRequest(identity={'host_id': 'fixture'}, page=1)


@pytest.fixture
def document_runtime(runtime):
    rt = runtime
    rt.ref = retained(rt.ledger, contact='owner')
    derivative(rt.ledger)
    rt.calls = []
    def get(path, **kwargs):
        return httpx.Response(200, json=rt.ledger.erasure_feed('owner', kwargs['params']['after']),
                              request=httpx.Request('GET', 'http://fixture' + path))
    def post(path, **kwargs):
        if path.endswith('/sources/erasures'):
            return freshness_response(rt.ledger, path, kwargs['json'])
        body = kwargs['json']; rt.calls.append(body)
        try:
            result = read(rt.ledger, contact_id=body['person_id'], session_id=body['session_id'],
                source_id=body['source_id'], source_version=body['source_version'], view=body['source_view'],
                asset_hash=body.get('asset_hash'), page=body.get('page'), offset=body.get('offset', 0),
                read_revision=body.get('read_revision'), claim_id=body.get('claim_id'))
            return httpx.Response(200, json={'source': result}, request=httpx.Request('POST', 'http://fixture' + path))
        except ValueError:
            return httpx.Response(409, json={'error': 'changed'}, request=httpx.Request('POST', 'http://fixture' + path))
    rt.client = SimpleNamespace(get=get, post=post)
    rt.middleware = rt.module.RequestMemory(rt.client, rt.outbox)
    rt.scope = SimpleNamespace(contact_id='owner', session_id='later', task_id='task', turn_id='turn',
                               valid_participant=True, authority_lane='guest')
    current = {'role': 'user', 'content': 'Open the original numbered PDF page.'}
    rt.middleware.observe(rt.scope, [current], user_message=current['content'])
    stamp = json.dumps({'contact_id': 'owner', 'watermark': 0, 'sources': [rt.ref]})
    current['api_content'] = current['content'] + '\n\n<memory-context>\n[colony-recall-v1 ' + stamp + ']\n' + ASSET + '\n[/colony-recall-v1]\n</memory-context>'
    rt.wire = {'role': 'user', 'content': current['api_content']}
    rt.middleware({'messages': [rt.wire]}, rt.scope)
    rt.helper = importlib.import_module(rt.module.__package__ + '.source_read')
    return rt


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('change', ['derivative', 'pending_complete', 'correction', 'erase', 'outage', 'attribution', 'missing_original'])
def test_native_dispatch_revalidates_actual_document_derivative_and_corrections(document_runtime, shape, change):
    rt = document_runtime
    if change == 'pending_complete':
        derivative(rt.ledger, status='pending', page_count=None)
    args = {**rt.ref, 'view': 'document', 'asset_hash': ASSET, 'page': 3}
    result = rt.helper.handle(args, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'actual-document'})
    assert 'error' not in json.loads(result), result
    if shape == 'responses':
        request = {'input': [rt.wire, {'type': 'function_call_output', 'call_id': 'actual-document', 'output': result}]}
    elif shape == 'anthropic':
        request = {'messages': [rt.wire, {'role': 'assistant', 'content': [{'type': 'tool_use',
            'id': 'actual-document', 'name': 'colony_memory_read_source', 'input': args}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'actual-document', 'content': result}]}]}
    else:
        request = {'messages': [rt.wire, {'role': 'tool', 'tool_call_id': 'actual-document', 'content': result}]}
    before = copy.deepcopy(request)
    assert rt.middleware(request, rt.scope)['request'] == request
    assert len(rt.calls) == 2 and rt.calls[-1]['read_revision'] == json.loads(result)['read_revision']
    if change in {'derivative', 'pending_complete'}:
        derivative(rt.ledger, parser_version='fixture-parser-b')
    elif change == 'correction':
        rt.ledger.append_source_annotation(contact_id='owner', session_id='later', annotation_id='note',
            **rt.ref, excerpt=CAPTION, correction='This PDF is a draft.', author_principal='operator')
    elif change == 'erase':
        rt.ledger.erase_sources(contact_id='owner', turn_ids=['document'])
    elif change == 'attribution':
        from apsimo.turns.source_attribution import correct
        correct(rt.ledger, operation_id='identity-correction', performed_by='operator', old_contact_id='owner',
                contact_id='actual-person', source_ids=['document'], evidence_refs=['owner-confirmation'])
    elif change == 'missing_original':
        from apsimo.turns.media import SourceMedia
        SourceMedia(rt.ledger).store._original_path(ASSET, 'application/pdf').unlink()
    else:
        rt.client.post = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('offline'))
    checked = rt.middleware(request, rt.scope)['request']
    assert 'withheld' in json.dumps(checked) and 'The original third page.' not in json.dumps(checked)
    if shape == 'anthropic':
        assert checked['messages'][-2] == request['messages'][-2]
    assert request == before


def test_native_document_selector_cannot_widen_scope_or_take_a_path(document_runtime):
    rt = document_runtime
    args = {**rt.ref, 'view': 'document', 'asset_hash': ASSET, 'page': 1}
    for changes in ({'page': True}, {'page': 0}, {'page': None}, {'asset_hash': '/tmp/reference.pdf'},
                    {'person_id': 'other'}, {'offset': 1}, {'view': 'source'}, {'claim_id': 'history'}):
        result = rt.helper.handle(args | changes, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'invalid'})
        assert 'error' in json.loads(result)
    assert rt.calls == []


def test_native_continuation_receipts_revalidate_the_same_original_page_and_offset(document_runtime):
    rt = document_runtime
    args = {**rt.ref, 'view': 'document', 'asset_hash': ASSET, 'page': 1}
    first = rt.helper.handle(args, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'first-page-chunk'})
    first_page = json.loads(first)
    assert len(first_page['content']) == 4096 and not first_page['complete']
    continuation = args | {'offset': first_page['next_offset'], 'read_revision': first_page['read_revision']}
    second = rt.helper.handle(continuation, rt.scope, rt.client, rt.middleware, {'tool_call_id': 'second-page-chunk'})
    assert json.loads(second)['offset'] == 4096 and len(json.loads(second)['content']) <= 4096
    request = {'messages': [rt.wire,
        {'role': 'tool', 'tool_call_id': 'first-page-chunk', 'content': first},
        {'role': 'tool', 'tool_call_id': 'second-page-chunk', 'content': second}]}
    assert rt.middleware(request, rt.scope)['request'] == request
    assert [(call['page'], call['offset']) for call in rt.calls] == [(1, 0), (1, 4096), (1, 0), (1, 4096)]
