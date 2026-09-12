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

MAX_BYTES = 16384
_EXCLUDED = {'session_search', 'colony_memory_retain_observation', 'apsimo_memory_retain_observation'}


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


def _request_results(request):
    calls, results = {}, {}
    for message in request.get('messages', request.get('input', [])):
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                if isinstance(call, dict) and isinstance(call.get('function'), dict):
                    calls[call.get('id')] = call['function'].get('name')
        elif message.get('type') == 'function_call':
            calls[message.get('call_id')] = message.get('name')
        if message.get('role') == 'tool':
            results[message.get('tool_call_id')] = message.get('content')
        elif message.get('type') == 'function_call_output':
            results[message.get('call_id')] = message.get('output')
        if isinstance(message.get('content'), list):
            for block in message['content']:
                if not isinstance(block, dict):
                    continue
                if message.get('role') == 'assistant' and block.get('type') == 'tool_use':
                    calls[block.get('id')] = block.get('name')
                elif message.get('role') == 'user' and block.get('type') == 'tool_result':
                    results[block.get('tool_use_id')] = block.get('content')
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

    def completed(self, scope, context, value):
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
                'input_sha256': hashlib.sha256(scope.user_message.encode()).hexdigest(), 'sources': self.request_memory.supplied_snapshot(scope)})
            while len(turn) > 16:
                turn.popitem(last=False)
            self._turns.move_to_end(key)
            while len(self._turns) > 64:
                self._turns.popitem(last=False)

    def checked(self, request, scope, request_id):
        key = _key(scope)
        if key is None or not isinstance(request, dict) or not isinstance(request_id, str) or not request_id:
            return
        calls, results = _request_results(request)
        with self._lock:
            for call_id, record in self._turns.get(key, {}).items():
                text = results.get(call_id)
                record['visible'].pop(request_id, None)
                name = calls.get(call_id)
                if (isinstance(name, str) and operation(name) == record['name'] and isinstance(text, str)
                        and hashlib.sha256(text.encode()).hexdigest() == record['sha256']):
                    record['visible'][request_id] = name
                while len(record['visible']) > 8:
                    record['visible'].pop(next(iter(record['visible'])))

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
                'kind': 'original_tool_quotation', 'selection_author': 'model'})
        except ValueError as exc:
            return json.dumps({'accepted': False, 'source_recorded': False, 'error': str(exc)})
        except Exception:
            return json.dumps({'accepted': False, 'source_recorded': False,
                'error': 'Observation persistence is unconfirmed; inspect or retry this same call in the current turn'})
