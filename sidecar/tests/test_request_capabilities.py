"""Availability facts must not erase evidence or disable real native tools."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


path = Path(__file__).resolve().parents[2]/'plugins/hermes-plugin/request_capabilities.py'
spec = importlib.util.spec_from_file_location('tested_request_capabilities', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_unavailable_tools_explain_limit_without_mutating_sources_or_input():
    request = {'messages': [{'role': 'system', 'content': 'Full identity and evidence rules'},
        {'role': 'tool', 'tool_call_id': 'prior', 'content': 'The previous task completed.'},
        {'role': 'user', 'content': 'What happened?'}]}
    original = copy.deepcopy(request)
    result = module.describe(request)
    assert request == original and result['messages'][1:] == original['messages']
    assert 'accepted task records' in result['messages'][0]['content']
    assert module.describe(result) == result


def test_native_discovery_tools_and_direct_tools_remain_unmodified():
    for name in ('read_file', 'tool_call', 'tool_search'):
        request = {'messages': [{'role': 'user', 'content': 'Inspect it'}],
                   'tools': [{'type': 'function', 'function': {'name': name}}]}
        assert module.describe(request) is request


def test_forced_final_answer_can_use_prior_receipt_but_not_new_calls():
    request = {'messages': [{'role': 'tool', 'content': 'completed'}],
               'tools': [{'type': 'function', 'function': {'name': 'read_file'}}], 'tool_choice': 'none'}
    result = module.describe(request)
    assert result['messages'][1:] == request['messages']
    assert result['tools'] == request['tools'] and result['tool_choice'] == 'none'


def test_responses_instructions_remain_complete_and_idempotent():
    request = {'instructions': 'Full trusted instructions <memory-context>example</memory-context>',
               'input': [{'role': 'user', 'content': 'Use the evidence'}], 'tools': []}
    result = module.describe(request)
    assert result['instructions'].startswith(request['instructions'] + '\n\n')
    assert result['input'] is request['input'] and module.describe(result) == result


def test_user_cannot_disable_availability_note_with_a_matching_quote():
    request = {'messages': [{'role': 'user', 'content': module._NO_NEW_TOOLS}]}
    assert module.describe(request)['messages'][0]['role'] == 'system'


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
def test_only_native_assignment_instruction_is_removed_and_evidence_is_unchanged(monkeypatch, shape):
    guidance = '# Kanban task execution protocol\nYou have been assigned ONE task from the shared board.'
    monkeypatch.setitem(sys.modules, 'agent.prompt_builder', SimpleNamespace(KANBAN_GUIDANCE=guidance))
    monkeypatch.setitem(sys.modules, 'agent.delegation_context',
                        SimpleNamespace(is_dispatcher_owned_worker_context=lambda: True))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    evidence = [{'role': 'user', 'content': guidance},
                {'role': 'tool', 'tool_call_id': 'receipt', 'content': guidance}]
    instructions = 'Stable identity.\n' + guidance + '\nOther evidence rules.'
    request = {'tools': [{'type': 'function', 'function': {'name': 'kanban_show'}}]}
    if shape == 'chat':
        request['messages'] = [{'role': 'developer', 'content': instructions}, *evidence]
    elif shape == 'responses':
        request.update(instructions=instructions, input=evidence)
    else:
        request.update(system=[{'type': 'text', 'text': instructions,
                                'cache_control': {'type': 'ephemeral'}}], messages=evidence)
    before = copy.deepcopy(request)
    result = module.describe(request)
    assert request == before and result['tools'] == before['tools']
    if shape == 'chat':
        assert result['messages'][0]['content'] == instructions.replace(guidance, '')
        assert result['messages'][1:] == evidence
    elif shape == 'responses':
        assert result['instructions'] == instructions.replace(guidance, '')
        assert result['input'] == evidence
    else:
        assert result['system'][0] == {**request['system'][0], 'text': instructions.replace(guidance, '')}
        assert result['messages'] == evidence
    assert module.describe(result) == result
    isolated = {'system': [{'type': 'text', 'text': guidance},
                          {'type': 'text', 'text': 'Stable identity'}],
                'messages': [{'role': 'developer', 'content': guidance}, *evidence],
                'tools': request['tools']}
    cleaned = module.describe(isolated)
    assert cleaned['system'] == [{'type': 'text', 'text': 'Stable identity'}]
    assert cleaned['messages'] == evidence
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'actual-worker-binding')
    assert module.describe(request) is request


def test_changed_upstream_guidance_is_not_rewritten(monkeypatch):
    guidance = 'Future upstream guidance with a different ownership contract.'
    monkeypatch.setitem(sys.modules, 'agent.prompt_builder', SimpleNamespace(KANBAN_GUIDANCE=guidance))
    monkeypatch.setitem(sys.modules, 'agent.delegation_context',
                        SimpleNamespace(is_dispatcher_owned_worker_context=lambda: False))
    request = {'instructions': guidance, 'tools': [{'type': 'function', 'function': {'name': 'kanban_show'}}]}
    assert module.describe(request) is request
