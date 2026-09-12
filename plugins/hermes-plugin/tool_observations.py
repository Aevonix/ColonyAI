"""Nominate a useful completed call; retain its actual bounded native original."""
from collections import OrderedDict
from contextlib import closing
import copy
import hashlib
import json
import math
import sqlite3
import threading
import time

from .followups import capture_instruction
from .naming import operation
from .request_work import replace_context

MAX_BYTES = 16384
_EXCLUDED = {'session_search', 'colony_memory_retain_observation', 'apsimo_memory_retain_observation'}
_HINT_MARKER = 'apsimo-observation-candidates-v1'
_CATALOG_HEADER = 'Deferred tool catalog (call schemas via `tool_describe`, invoke via `tool_call`):'


def argument_preview(arguments):
    """A display label from execution arguments, never a replacement for evidence."""
    try:
        text = json.dumps(arguments, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return {'arguments_preview': None, 'arguments_truncated': False}
    return {'arguments_preview': text[:128], 'arguments_truncated': len(text) > 128}


def _available_retention(request):
    if (request.get('tool_choice') in ('none', {'type': 'none'})
            or request.get('function_call') == 'none'):
        return None
    schemas = request.get('tools') or request.get('functions') or []
    functions = [tool.get('function', tool) for tool in schemas if isinstance(tool, dict)]
    named = {fn['name']: fn for fn in functions if isinstance(fn, dict) and isinstance(fn.get('name'), str)}
    for name in named:
        if operation(name) == 'colony_memory_retain_observation':
            return name, False
    if not {'tool_search', 'tool_describe', 'tool_call'} <= named.keys():
        return None
    description = named['tool_search'].get('description', '')
    if not isinstance(description, str) or _CATALOG_HEADER not in description:
        return None
    for line in description.split(_CATALOG_HEADER, 1)[1].splitlines():
        names = [line[2:].split(':', 1)[0]] if line.startswith('- ') else line.split(',')
        for name in names:
            name = name.strip()
            if operation(name) == 'colony_memory_retain_observation':
                return name, True
    return None


def _key(scope):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review', 'colony_task'}
            or getattr(scope, 'parent_session_id', '')
            or not isinstance(getattr(scope, 'user_message', None), str)
            or not scope.user_message.strip() or len(scope.user_message) > 32768
            or not all(getattr(scope, name, '') for name in ('contact_id', 'session_id', 'task_id', 'turn_id'))):
        return None
    from .input_provenance import current
    if current() is not None:
        return None
    return scope.contact_id, scope.session_id, scope.task_id, scope.turn_id


def _arguments_hash(arguments):
    try:
        return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return None


def _call_identity(name, arguments):
    """Native dispatch unwraps local calls, while SDK history keeps tool_call.

    Normalization alone grants nothing: checked() still requires the matching
    observed execution arguments, result bytes and current request identity.
    """
    if name != 'tool_call':
        return name, None
    try:
        from tools import tool_search
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(arguments, dict):
            return None
        underlying, selected, error = tool_search.resolve_underlying_call(arguments)
        if error or not underlying or underlying == getattr(tool_search, 'CONNECTOR_BATCH_SENTINEL', None):
            return None
        digest = _arguments_hash(selected)
        return (underlying, digest) if digest is not None else None
    except Exception:
        return None


def _request_results(request):
    calls, results = {}, {}
    def add(rows, call_id, value):
        # A repeated ID cannot disambiguate one actual native completion.
        rows[call_id] = value if call_id not in rows else None
    for message in request.get('messages', request.get('input', [])):
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                if isinstance(call, dict) and isinstance(call.get('function'), dict):
                    add(calls, call.get('id'), _call_identity(call['function'].get('name'),
                                                           call['function'].get('arguments')))
        elif message.get('type') == 'function_call':
            add(calls, message.get('call_id'), _call_identity(message.get('name'), message.get('arguments')))
        if message.get('role') == 'tool':
            add(results, message.get('tool_call_id'), message.get('content'))
        elif message.get('type') == 'function_call_output':
            add(results, message.get('call_id'), message.get('output'))
        if isinstance(message.get('content'), list):
            for block in message['content']:
                if not isinstance(block, dict):
                    continue
                if message.get('role') == 'assistant' and block.get('type') == 'tool_use':
                    add(calls, block.get('id'), _call_identity(block.get('name'), block.get('input')))
                elif message.get('role') == 'user' and block.get('type') == 'tool_result':
                    add(results, block.get('tool_use_id'), block.get('content'))
    return calls, results


