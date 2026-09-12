"""Typed local draft acceptance and transport-held native undertaking context."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

ACTIVE = ContextVar('pacomind_selected_local_work', default=None)


def request(client, path, body=None):
    response = client.post(path, json=body, timeout=3) if body is not None else client.get(path, timeout=3)
    response.raise_for_status()
    return response.json()


def accept(args, scope, client, native=None, *, coordinator=None, context=None):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review'}
            or not scope.turn_id or not scope.user_message.strip()
            or not {'question', 'sources'}.issubset(args)
            or set(args) - {'commitment_id', 'question', 'sources', 'new_draft'}
            or ('new_draft' in args and type(args['new_draft']) is not bool)):
        return json.dumps({'error': 'An owner turn accepting this local draft is required'})
    try:
        path = "/v1/host/commitments/local-draft"
        if 'commitment_id' in args:
            identifier = quote(args['commitment_id'], safe='')
            path = f"/v1/host/commitments/{identifier}/local-draft"
        body = {
            'contact_id': scope.contact_id, 'session_id': scope.session_id, 'turn_id': scope.turn_id,
            'question': args['question'], 'sources': args['sources']}
        if 'new_draft' in args:
            body['new_draft'] = args['new_draft']
        handoff = coordinator.handoff(args.get('commitment_id'), context or {}) if coordinator else None
        if handoff:
            body['handoff'] = handoff
        if native is not None:
            origin = native.origin(scope)
            if origin:
                body['origin'] = origin
        result = request(client, path, body)
        if handoff:
            if result.get('handoff_released') is not True:
                raise ValueError('acceptance_handoff_unconfirmed')
            coordinator.detach_handoff(args.get('commitment_id'), context or {}, handoff)
        if result['context'].get('execution_backend') == 'kanban':
            if native is None:
                raise ValueError('native_local_work_adapter_required')
            accepted = result
            result = native.ensure_task(result)
            result.update({key: accepted[key] for key in ('acceptance_matches_request', 'handoff_released')
                           if key in accepted})
        return json.dumps(result)
    except Exception as error:
        response = getattr(error, 'response', None)
        if response is not None and response.status_code == 409:
            try:
                detail = response.json()['detail']
                if (isinstance(detail, dict) and set(detail) == {'reason', 'initiative_id'}
                        and detail['reason'] in {'multiple_active_local_drafts', 'local_draft_in_progress',
                                                'local_draft_scope_changed_requires_new_draft'}):
                    return json.dumps({'error': detail['reason'], 'initiative_id': detail['initiative_id'],
                                       'execution_created': False})
            except (ValueError, KeyError, TypeError):
                pass
        return json.dumps({'error': 'Local draft acceptance unavailable; no execution is confirmed'})


class Undertaking:
    def __init__(self, assignment, client):
        self.assignment, self.client = assignment, client
        self.task_id = 'local-work:' + str(assignment['context'].get('native_execution_id')
                                         or assignment['context'].get('native_task_id', ''))
        self.bound = False
        self.error = None
        self.read = set()
        self.sources = {}
        self.holder = None

    def current(self):
        identifier = quote(self.assignment['id'], safe='')
        contact = quote(self.assignment['context']['contact_id'], safe='')
        value = request(self.client, f"/v1/host/commitments/local-work/{identifier}?contact_id={contact}")
        expected = self.assignment['context']
        if (value['status'] != 'assigned'
                or any(value['context'].get(key) != expected.get(key)
                       for key in ('native_job_id', 'native_execution_id', 'source_home_id', 'sources', 'question'))):
            raise ValueError('assignment_cancelled_or_superseded')
        return value

    def bind(self, scope, coordinator, context):
        try:
            if (scope is None or not scope.valid_participant or scope.platform != 'cli'
                    or scope.authority_lane != 'system'
                    or scope.contact_id != self.assignment['context']['contact_id']
                    or context.get('task_id') != self.task_id):
                raise ValueError('native_local_work_scope_unavailable')
            self.current()
            if self.assignment['context']['commitment_id'] is None:
                # A standalone task is already exclusively assigned to this
                # native execution. No second obligation or lease is needed.
                self.bound = True
                return
            result = json.loads(coordinator.handle({'operation': 'claim',
                'commitment_id': self.assignment['context']['commitment_id']}, scope, context))
            if result.get('accepted') is not True:
                raise ValueError('undertaking_not_acquired')
            self.bound = True
            self.holder = (coordinator, scope, dict(context))
        except Exception as error:
            self.error = type(error).__name__

    def release(self):
        if self.holder:
            coordinator, scope, context = self.holder
            coordinator.handle({'operation': 'release',
                'commitment_id': self.assignment['context']['commitment_id']}, scope, context)

    def before_tool(self, context):
        try:
            if (not self.bound or self.error or context.get('task_id') != self.task_id
                    or context.get('tool_name') not in {'pacomind_read_work_source', 'tool_search', 'tool_describe'}):
                raise ValueError('selected_local_work_tool_required')
            self.current()
        except Exception:
            return json.dumps({'error': 'This local undertaking is unavailable, cancelled or outside its read scope',
                               'effect_performed': False})
        return None

    def verify_holder(self):
        self.current()
        if not self.bound or self.error:
            raise ValueError('undertaking_not_acquired')
        if self.assignment['context']['commitment_id'] is None:
            return
        if not self.holder:
            raise ValueError('undertaking_not_acquired')
        coordinator, _, context = self.holder
        if coordinator.before_tool({**context, 'tool_name': 'pacomind_read_work_source'}) is not None:
            raise ValueError('undertaking_superseded')

    def read_source(self, args, context):
        from tools.file_tools import read_file_tool
        try:
            if self.before_tool(context) is not None or set(args) != {'source'}:
                raise ValueError('selected_source_required')
            index = args['source']
            paths = self.assignment['context']['sources']
            if type(index) is not int or not 0 <= index < len(paths):
                raise ValueError('unknown_source')
            path = Path(paths[index])
            raw = path.read_bytes()
            # This first task class handles bounded local UTF-8 documents.
            if len(raw) > 64 * 1024:
                raise ValueError('source_exceeds_local_draft_bound')
            raw.decode('utf-8')
            digest = hashlib.sha256(raw).hexdigest()
            if str(index) in self.sources and self.sources[str(index)]['sha256'] != digest:
                raise ValueError('source_changed_during_draft')
            result = read_file_tool(str(path), offset=1, limit=2000, task_id=self.task_id)
            value = json.loads(result)
            if value.get('error') or value.get('truncated'):
                raise ValueError('source_read_incomplete')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('source_changed_during_read')
            self.sources[str(index)] = {'path': str(path), 'sha256': digest}
            self.read.add(index)
            return json.dumps({'source': index, 'sha256': digest, 'native_read': value})
        except Exception as error:
            self.error = type(error).__name__
            return json.dumps({'error': str(error) if isinstance(error, ValueError) else type(error).__name__,
                               'effect_performed': False})


@contextmanager
def selected(undertaking):
    token = ACTIVE.set(undertaking)
    try:
        yield undertaking
    finally:
        ACTIVE.reset(token)


def bind_selected(scope, coordinator, context):
    work = ACTIVE.get()
    if work is not None:
        work.bind(scope, coordinator, context)


def before_tool(context):
    work = ACTIVE.get()
    return work.before_tool(context) if work is not None else None


def read_source(args, context, work=None):
    work = work or ACTIVE.get()
    if work is None:
        return json.dumps({'error': 'No accepted local undertaking is bound'})
    return work.read_source(args, context)
