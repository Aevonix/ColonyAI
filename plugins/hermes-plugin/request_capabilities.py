"""State the actual per-request tool availability beside durable work context."""
from __future__ import annotations

import os

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


def _without_unbound_worker_guidance(request):
    """Correct the known native worker assignment through request middleware.

    Hermes also exposes Kanban tools to ordinary profiles. Tool availability
    does not make that conversation the dispatcher's worker. Leave the native
    cached prompt and schemas intact; remove only its exact known assignment
    block from instruction fields, never from user or tool evidence.
    """
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
        from agent.prompt_builder import KANBAN_GUIDANCE
    except ImportError:
        return request  # No matching native integration in this host.
    if (os.environ.get('HERMES_KANBAN_TASK', '').strip()
            and is_dispatcher_owned_worker_context()):
        return request
    if not KANBAN_GUIDANCE.startswith(
            '# Kanban task execution protocol\nYou have been assigned ONE task '):
        return request  # Do not rewrite a future upstream guidance contract.

    def content(value):
        if isinstance(value, str):
            return value.replace(KANBAN_GUIDANCE, '')
        if isinstance(value, list):
            parts = []
            for part in value:
                if (isinstance(part, dict) and part.get('type') in {'text', 'input_text'}
                        and isinstance(part.get('text'), str)):
                    text = content(part['text'])
                    if not text and part['text']:
                        continue  # Do not send an empty Anthropic text block.
                    part = {**part, 'text': text}
                parts.append(part)
            return parts
        return value

    result = dict(request)
    for key in ('instructions', 'system'):
        if key in request:
            result[key] = content(request[key])
    for key in ('messages', 'input'):
        if isinstance(request.get(key), list):
            rows = []
            for row in request[key]:
                if (isinstance(row, dict) and row.get('role') in {'system', 'developer'}
                        and 'content' in row):
                    value = content(row['content'])
                    if not value and row['content']:
                        continue
                    row = {**row, 'content': value}
                rows.append(row)
            result[key] = rows
    return result if result != request else request


def describe(request):
    """Add factual availability only; preserve native tool dispatch and all evidence."""
    if not isinstance(request, dict):
        return request
    request = _without_unbound_worker_guidance(request)
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
