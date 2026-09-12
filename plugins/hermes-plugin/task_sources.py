"""Canonical native text instructions and current owner task authority.

Only trusted native transport scopes enter ``capture`` and ``authorize_control``.
Stored source envelopes are provenance, never model-supplied credentials. The
existing canonical ledger and turn outbox own source freshness and forgetting.
"""
import time

from .client import redact_source_payload, source_input_erased, source_message_hash
from .followups import capture_instruction
from .input_provenance import _refs, current
from .task_handoffs import TaskHandoffError


_NON_DIRECT = frozenset({'cron', 'subagent', 'background_review', 'pacomind_task'})
_ORIGIN_FIELDS = ('platform', 'authority_gateway', 'sender_id', 'session_id', 'turn_id')


def _parents(original, additional, digest):
    merged = list(original)
    for ref in _refs(additional, digest) if additional else []:
        if ref not in merged:
            merged.append(ref)
    return _refs(merged, digest)


class NativeTaskSources:
    """Reuse owner resolution, canonical capture and the existing erasure cursor.

    ``erase(contact_id, rules)`` removes derived task text from the caller's
    association store. No raw instruction is copied into source envelopes.
    Configured local platforms use the same explicit owner attestation as the
    general plugin; remote handles are resolved again on every operation.
    """

    def __init__(self, client, outbox, owner_contact_id, *,
                 attested_system_platforms=('cli',), erase=None):
        self.client, self.outbox = client, outbox
        self.owner_contact_id = str(owner_contact_id or '').strip()
        self.attested_system_platforms = frozenset(attested_system_platforms) - _NON_DIRECT
        self.erase = erase

    def _owner(self, origin):
        gateway = origin['authority_gateway']
        if (origin['platform'] in self.attested_system_platforms
                and gateway == origin['platform']):
            if self.owner_contact_id:
                return self.owner_contact_id
            raise TaskHandoffError('The local task owner is not configured')
        if not origin['sender_id'] or gateway in self.attested_system_platforms:
            raise TaskHandoffError('Current task owner binding is unavailable')
        try:
            response = self.client.get('/v1/host/contacts/resolve', params={
                'gateway': gateway, 'address': origin['sender_id'], 'create': 'false'},
                timeout=.5, _deadline_monotonic=time.monotonic() + .5)
            response.raise_for_status()
            contact = response.json().get('contact_id')
        except Exception:
            raise TaskHandoffError('Current task owner binding is unavailable') from None
        if not self.owner_contact_id or contact != self.owner_contact_id:
            raise TaskHandoffError('The task participant is no longer the configured owner')
        return contact

    def _scope(self, scope):
        if (scope is None or not scope.valid_participant
                or scope.authority_lane not in {'owner', 'system'}
                or scope.platform in _NON_DIRECT or current() is not None
                or getattr(scope, 'parent_session_id', '')
                or not scope.session_id or not scope.turn_id):
            raise TaskHandoffError('An ordinary authenticated owner turn is required')
        origin = dict(platform=scope.platform,
            authority_gateway=getattr(scope, 'authority_gateway', '') or scope.platform,
            sender_id=scope.sender_id, session_id=scope.session_id, turn_id=scope.turn_id)
        if (scope.authority_lane == 'system' and
                (scope.resolution_status != 'attested_system'
                 or origin['platform'] not in self.attested_system_platforms
                 or origin['authority_gateway'] != origin['platform'])):
            raise TaskHandoffError('This local task platform is not attested')
        owner = self._owner(origin)
        if owner != scope.contact_id:
            raise TaskHandoffError('The task participant binding changed')
        return origin, owner

    def capture(self, scope):
        origin, owner = self._scope(scope)
        if (not isinstance(scope.user_message, str) or not scope.user_message.strip()
                or len(scope.user_message) > 32768):
            raise TaskHandoffError('An exact bounded direct text instruction is required')
        try:
            versions = capture_instruction(scope, self.client)
        except Exception:
            raise TaskHandoffError('Task instruction capture is unconfirmed; retry the same operation') from None
        if len(versions) != 1:
            raise TaskHandoffError('One exact captured task instruction is required')
        source_id, version = next(iter(versions.items()))
        return self.resolve_source({
            'version': 1, 'principal': 'hermes:' + origin['authority_gateway'],
            'source_session_id': scope.session_id, 'contact_id': owner, 'watermark': 0,
            'input_refs': [{'source_id': source_id, 'input_message_hash': source_message_hash(
                scope.session_id, {'role': 'user', 'content': scope.user_message})}],
            'source_refs': [{'source_id': source_id, 'source_version': version}],
            'origin': origin})

    def actor_contact(self, scope):
        """Resolve the actual ordinary actor without manufacturing source input."""
        return self._scope(scope)[1]

    def execution_identity(self, source):
        owner = self.resolve_owner(source, require_task_grant=True)
        origin = source['origin']
        local = origin['platform'] in self.attested_system_platforms
        return {'sender_id': origin['sender_id'], 'contact_id': owner,
                'authority_lane': 'system' if local else 'owner',
                'resolution_status': 'attested_system' if local else 'resolved',
                'authority_gateway': origin['authority_gateway']}

    def resolve_owner(self, source, *, require_task_grant):
        """Resolve current ownership without reading or refreshing old content.

        Generic native tasks require the configured owner for both control and
        new work. There is no separate mutable per-channel recall grant here.
        ``require_task_grant`` preserves the shared store's resolver contract.
        """
        origin = source.get('origin') if isinstance(source, dict) else None
        if (not isinstance(origin, dict) or set(origin) != set(_ORIGIN_FIELDS)
                or any(not isinstance(origin[key], str) or len(origin[key]) > 256
                       for key in _ORIGIN_FIELDS)
                or not all(origin[key] for key in _ORIGIN_FIELDS if key != 'sender_id')
                or origin['platform'] in _NON_DIRECT
                or source.get('version') != 1
                or source.get('principal') != 'hermes:' + origin['authority_gateway']
                or source.get('source_session_id') != origin['session_id']):
            raise TaskHandoffError('The retained native task origin is invalid')
        owner = self._owner(origin)
        if source.get('contact_id') != owner:
            raise TaskHandoffError('The retained task owner changed')
        return owner

    def authorize_control(self, source, scope):
        """Allow another currently authenticated channel of this same owner."""
        _, actor = self._scope(scope)
        if actor != self.resolve_owner(source, require_task_grant=False):
            raise TaskHandoffError('The current participant does not own this task')
        return actor

    def resolve_source(self, source, dependencies=None):
        """Check exact current, unannotated input and all consumed dependencies.

        An annotation can change intent without changing source bytes or the
        erasure cursor. This narrow resolver then withholds the old instruction;
        a fresh owner instruction must be captured rather than ignoring the note.
        """
        owner = self.resolve_owner(source, require_task_grant=True)
        try:
            inputs = _refs(source['input_refs'], 'input_message_hash')
            sources = _refs(source['source_refs'], 'source_version')
            if len(inputs) != 1 or inputs[0]['source_id'] not in {r['source_id'] for r in sources}:
                raise ValueError('One captured native instruction is required')
            dependencies = dependencies or {}
            parents = _parents(inputs, dependencies.get('input_refs', []), 'input_message_hash')
            versions = _parents(sources, dependencies.get('source_refs', []), 'source_version')
            watermark = source['watermark']
            if type(watermark) is not int or watermark < 0:
                raise ValueError('Invalid source watermark')
            deadline = time.monotonic() + 2
            after, rules = self.outbox.erasure_state(owner, deadline_monotonic=deadline)
            if self.erase:
                self.erase(owner, rules)
            response = self.client.post('/v1/host/memory/sources/erasures', json={
                'contact_id': owner, 'session_id': source['source_session_id'], 'after': after,
                'source_refs': versions, 'unannotated_input_refs': parents},
                timeout=max(.001, deadline - time.monotonic()), _deadline_monotonic=deadline)
            response.raise_for_status()
            page = response.json()
            self.outbox.apply_erasure_page(owner, page, deadline_monotonic=deadline)
            after, rules = self.outbox.erasure_state(owner, deadline_monotonic=deadline)
            if self.erase:
                self.erase(owner, rules)
            if (page.get('complete') is not True or page.get('sources_current') is not True
                    or after < watermark or time.monotonic() > deadline
                    or any(source_input_erased(ref, rules) for ref in parents)
                    or redact_source_payload({'contact_id': owner,
                        'session_id': source['source_session_id'], 'turn_id': 'native-task-source-check',
                        'assistant_message': 'dependency-check', 'assistant_source_refs': versions}, rules) is None):
                raise ValueError('Task source unavailable')
        except Exception:
            raise TaskHandoffError('Task sources are unavailable or changed; inspect source state before continuing') from None
        return {'version': 1, 'principal': source['principal'],
            'source_session_id': source['source_session_id'], 'contact_id': owner,
            'input_refs': inputs, 'source_refs': sources, 'watermark': after,
            'origin': dict(source['origin'])}
