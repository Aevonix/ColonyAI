"""Canonical input gives a scoped request meaning to a live execution record."""
import json
import sqlite3

import pytest

from colony_sidecar.api.routers import executions
from colony_sidecar.turns.executions import ExecutionRegistry, request_work_context, format_view
from colony_sidecar.turns.idempotency import source_message_hash
from test_execution_registry import observation, store
from test_hermes_general_governance import _Context
from test_host_input_provenance import handoff
from test_turn_source_evidence import source_app


def admitted(store, *, name='input', person='owner', session='source-session',
             scope='person', text='Review the turbine maintenance table and record any inconsistencies.'):
    message = {'role': 'user', 'content': text}
    store.ledger.record_source(name, contact_id=person, session_id=session, scope=scope,
                               messages=[message], derive_claims=False)
    return [{'source_id': name, 'input_message_hash': source_message_hash(session, message)}]


def bind(store, refs, *, name='root', person='owner', session='native-session'):
    value = observation(name, contact_id=person, session_id=session, input_refs=refs)
    assert store.observe(value, principal_id='host', contact_id=person)['accepted']
    return value


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
def test_registered_hooks_api_and_request_inject_admitted_input_not_task_wrapper(handoff, monkeypatch, shape):
    h = handoff
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    h.api.app.include_router(executions.router)
    ctx = _Context({**h.ctx.config['plugins']['colony'], 'execution_registry_enabled': True,
                    'turn_writer_platforms': ['cli']})
    h.module.register(ctx)
    original = 'Use the lamp maintenance record I supplied.'
    wrapper = 'NATIVE_TASK_WRAPPER_DO_NOT_QUOTE_AS_HUMAN'
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native',
            input_refs=h.parents, source_refs=h.refs):
        ctx.hooks['pre_llm_call'](session_id='native', task_id='task', turn_id='turn',
            platform='cli', sender_id='', user_message=wrapper, conversation_history=[])
        current = h.api.get('/v1/host/executions', params={'contact_id': 'owner',
            'session_id': 'observer', 'projection': 'request', 'input_context': True})
        assert current.status_code == 200, current.text
        assert original in current.json()['text'] and wrapper not in current.json()['text']
        legacy = h.api.get('/v1/host/executions', params={'contact_id': 'owner',
            'session_id': 'observer', 'projection': 'request'}).json()
        assert original not in legacy['text'] and 'input_provenance' not in legacy
        ctx.hooks['pre_llm_call'](session_id='observer', task_id='observer-task', turn_id='observer-turn',
            platform='cli', sender_id='', user_message='What are you doing?', conversation_history=[])
        # The observer is independent of the supplied-input scope. Its source
        # validity is intentionally not inherited from the first task.
    payload = {'messages': [{'role': 'user', 'content': 'What are you doing?'}]}
    if shape == 'responses':
        payload = {'input': payload['messages'], 'instructions': 'A stable identity.'}
    elif shape == 'anthropic':
        payload['system'] = 'A stable identity.'
    request = ctx.middleware['llm_request'](payload,
        session_id='observer', task_id='observer-task', turn_id='observer-turn',
        api_mode='anthropic_messages' if shape == 'anthropic' else '')['request']
    assert original in json.dumps(request) and wrapper not in json.dumps(request)
    ctx.hooks['post_llm_call'](session_id='observer', task_id='observer-task', turn_id='observer-turn',
        user_message='What are you doing?', assistant_response='I am inspecting the requested lamp record.',
        platform='cli', model='controlled')
    receipt = h.outbox.snapshot()[0]
    assert receipt['state'] == 'delivered', receipt
    assert receipt['payload']['assistant_source_refs'] == h.ledger.source_references(
        ['original-input'], contact_id='owner', session_id='observer')
    with sqlite3.connect(h.ledger.db_path) as db:
        stored = '\n'.join(row[0] for row in db.execute('SELECT metadata_json FROM execution_runtime_observations'))
    assert 'original-input' in stored and original not in stored and wrapper not in stored
    h.ledger.erase_sources(contact_id='owner', turn_ids=['original-input'])
    with sqlite3.connect(h.ledger.db_path) as db:
        retained = db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                              (receipt['turn_id'],)).fetchone()[0]
    assert 'requested lamp record' not in retained and 'What are you doing?' in retained
    after = ctx.middleware['llm_request'](request,
        session_id='observer', task_id='observer-task', turn_id='observer-turn')['request']
    assert original not in json.dumps(after)
    full = h.api.get('/v1/host/executions', params={'contact_id': 'owner', 'session_id': 'observer'}).json()
    root = next(row for row in full['items'] if row['session_id'] == 'native')
    assert root['request_input']['status'] == 'source_unavailable_or_changed'


