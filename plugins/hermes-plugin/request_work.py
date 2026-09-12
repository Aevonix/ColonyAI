"""Refresh operational context at the native model-request boundary.

This reads the existing work view. It does not repeat memory recall or write
work, conversation history, commitments or source evidence.
"""
from __future__ import annotations

import json
import math
import re
import time


_OPEN = '[pacomind-work-request-v1]'
_CLOSE = '[/pacomind-work-request-v1]'
_BLOCK = re.compile(r'(?:\n\n)?\[pacomind-work-request-v1\].*?\[/pacomind-work-request-v1\]', re.S)
_UNAVAILABLE = ('Current shared work is unavailable for this model request. '
                'The turn-start snapshot may be stale; this does not establish '
                'that previously observed work has stopped.')


def _owned_message(row, opening=_OPEN, closing=_CLOSE):
    return (isinstance(row, dict) and row.get('role') in ('system', 'developer')
            and isinstance(row.get('content'), str)
            and row['content'].startswith(opening + '\n')
            and row['content'].endswith('\n' + closing))


def replace_context(request, text=None, *, api_mode='', marker='pacomind-work-request-v1'):
    """Replace our request-only block without changing user or tool content."""
    result = dict(request)
    opening, closing = '[' + marker + ']', '[/' + marker + ']'
    pattern = _BLOCK if opening == _OPEN else re.compile(
        r'(?:\n\n)?' + re.escape(opening) + '.*?' + re.escape(closing), re.S)
    for name in ('messages', 'input'):
        if isinstance(request.get(name), list):
            result[name] = [row for row in request[name] if not _owned_message(row, opening, closing)]
    for name in ('instructions', 'system'):
        value = request.get(name)
        if isinstance(value, str):
            result[name] = pattern.sub('', value)
        elif name == 'system' and isinstance(value, list):
            result[name] = [row for row in value if not (
                isinstance(row, dict) and row.get('type') == 'text'
                and isinstance(row.get('text'), str)
                and row['text'].startswith(opening + '\n')
                and row['text'].endswith('\n' + closing))]
    if text is None:
        return result
    block = opening + '\n' + text + '\n' + closing
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
        return self.prepare(request, scope, api_mode=api_mode)[0]

    def prepare(self, request, scope, *, api_mode=''):
        """Return an authentic request-only block and its optional input lineage.

        The registered adapter passes that lineage to its existing source
        freshness check before dispatch. No text marker nominates a source.
        """
        if (scope is None or not scope.valid_participant
                or not (scope.authority_lane == 'owner'
                        or (scope.authority_lane == 'system'
                            and scope.resolution_status == 'attested_system'))
                or scope.platform in ('cron', 'background_review')):
            return replace_context(request, api_mode=api_mode), None
        session = json.dumps(scope.session_id, ensure_ascii=True).replace(
            _CLOSE, r'\u005b/pacomind-work-request-v1\u005d')
        identity = f'Current request session: {session}.\n'
        text = identity + _UNAVAILABLE
        provenance = None
        deadline = time.monotonic() + .25
        try:
            response = self.client.get("/v1/host/executions",
                params={'contact_id': scope.contact_id, 'session_id': scope.session_id,
                        'limit': 8, 'projection': 'request', 'input_context': True},
                timeout=.25, _deadline_monotonic=deadline)
            response.raise_for_status()
            value = response.json()
            observed = value.get('observed_at')
            if (value.get('schema') != 'PacoMindRequestWorkV1'
                    or not isinstance(value.get('text'), str)
                    or not 1 <= len(value['text']) <= 4000
                    or type(observed) not in (int, float) or not math.isfinite(observed)
                    or time.monotonic() > deadline):
                raise ValueError('Invalid or late operational view')
            text = identity + f"Observed at {observed:.3f}.\n" + value['text']
            supplied = value.get('input_provenance')
            if supplied is not None:
                refs = supplied.get('source_refs')
                input_refs = supplied.get('unannotated_input_refs')
                if (supplied.get('contact_id') != scope.contact_id
                        or type(supplied.get('watermark')) is not int or supplied['watermark'] < 0
                        or not isinstance(refs, list) or not 1 <= len(refs) <= 512
                        or any(not isinstance(ref, dict) or set(ref) != {'source_id', 'source_version'}
                            or not isinstance(ref['source_id'], str) or not 1 <= len(ref['source_id']) <= 256
                            or not isinstance(ref['source_version'], str)
                            or not re.fullmatch('[a-f0-9]{64}', ref['source_version']) for ref in refs)):
                    raise ValueError('Invalid operational input provenance')
                if (not isinstance(input_refs, list) or not 1 <= len(input_refs) <= 512
                        or any(not isinstance(ref, dict) or set(ref) != {'source_id', 'input_message_hash'}
                            or ref['source_id'] not in {source['source_id'] for source in refs}
                            or not isinstance(ref['input_message_hash'], str)
                            or not re.fullmatch('[a-f0-9]{64}', ref['input_message_hash']) for ref in input_refs)):
                    raise ValueError('Operational input requires current annotation checks')
                provenance = {**supplied, 'text': _OPEN + '\n' + text + '\n' + _CLOSE}
        except Exception:
            # A temporary work-service failure must not stall a conversation
            # or advertise the previous request's operational state as fresh.
            text, provenance = identity + _UNAVAILABLE, None
        return replace_context(request, text, api_mode=api_mode), provenance