def native_original(scope, call_id, expected):
    """Read the exact completed current-session row; never scan other sessions."""
    from hermes_state import SessionDB, _default_db_path
    path = _default_db_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT id,role,content,tool_name,timestamp FROM messages
            WHERE session_id=? AND tool_call_id=? AND role='tool' AND active=1 LIMIT 2''',
            (scope.session_id, call_id)).fetchall()
    if len(rows) != 1:
        raise ValueError('A unique completed native tool message is required')
    row = dict(rows[0])
    content = SessionDB._decode_content(row['content'])
    if (not isinstance(content, str) or len(content.encode()) > MAX_BYTES
            or hashlib.sha256(content.encode()).hexdigest() != expected['sha256']
            or (row['tool_name'] and operation(row['tool_name']) != expected['name'])
            or type(row['timestamp']) not in (int, float) or not math.isfinite(row['timestamp'])
            or row['timestamp'] <= 0):
        raise ValueError('The native original does not match the observed completed call')
    native = {'profile_id': hashlib.sha256(str(path.parent).encode()).hexdigest(),
        'session_id': scope.session_id, 'task_id': scope.task_id, 'turn_id': scope.turn_id,
        'tool_call_id': call_id, 'tool_name': row['tool_name'] or expected['visible_name'], 'message_id': row['id'],
        'api_request_id': expected['api_request_id'],
        'timestamp': row['timestamp'], 'result_sha256': expected['sha256']}
    return content, native


class ToolObservations:
    def __init__(self, client, outbox, request_memory):
        self.client, self.outbox, self.request_memory = client, outbox, request_memory
        self._lock, self._turns = threading.RLock(), OrderedDict()

    def completed(self, scope, context, value, *, arguments=None):
        key, call_id, name = _key(scope), context.get('tool_call_id'), context.get('tool_name')
        request_id = context.get('api_request_id')
        if (key is None or not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
                or not isinstance(call_id, str) or not 1 <= len(call_id) <= 256
                or not isinstance(name, str) or not 1 <= len(name) <= 128 or name in _EXCLUDED
                or not isinstance(value, str) or not value.strip() or len(value.encode()) > MAX_BYTES):
            return
        with self._lock:
            turn = self._turns.setdefault(key, OrderedDict())
            # Retrying the same observed call cannot replace its bytes.
            turn.setdefault(call_id, {'name': name, 'sha256': hashlib.sha256(value.encode()).hexdigest(),
                                       'visible': {}, 'api_request_id': request_id,
                'arguments': argument_preview(arguments),
                'arguments_sha256': _arguments_hash(arguments),
                'input_sha256': hashlib.sha256(scope.user_message.encode()).hexdigest(), 'sources': self.request_memory.supplied_snapshot(scope)})
            while len(turn) > 16:
                turn.popitem(last=False)
            self._turns.move_to_end(key)
            while len(self._turns) > 64:
                self._turns.popitem(last=False)

    def checked(self, request, scope, request_id, *, api_mode=''):
        key = _key(scope)
        if not isinstance(request, dict):
            return request
        request = replace_context(request, api_mode=api_mode, marker=_HINT_MARKER)
        if key is None or not isinstance(request_id, str) or not request_id:
            return request
        calls, results = _request_results(request)
        eligible = []
        with self._lock:
            for call_id, record in self._turns.get(key, {}).items():
                text = results.get(call_id)
                record['visible'].pop(request_id, None)
                identity = calls.get(call_id)
                name, arguments_hash = identity if identity is not None else (None, None)
                if (isinstance(name, str) and operation(name) == record['name']
                        and (arguments_hash is None or arguments_hash == record['arguments_sha256']) and isinstance(text, str)
                        and hashlib.sha256(text.encode()).hexdigest() == record['sha256']):
                    record['visible'][request_id] = name
                    if (record['sources'] is not None
                            and record['input_sha256'] == hashlib.sha256(scope.user_message.encode()).hexdigest()):
                        eligible.append({'call_id': call_id, 'tool_name': name, **record['arguments']})
                while len(record['visible']) > 8:
                    record['visible'].pop(next(iter(record['visible'])))
        available = _available_retention(request)
        if not eligible or not available or self.request_memory.supplied_snapshot(scope) is None:
            return request
        name, deferred = available
        guidance = (f'For durable findings or meaningful outcomes with likely future use, you may retain '
            f'an original tool result using {name}(call_id, reason). Skip incidental output, duplicate '
            'status, transient noise and secrets. These are candidates, not saved memories. '
            'Match the call ID to its execution arguments; truncated previews require checking the original call. ')
        if deferred:
            guidance += f'Load {name} with tool_describe, then invoke it with tool_call. '
        guidance += '\nEligible completed calls in this request: '
        listed = []
        wrapper_chars = len(f'[{_HINT_MARKER}]\n\n[/{_HINT_MARKER}]')
        for item in reversed(eligible):
            encoded = json.dumps([*listed, item], ensure_ascii=True).replace('[/', r'\u005b/')
            if wrapper_chars + len(guidance) + len(encoded) > 2048 or len(listed) == 8:
                break
            listed.append(item)
        if not listed:
            return request
        text = guidance + json.dumps(listed, ensure_ascii=True).replace('[/', r'\u005b/')
        return replace_context(request, text, api_mode=api_mode, marker=_HINT_MARKER)

    def finish(self, scope):
        with self._lock:
            if scope is not None:
                self._turns.pop(tuple(getattr(scope, name, '') for name in
                    ('contact_id', 'session_id', 'task_id', 'turn_id')), None)

    def handle(self, args, scope, context):
        key = _key(scope)
        if (key is None or not isinstance(args, dict) or set(args) != {'call_id', 'reason'}
                or not isinstance(args['call_id'], str) or not 1 <= len(args['call_id']) <= 256
                or not isinstance(args['reason'], str) or not args['reason'].strip() or len(args['reason']) > 512):
            return json.dumps({'accepted': False, 'error': 'Nominate one completed current owner-turn call and a bounded future-use reason'})
        try:
            with self._lock:
                record = copy.deepcopy(self._turns.get(key, {}).get(args['call_id']))
            if (not record or context.get('api_request_id') not in record['visible']
                    or record['input_sha256'] != hashlib.sha256(scope.user_message.encode()).hexdigest()):
                raise ValueError('The exact completed call must be present in this authorized native request')
            references = record['sources']
            if references is None or self.request_memory.supplied_snapshot(scope) is None:
                raise ValueError('Current request source lineage is unavailable')
            if 'payload' in record:
                payload = record['payload']  # First nomination owns immutable retry bytes.
            else:
                record['visible_name'] = record['visible'][context['api_request_id']]
                content, native = native_original(scope, args['call_id'], record)
                origins = capture_instruction(scope, self.client)
                if len(origins) != 1:
                    raise ValueError('The exact current owner instruction must be retained first')
                source_id = 'native-observation:' + hashlib.sha256(json.dumps(native, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                origin_id, origin_version = next(iter(origins.items()))
                payload = {'session_id': scope.session_id, 'contact_id': scope.contact_id,
                    'turn_id': source_id, 'require_source_receipt': True,
                    'observation': {'native': native, 'content': content, 'reason': args['reason'],
                        'origin': {'source_id': origin_id, 'source_version': origin_version},
                        'sources': references}}
                with self._lock:
                    current = self._turns.get(key, {}).get(args['call_id'])
                    if current is None or context.get('api_request_id') not in current['visible']:
                        raise ValueError('The current observation turn ended')
                    payload = current.setdefault('payload', payload)
            # A delivered receipt is historical, not proof the source still exists.
            deadline = time.monotonic()+.25
            after = self.outbox.erasure_watermark(scope.contact_id, deadline_monotonic=deadline)
            response = self.client.get('/v1/host/memory/sources/erasures',
                params={'contact_id': scope.contact_id, 'after': after}, timeout=.25,
                _deadline_monotonic=deadline)
            response.raise_for_status()
            page = response.json()
            self.outbox.apply_erasure_page(scope.contact_id, page, deadline_monotonic=deadline)
            if page.get('complete') is not True:
                raise ValueError('Current source erasure state is incomplete')
            receipt = self.outbox.enqueue(payload['turn_id'], payload)
            if receipt['state'] == 'pending':
                self.outbox.drain(lambda stored, timeout_seconds: self.client.sync_turn(
                    **stored, outbox=self.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
                receipt = self.outbox.enqueue(payload['turn_id'], payload)
            return json.dumps({'accepted': receipt['state'] == 'delivered', 'state': receipt['state'],
                'source_id': payload['turn_id'], 'source_recorded': receipt['state'] == 'delivered',
                'kind': 'original_tool_quotation', 'selection_author': 'model',
                'selected_call': {**{key: payload['observation']['native'][key] for key in
                    ('tool_call_id', 'tool_name', 'message_id', 'result_sha256')}, **record['arguments']}})
        except ValueError as exc:
            return json.dumps({'accepted': False, 'source_recorded': False, 'error': str(exc)})
        except Exception:
            return json.dumps({'accepted': False, 'source_recorded': False,
                'error': 'Observation persistence is unconfirmed; inspect or retry this same call in the current turn'})