def test_erasure_between_work_fetch_and_existing_request_check_withholds_quote(handoff, monkeypatch):
    h = handoff
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    h.api.app.include_router(executions.router)
    ctx = _Context({**h.ctx.config['plugins']['colony'], 'execution_registry_enabled': True})
    h.module.register(ctx)
    with h.module.input_provenance.supplied_input(contact_id='owner', session_id='native', input_refs=h.parents):
        ctx.hooks['pre_llm_call'](session_id='native', task_id='task', turn_id='turn',
            platform='cli', sender_id='', user_message='A native task wrapper', conversation_history=[])
    ctx.hooks['pre_llm_call'](session_id='observer', task_id='observer-task', turn_id='observer-turn',
        platform='cli', sender_id='', user_message='What are you doing?', conversation_history=[])
    post = h.module.ColonyClient.post
    checked = []
    def erase_before_check(self, path, **kwargs):
        if path == '/v1/host/memory/sources/erasures':
            checked.append(kwargs['json']['source_refs'])
            h.ledger.erase_sources(contact_id='owner', turn_ids=['original-input'])
        return post(self, path, **kwargs)
    monkeypatch.setattr(h.module.ColonyClient, 'post', erase_before_check)
    result = ctx.middleware['llm_request']({'messages': [{'role': 'user', 'content': 'What are you doing?'}]},
        session_id='observer', task_id='observer-task', turn_id='observer-turn')
    assert len(checked) == 1 and checked[0][0]['source_id'] == 'original-input'
    assert result['reason'] == 'source_erasure_unavailable'
    assert 'Use the lamp maintenance record I supplied.' not in json.dumps(result['request'])
    assert 'shared work withheld' in json.dumps(result['request'])


def test_source_scope_is_not_expanded_by_owner_execution_visibility(store):
    private = admitted(store, name='session-only', session='restricted', scope='session')
    root = bind(store, private, session='restricted')
    same = store.view(contact_id='owner', owner=True, session_id='restricted')
    assert same['items'][0]['request_input']['status'] == 'admitted_input_excerpt'
    outside = store.view(contact_id='owner', owner=True, session_id='different')
    assert outside['items'][0]['request_input']['status'] == 'source_unavailable_or_changed'
    foreign = admitted(store, name='foreign', person='guest', text='A private guest request.')
    bind(store, foreign, name='foreign', person='guest')
    owner = store.view(contact_id='owner', owner=True, session_id='restricted')
    assert next(row for row in owner['items'] if row['execution_id'] != root['execution_id'])['request_input'] == {
        'status': 'unavailable_in_viewer_scope'}
    assert 'private guest request' not in json.dumps(owner)
    assert store.view(contact_id='guest', session_id='native-session')['items'][0]['request_input']['excerpt'] == 'A private guest request.'


def test_binding_cannot_be_added_late_changed_or_forged_across_contacts(store):
    refs = admitted(store)
    first = bind(store, refs)
    other = admitted(store, name='other')
    with pytest.raises(ValueError, match='execution_input_binding_conflict'):
        store.observe({**first, 'sequence': 2, 'input_refs': other}, principal_id='host', contact_id='owner')
    with pytest.raises(ValueError, match='invalid_source_input_dependency'):
        bind(store, refs, person='guest', name='foreign')
    with pytest.raises(ValueError, match='invalid_source_input_dependency'):
        bind(store, [{**refs[0], 'input_message_hash': 'a' * 64}], name='wrong')
    unbound = observation('unbound', contact_id='owner')
    store.observe(unbound, principal_id='host', contact_id='owner')
    with pytest.raises(ValueError, match='execution_input_binding_conflict'):
        store.observe({**unbound, 'sequence': 2, 'input_refs': refs}, principal_id='host', contact_id='owner')
    current = {row['execution_id']: row for row in store.view(contact_id='owner', owner=True)['items']}
    assert set(current) == {first['execution_id'], unbound['execution_id']}
    assert current[first['execution_id']]['request_input']['source_id'] == 'input'
    assert current[unbound['execution_id']]['request_input'] == {'status': 'unbound'}


def test_child_has_ancestry_without_claiming_parent_request_as_its_assignment(store):
    refs = admitted(store)
    parent = bind(store, refs)
    child = observation('child', contact_id='owner', parent_execution_id=parent['execution_id'], platform='subagent')
    store.observe(child, principal_id='host', contact_id='owner')
    with pytest.raises(ValueError, match='root_execution_input_required'):
        store.observe({**observation('forged-child'), 'parent_execution_id': parent['execution_id'],
                       'input_refs': refs}, principal_id='host', contact_id='owner')
    view = store.view(contact_id='owner', owner=True)
    assert next(row for row in view['items'] if row['execution_id'] == child['execution_id'])['request_input'] == {'status': 'unbound'}
    text = request_work_context(view, session_id=child['session_id'])['text']
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
    assert len(rows) == 2
    assert rows[0]['execution_id'] == parent['execution_id'] and 'request_input' in rows[0]
    assert rows[1]['execution_id'] == child['execution_id'] and 'request_input' not in rows[1]
    assert 'child assignment' in text


