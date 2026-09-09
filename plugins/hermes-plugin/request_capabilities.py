"""State the actual per-request tool availability beside durable work context."""
from __future__ import annotations

_NO_NEW_TOOLS = (
    '[Current request capabilities]\n'
    'No new tool calls are available in this request. Answer from the supplied '
    'evidence when possible. You may describe work already supported by actual '
    'tool results or accepted task records, but cannot start a new lookup or '
    'action here. If the request needs an unavailable tool, explain that limitation. '
    'Do not announce that you are checking or requesting it, simulate a tool call '
    'in text, or invent a result. This does not prevent answering ordinary questions '
    'from the supplied information.'
)


def describe(request):
    """Add factual availability only; preserve native tool dispatch and all evidence."""
    if not isinstance(request, dict):
        return request
    available = request.get('tools') or request.get('functions')
    if available and request.get('tool_choice') != 'none' and request.get('function_call') != 'none':
        return request
    result = dict(request)
    if isinstance(request.get('instructions'), str):
        if _NO_NEW_TOOLS not in request['instructions']:
            result['instructions'] = request['instructions'] + '\n\n' + _NO_NEW_TOOLS
    else:
        key = 'messages' if isinstance(request.get('messages'), list) else 'input'
        if not isinstance(request.get(key), list):
            return request
        rows = request[key]
        if not any(isinstance(row, dict) and row.get('role') in {'system', 'developer'}
                   and row.get('content') == _NO_NEW_TOOLS for row in rows):
            # The original native system/SOUL and the current input stay intact.
            result[key] = [{'role': 'system', 'content': _NO_NEW_TOOLS}, *rows]
    return result
