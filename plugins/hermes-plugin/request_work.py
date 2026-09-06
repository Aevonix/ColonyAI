"""Refresh operational context at the native model-request boundary.

This reads the existing work view. It does not repeat memory recall or write
work, conversation history, commitments or source evidence.
"""
from __future__ import annotations

import math
import re
import time


_OPEN = '[colony-work-request-v1]'
_CLOSE = '[/colony-work-request-v1]'
_BLOCK = re.compile(r'(?:\n\n)?\[colony-work-request-v1\].*?\[/colony-work-request-v1\]', re.S)
_UNAVAILABLE = ('Current shared work is unavailable for this model request. '
                'The turn-start snapshot may be stale; this does not establish '
                'that previously observed work has stopped.')


def _owned_message(row):
    return (isinstance(row, dict) and row.get('role') in ('system', 'developer')
            and isinstance(row.get('content'), str)
            and row['content'].startswith(_OPEN + '\n')
            and row['content'].endswith('\n' + _CLOSE))


def replace_context(request, text=None, *, api_mode=''):
    """Replace our request-only block without changing user or tool content."""
    result = dict(request)
    for name in ('messages', 'input'):
        if isinstance(request.get(name), list):
            result[name] = [row for row in request[name] if not _owned_message(row)]
    for name in ('instructions', 'system'):
        value = request.get(name)
        if isinstance(value, str):
            result[name] = _BLOCK.sub('', value)
        elif name == 'system' and isinstance(value, list):
            result[name] = [row for row in value if not (
                isinstance(row, dict) and row.get('type') == 'text'
                and isinstance(row.get('text'), str)
                and row['text'].startswith(_OPEN + '\n')
                and row['text'].endswith('\n' + _CLOSE))]
    if text is None:
        return result
    block = _OPEN + '\n' + text + '\n' + _CLOSE
    if 'input' in result:
        instructions = result.get('instructions') or ''
        if isinstance(instructions, str):
            result['instructions'] = instructions + ('\n\n' if instructions else '') + block
    elif isinstance(result.get('system'), str):
        result['system'] += ('\n\n' if result['system'] else '') + block
    elif isinstance(result.get('system'), list):
        result['system'].append({'type': 'text', 'text': block})
    elif api_mode == 'anthropic_messages':
        # Native Anthropic kwargs omit system entirely when it is empty.
        result['system'] = block
    elif isinstance(result.get('messages'), list):
        messages = result['messages']
        # Hermes has already converted the leading system role for models
        # that use developer instructions before invoking request middleware.
        role = ('developer' if messages and isinstance(messages[0], dict)
                and messages[0].get('role') == 'developer' else 'system')
        messages.append({'role': role, 'content': block})
    return result


class RequestWork:
    def __init__(self, client):
        self.client = client

    def __call__(self, request, scope, *, api_mode=''):
        if (scope is None or not scope.valid_participant
                or not (scope.authority_lane == 'owner'
                        or (scope.authority_lane == 'system'
                            and scope.resolution_status == 'attested_system'))
                or scope.platform in ('cron', 'background_review')):
            return replace_context(request, api_mode=api_mode)
        text = _UNAVAILABLE
        deadline = time.monotonic() + .25
        try:
            response = self.client.get("/v1/host/executions",
                params={'contact_id': scope.contact_id, 'session_id': scope.session_id,
                        'limit': 8, 'projection': 'request'},
                timeout=.25, _deadline_monotonic=deadline)
            response.raise_for_status()
            value = response.json()
            observed = value.get('observed_at')
            if (value.get('schema') != 'ColonyRequestWorkV1'
                    or not isinstance(value.get('text'), str)
                    or not 1 <= len(value['text']) <= 4000
                    or type(observed) not in (int, float) or not math.isfinite(observed)
                    or time.monotonic() > deadline):
                raise ValueError('Invalid or late operational view')
            text = f"Observed at {observed:.3f}.\n" + value['text']
        except Exception:
            # A temporary work-service failure must not stall a conversation
            # or advertise the previous request's operational state as fresh.
            pass
        return replace_context(request, text, api_mode=api_mode)
