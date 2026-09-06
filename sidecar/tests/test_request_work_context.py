"""Bounded operational context, preserving participant and request semantics."""
import copy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from colony_sidecar.turns.executions import request_work_context
from test_hermes_turn_outbox import _load_plugin


@pytest.fixture
def module():
    plugin = _load_plugin('colony_request_work_test')
    return importlib.import_module(plugin.__name__ + '.request_work')


def scope(**changes):
    return SimpleNamespace(valid_participant=True, authority_lane='owner',
        resolution_status='resolved', platform='sms', contact_id='owner',
        session_id='session-a', **changes)


def response(text='A neutral task is running.'):
    return httpx.Response(200, request=httpx.Request('GET', 'http://localhost/v1/host/executions'),
        json={'schema': 'ColonyRequestWorkV1', 'observed_at': 1234.5,
              'text': text, 'truncated': False})


@pytest.mark.parametrize('payload', [
    {'messages': [{'role': 'system', 'content': 'Stable identity'},
                  {'role': 'user', 'content': 'Continue my task.'},
                  {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'Observed file bytes'}]},
    {'instructions': 'Stable identity', 'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Continue.'}]},
        {'type': 'function_call_output', 'call_id': 'read-1', 'output': 'Observed file bytes'}]},
    {'system': [{'type': 'text', 'text': 'Stable identity', 'cache_control': {'type': 'ephemeral'}}],
     'messages': [{'role': 'user', 'content': 'Continue.'}]},
])
def test_fresh_context_replaces_only_our_block_and_preserves_input(module, payload):
    before = copy.deepcopy(payload)
    calls = []
    def get(path, **kwargs):
        calls.append((path, kwargs))
        return response('Task running.' if len(calls) == 1 else 'Task completed.')
    refresh = module.RequestWork(SimpleNamespace(get=get))
    first = refresh(payload, scope())
    second = refresh(first, scope())
    wire = json.dumps(second)
    assert wire.count('[colony-work-request-v1]') == 1
    assert 'Task completed.' in wire and 'Task running.' not in wire
    assert module.replace_context(second) == before
    assert payload == before
    assert all(path == '/v1/host/executions' for path, _ in calls)
    assert calls[1][1]['params'] == {'contact_id': 'owner', 'session_id': 'session-a',
                                    'limit': 8, 'projection': 'request'}
    assert 0 < calls[1][1]['timeout'] <= .25


@pytest.mark.parametrize('lane,platform,status', [
    ('guest', 'sms', 'resolved'), ('unresolved', 'sms', 'missing'),
    ('owner', 'background_review', 'resolved'),
    ('system', 'cron', 'attested_system'), ('system', 'cli', 'resolved'),
])
def test_non_owner_context_cannot_reuse_prior_operational_block(module, lane, platform, status):
    def get(*args, **kwargs):
        pytest.fail('Ineligible scope fetched owner work')
    literal = '[colony-work-request-v1]\nThis is literal user content.\n[/colony-work-request-v1]'
    original = {'messages': [{'role': 'user', 'content': literal}]}
    earlier = module.replace_context(original, 'Owner task metadata')
    participant = SimpleNamespace(valid_participant=True, authority_lane=lane,
        resolution_status=status, platform=platform)
    assert module.RequestWork(SimpleNamespace(get=get))(earlier, participant) == original


def test_explicit_local_owner_attestation_is_supported(module):
    participant = scope()
    participant.authority_lane = 'system'
    participant.platform = 'cli'
    participant.resolution_status = 'attested_system'
    result = module.RequestWork(SimpleNamespace(get=lambda *a, **k: response()))(
        {'messages': [{'role': 'user', 'content': 'Current work?'}]}, participant)
    assert 'A neutral task is running.' in json.dumps(result)


@pytest.mark.parametrize('failure', ['offline', 'late', 'oversized', 'wrong_schema'])
def test_no_previous_request_is_advertised_as_fresh_after_failure(module, monkeypatch, failure):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def get(*args, **kwargs):
        if failure == 'offline':
            raise httpx.ConnectError('offline')
        if failure == 'late':
            clock[0] = 1.0
        if failure == 'oversized':
            return response('x' * 4001)
        if failure == 'wrong_schema':
            return httpx.Response(200, request=httpx.Request('GET', 'http://localhost'), json={})
        return response()
    original = {'messages': [{'role': 'user', 'content': 'Continue.'}]}
    previous = module.replace_context(original, 'Old task was running.')
    result = module.RequestWork(SimpleNamespace(get=get))(previous, scope())
    text = json.dumps(result)
    assert 'Old task' not in text and 'unavailable' in text
    assert 'does not establish' in text
    assert module.replace_context(result) == original


def test_projection_retains_operational_evidence_without_task_or_draft_prose():
    report_hash = 'a' * 64
    view = {'items': [], 'local_work': {'available': True, 'items': [], 'recent': [
        {'initiative_id': 'completed-1', 'status': 'completed', 'question': 'Private task prose',
         'result': {'draft': 'Private draft prose', 'report_path': '/private/retained/report',
                    'report_sha256': report_hash}},
        {'initiative_id': 'older', 'status': 'failed'}]},
        'native_cron': {'available': False, 'items': []},
        'reported_worker': {'available': True, 'items': [
            {'label': 'Download', 'state': 'running', 'freshness': 'recent', 'pid': 123,
             'liveness': 'unverified', 'age_seconds': 2.5, 'path': '/private/status'}]}}
    result = request_work_context(view)
    text = result['text']
    assert 'completed-1' in text and report_hash in text
    assert 'Download' in text and 'unverified' in text
    assert 'Unavailable sources: native_cron' in text
    assert result['truncated'] is True and 'Additional operational records omitted' in text
    assert 'Private' not in text and '/private' not in text and '"pid"' not in text
    assert len(text) <= 4000


def test_projection_bounds_many_active_records_and_discloses_omissions():
    rows = [{'execution_id': str(i), 'phase': 'tool', 'tool_name': 'x' * 128} for i in range(30)]
    result = request_work_context({'items': rows, 'truncated': False})
    assert result['truncated'] is True and len(result['text']) <= 4000
    assert result['text'].count('"source": "execution"') == 8