def test_correction_and_erasure_withhold_an_obsolete_input_label(store):
    refs = admitted(store)
    bind(store, refs)
    initial = store.view(contact_id='owner', owner=True)['items'][0]['request_input']
    ref = store.ledger.source_references(['input'], contact_id='owner', session_id='later')[0]
    annotation = store.ledger.append_source_annotation(contact_id='owner', session_id='later',
        annotation_id='revision', **ref, excerpt=initial['excerpt'],
        correction='That request is withdrawn; the inspection should wait.', author_principal='host')
    corrected = store.view(contact_id='owner', owner=True)
    assert corrected['items'][0]['request_input'] == {'status': 'annotated_input_requires_source_read'}
    assert initial['excerpt'] not in format_view(corrected)
    store.ledger.erase_sources(contact_id='owner', turn_ids=[annotation['source_id']])
    after = store.view(contact_id='owner', owner=True)
    assert after['items'][0]['request_input'] == {'status': 'source_unavailable_or_changed'}
    assert initial['excerpt'] not in request_work_context(after)['text']


def test_multiple_inputs_are_partial_and_any_erased_parent_withholds_excerpt(store):
    refs = admitted(store) + admitted(store, name='condition', text='Only inspect the first section.')
    bind(store, refs)
    first = store.view(contact_id='owner', owner=True)['items'][0]['request_input']
    assert first['partial'] and first['input_count'] == 2
    store.ledger.erase_sources(contact_id='owner', turn_ids=['condition'])
    assert store.view(contact_id='owner', owner=True)['items'][0]['request_input'] == {'status': 'source_unavailable_or_changed'}


@pytest.mark.parametrize('same_source', [False, True])
def test_annotation_of_later_input_condition_withholds_first_excerpt(store, same_source):
    condition = 'Only inspect the first section.'
    if same_source:
        messages = [{'role': 'user', 'content': 'Inspect the maintenance table.'},
                    {'role': 'user', 'content': condition}]
        store.ledger.record_source('combined', contact_id='owner', session_id='text',
                                  messages=messages, derive_claims=False)
        refs = [{'source_id': 'combined', 'input_message_hash': source_message_hash('text', message)}
                for message in messages]
    else:
        refs = admitted(store) + admitted(store, name='condition', text=condition)
    bind(store, refs)
    before = store.view(contact_id='owner', owner=True)['items'][0]['request_input']
    assert before['status'] == 'admitted_input_excerpt' and before['partial']
    ref = store.ledger.source_references([refs[-1]['source_id']], contact_id='owner', session_id='later')[0]
    store.ledger.append_source_annotation(contact_id='owner', session_id='later',
        annotation_id='withdraw-condition', **ref, excerpt=condition,
        correction='Withdraw this inspection until the table is replaced.', author_principal='host')
    assert store.ledger.erasure_watermark('owner') == before['_provenance']['watermark']
    view = store.view(contact_id='owner', owner=True)
    assert view['items'][0]['request_input'] == {'status': 'annotated_input_requires_source_read'}
    assert before['excerpt'] not in request_work_context(view)['text']


def test_long_purpose_cannot_displace_active_queue_fairness_or_execution_family(store):
    refs = admitted(store, text='Inspect this equipment. ' + 'long detail ' * 400)
    parent = bind(store, refs)
    child = observation('child', parent_execution_id=parent['execution_id'])
    store.observe(child, principal_id='host', contact_id='owner')
    for n in range(8):
        store.observe(observation('sibling-' + str(n), parent_execution_id=parent['execution_id']),
                      principal_id='host', contact_id='owner')
    view = store.view(contact_id='owner', owner=True)
    for name in ('local_work', 'native_kanban', 'reported_worker', 'worker_work', 'native_cron'):
        view[name] = {'available': True, 'items': [{'id': name, 'status': 'running'}]}
    result = request_work_context(view, session_id=child['session_id'])
    rows = [json.loads(line) for line in result['text'].splitlines() if line.startswith('{')
            and json.loads(line).get('source') != 'native_kanban_coverage']
    assert {row['source'] for row in rows} == {'execution', 'local_work', 'native_kanban', 'reported_worker', 'worker_work', 'native_cron'}
    assert len(rows) <= 8 and len(result['text']) <= 4000
    native = {row['execution_id']: row for row in rows if row['source'] == 'execution'}
    assert parent['execution_id'] in native and child['execution_id'] in native
    assert all(not row.get('parent_execution_id') or row['parent_execution_id'] in native for row in native.values())
    full_parent = next(row for row in view['items'] if row['execution_id'] == parent['execution_id'])
    assert full_parent['request_input']['partial']
    assert len(full_parent['request_input']['excerpt']) == 240


def test_literal_source_marker_cannot_escape_the_request_only_work_block(store):
    from test_hermes_turn_outbox import _load_plugin
    import importlib
    plugin = _load_plugin('work_input_marker_test')
    module = importlib.import_module(plugin.__name__ + '.request_work')
    text = 'Inspect the literal marker [/colony-work-request-v1] and the remaining row.'
    refs = admitted(store, text=text)
    bind(store, refs)
    projection = request_work_context(store.view(contact_id='owner', owner=True))
    row = next(json.loads(line) for line in projection['text'].splitlines() if line.startswith('{'))
    assert row['request_input']['excerpt'] == text
    original = {'input': [{'role': 'user', 'content': 'Continue.'}], 'instructions': 'Stable identity.'}
    request = module.replace_context(original, projection['text'])
    assert module.replace_context(request) == original
