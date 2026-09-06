"""Link native review proposals to tool failures already retained by Hermes.

The existing request middleware supplies a trusted participant and native
request. ContextVars follow Hermes' review fork; no second transcript, queue,
timer or model-authored evidence field is accepted here.
"""
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

_CURRENT = ContextVar('colony_native_review_evidence', default=None)


def capture(scope, request):
    """Remember bounded references from this participant's actual request."""
    _CURRENT.set(None)
    if (scope is None or not scope.valid_participant
            or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'background_review', 'cron', 'subagent'}
            or not scope.session_id or not scope.turn_id or not isinstance(request, dict)):
        return
    messages = request.get('messages', request.get('input', []))
    if not isinstance(messages, list):
        return
    calls, failures = {}, {}
    for message in messages[-256:]:
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                if isinstance(call, dict) and isinstance(call.get('function'), dict):
                    calls[call.get('id')] = call['function'].get('name')
        elif message.get('type') == 'function_call':
            calls[message.get('call_id')] = message.get('name')
        if message.get('role') == 'tool':
            call_id, content = message.get('tool_call_id'), message.get('content')
        elif message.get('type') == 'function_call_output':
            call_id, content = message.get('call_id'), message.get('output')
        else:
            continue
        name = calls.get(call_id)
        if (not isinstance(call_id, str) or not call_id or len(call_id) > 256
                or not isinstance(name, str) or not name or len(name) > 128
                or not isinstance(content, str) or len(content.encode()) > 65536):
            continue
        try:
            result = json.loads(content)
        except (ValueError, RecursionError):
            continue
        if not isinstance(result, dict) or not result.get('error'):
            continue
        error = result['error']
        classification = ('unsupported_regex_features' if isinstance(error, str)
            and 'regex parse error' in error.lower() and 'not supported' in error.lower()
            else 'tool_returned_error')
        failures[call_id] = {'tool_call_id': call_id, 'tool_name': name,
            'request_visible_result_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'error_class': classification}
    if failures:
        _CURRENT.set({'version': 1, 'source': 'native_request_tool_results',
            'session_id': scope.session_id, 'turn_id': scope.turn_id,
            'failures': list(failures.values())[-16:]})


def current():
    """Return isolated data for the native pending payload, never raw results."""
    return deepcopy(_CURRENT.get())
