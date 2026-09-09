"""Availability facts must not erase evidence or disable real native tools."""
import copy
import importlib.util
from pathlib import Path


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
