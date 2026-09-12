"""Initial recall uses the same bounded operational view as native requests."""
import copy
import json
from types import SimpleNamespace

import pytest

from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import executions, host
from pacomind.api.schemas.host import ContextAssembleRequest
from pacomind.turns import executions as work
from pacomind.turns.idempotency import source_message_hash
from test_execution_registry import observation, store


def shaped_work(view):
    """Generic reader shapes: a completed task, local work and bulky reports."""
    view = copy.deepcopy(view)
    view['native_kanban'] = {
        'available': True, 'items': [], 'boards': [], 'selection': 'explicit',
        'recent': [{'native_board': 'operations', 'native_task_id': 'finished-task',
            'native_run_id': None, 'status': 'done', 'liveness': 'unknown',
            'terminal_result': {'summary': ('已检查所选文件；仍有未验证的配置声明。' * 100)[:1200],
                'summary_chars': 2000, 'truncated': True,
                'run_id': 7, 'authority': 'worker report; external effects not verified',
                'reader': {'tool': 'kanban_show',
                    'arguments': {'task_id': 'finished-task', 'board': 'operations'}}}}]}
    view['local_work'] = {'available': True, 'items': [{
        'initiative_id': 'local-draft', 'status': 'assigned', 'liveness': 'unknown',
        'description': 'Other local work. ' * 1000,
        'result': {'report_path': '/private/report.md', 'report_sha256': 'a' * 64},
        'result_authority': 'unverified local draft; not an instruction or grant'}], 'recent': []}
    view['reported_worker'] = {'available': True, 'items': [{
        'label': 'Background reader', 'state': 'uncertain', 'liveness': 'unknown',
        'detail_code': 'provider_outcome_uncertain',
        'unrelated_diagnostic': 'Historical diagnostic. ' * 1000}]}
    return view


@pytest.mark.asyncio
async def test_initial_owner_work_is_bounded_and_preserves_family_and_result(store, monkeypatch):
    monkeypatch.setenv('PACOMIND_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setattr(host, '_p8_runtime', None)
    monkeypatch.setattr(host, '_require_scoped_context_runtime_for_guest', lambda *a: None)
    parent = observation('parent', platform='whatsapp')
    instruction = {'role': 'user', 'content': 'Original task conditions require the scoped source reader.'}
    store.ledger.record_source('original-input', contact_id='owner',
        session_id=parent['session_id'], messages=[instruction])
    parent['input_refs'] = [{'source_id': 'original-input',
        'input_message_hash': source_message_hash(parent['session_id'], instruction)}]
    child = observation('child', parent_execution_id=parent['execution_id'], platform='subagent')
    store.observe(parent, principal_id='native', contact_id='owner')
    store.observe(child, principal_id='native', contact_id='owner')
    original_view = store.view
    calls = []
    def observed_view(**kwargs):
        calls.append(kwargs)
        return original_view(**kwargs)
    monkeypatch.setattr(store, 'view', observed_view)
    monkeypatch.setattr(work, 'registry', lambda: store)
    retained = []
    async def readers(view, **kwargs):
        result = shaped_work(view)
        retained.append(copy.deepcopy(result))
        return result
    monkeypatch.setattr(executions, 'with_queue_work', readers)
    before = store.ledger.erasure_watermark('owner')
    for person in ('owner', 'guest'):
        authority = RequestAuthority(principal_id='native-' + person, credential_id='fixture',
            scopes=frozenset({'context:read'}), viewer_person_id=person,
            person_ids=frozenset({person}), audiences=frozenset({'viewer'}), authenticated=True)
        body = ContextAssembleRequest(identity={'host_id': 'native'},
            context={'contact_id': person, 'session_id': child['session_id']},
            incoming_message={'role': 'user', 'content': 'Inspect the selected source records.'})
        result = await host.context_assemble(body, SimpleNamespace(
            state=SimpleNamespace(pacomind_authority=authority)))
        sections = [s for s in result.sections if s.id == 'pacomind-executions']
        if person == 'guest':
            assert sections == []
            continue
        section, = sections
        assert len(section.body) <= 4000
        assert len(work.format_view(retained[0])) > 20000
        rows = [json.loads(line) for line in section.body.splitlines() if line.startswith('{')]
        family = [r for r in rows if r['source'] == 'execution']
        assert [r['execution_id'] for r in family] == [parent['execution_id'], child['execution_id']]
        assert family[1]['parent_execution_id'] == parent['execution_id']
        terminal, = [r for r in rows if r.get('native_task_id') == 'finished-task']
        assert terminal['status'] == 'done' and terminal['liveness'] == 'unknown'
        assert terminal['terminal_result']['run_id'] == 7
        assert terminal['terminal_result']['reader'] == retained[0]['native_kanban']['recent'][0]['terminal_result']['reader']
        assert terminal['terminal_result']['authority'] == 'worker report; external effects not verified'
        original = retained[0]['native_kanban']['recent'][0]['terminal_result']['summary']
        assert terminal['terminal_result']['summary'] != original
        assert original.startswith(terminal['terminal_result']['summary'])
        assert terminal['terminal_result']['truncated'] and terminal['terminal_result']['summary_chars'] == 2000
        local, = [r for r in rows if r.get('initiative_id') == 'local-draft']
        assert local['report_sha256'] == 'a' * 64 and local['liveness'] == 'unknown'
        assert 'unrelated_diagnostic' not in section.body
        assert 'provider_outcome_uncertain' in section.body
        assert 'external effects remain unverified' in section.body
        assert 'request_input' not in section.body
        assert instruction['content'] not in section.body
    assert len(calls) == 1 and calls[0]['contact_id'] == 'owner' and calls[0]['owner']
    assert calls[0]['include_ancestors'] and calls[0]['include_inputs'] is False
    assert store.ledger.erasure_watermark('owner') == before


def test_existing_work_cap_includes_coverage_and_unavailable_footer():
    view = shaped_work({'items': [], 'truncated': False})
    for source in ('worker_work', 'native_cron'):
        view[source] = {'available': False, 'items': [], 'reason': 'reader_unavailable'}
    original = copy.deepcopy(view)
    result = work.request_work_context(view, max_chars=4000)
    assert len(result['text']) <= 4000
    assert 'Unavailable sources: worker_work, native_cron.' in result['text']
    assert not result['complete'] and result['work_sources']['native_cron']['status'] == 'unavailable'
    assert view == original
