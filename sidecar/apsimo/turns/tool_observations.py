"""Explicit host-retained original tool evidence, using the canonical source ledger."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'native-tool-observation-v1'
MAX_BYTES = 16384
LINKED_INCLUSION_POLICY = 'exact-origin-spare-budget-v1'


def initialize(conn):
    # Rebuildable from canonical JSON; the v1 writer always inserts origin first.
    # This is only a bounded lookup key. Retrieval independently checks its hash.
    conn.execute('''CREATE INDEX IF NOT EXISTS native_observation_origin
        ON turn_sources(contact_id, json_extract(messages_json, '$[0]._observation_sources[0].source_id'), turn_id)
        WHERE json_extract(messages_json, '$[0]._native_tool_observation')='native-tool-observation-v1' ''')


class NativeToolIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str = Field(pattern='^[0-9a-f]{64}$')
    session_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    api_request_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=128)
    message_id: int = Field(gt=0)
    timestamp: float = Field(gt=0, allow_inf_nan=False)
    result_sha256: str = Field(pattern='^[0-9a-f]{64}$')


class ObservationSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(pattern='^[0-9a-f]{64}$')


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native: NativeToolIdentity
    content: str = Field(min_length=1, max_length=MAX_BYTES)
    reason: str = Field(min_length=1, max_length=512)
    origin: ObservationSource
    sources: list[ObservationSource] = Field(default_factory=list, max_length=256)

    @model_validator(mode='after')
    def exact_result(self):
        if (not self.content.strip() or not self.reason.strip()
                or len(self.content.encode()) > MAX_BYTES
                or hashlib.sha256(self.content.encode()).hexdigest() != self.native.result_sha256):
            raise ValueError('invalid_original_tool_result')
        return self


def identity_id(native):
    return 'native-observation:' + hashlib.sha256(json.dumps(native, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def record(ledger, observation, *, contact_id, session_id, source_id):
    """The authenticated host supplies provenance; the model supplies only nomination."""
    native = observation.native.model_dump()
    if native['session_id'] != session_id or source_id != identity_id(native):
        raise ValueError('observation_identity_mismatch')
    origin = observation.origin.model_dump()
    # The direct current owner instruction is already captured by the host.
    # Tool results and machine task wrappers cannot stand in for this origin.
    with closing(ledger._connect()) as conn:
        row = conn.execute('SELECT session_id,messages_json FROM turn_sources WHERE turn_id=? AND contact_id=?',
                           (origin['source_id'], contact_id)).fetchone()
        from apsimo.turns.idempotency import canonical_turn_digest, SourceErased
        if row is None:
            if ledger.is_source_erased(origin['source_id'], contact_id):
                raise SourceErased('observation_origin_erased')
            raise ValueError('observation_origin_missing')
        messages = json.loads(row['messages_json'])
        if (row['session_id'] != session_id or len(messages) != 1 or messages[0].get('role') != 'user'
                or canonical_turn_digest(messages) != origin['source_version']):
            raise ValueError('observation_origin_mismatch')
    refs = {}
    for ref in [origin, *(s.model_dump() for s in observation.sources)]:
        if ref['source_id'] in refs and refs[ref['source_id']] != ref:
            raise ValueError('observation_source_revision_conflict')
        refs[ref['source_id']] = ref
    message = {'role': 'tool', 'content': observation.content, '_native_tool_observation': VERSION,
        '_observation_sources': list(refs.values()),
        'provenance': {'kind': VERSION, 'native': native,
                       'selection': {'author': 'model', 'reason': observation.reason}}}
    created = ledger.record_source(source_id, contact_id=contact_id, session_id=session_id,
        messages=[message], occurred_at=datetime.fromtimestamp(native['timestamp'], timezone.utc).isoformat(),
        derive_claims=False)
    return created


def expand_selected(ledger, selected, *, contact_id, session_id, limit=5, max_chars=6000):
    """Add exact originating-request evidence only within the unused packet.

    Linked inclusion is not a rerank pass or a relevance-score promotion. Its
    separately versioned policy needs complete-packet qualification. Only a
    bounded complete retained set is quoted; it is never the entire task history.
    """
    from .idempotency import canonical_turn_digest, source_message_hash
    from .source_annotations import expand, current_candidates
    from apsimo.intelligence.graph.recall import source_candidates, pack_memory_context
    scope = dict(contact_id=contact_id, session_id=session_id)
    result, body = pack_memory_context(selected, limit=limit, max_chars=max_chars)
    baseline = list(result)
    seeds = []
    for row in baseline:
        identifier = row.get('source_turn_id') or str(row.get('source_uri', '')).removeprefix('turn:')
        ref = next((r for r in row.get('_annotation_source_refs', []) if r['source_id'] == identifier), None)
        if ref and identifier.startswith('task-instruction:') and ref not in seeds:
            seeds.append(ref)
    for origin in seeds:
        spare = min(4, max(0, limit - len(result)))
        if not spare:
            break
        direct = {}
        for row in result:
            if (row.get('kind') == 'source_quote' and row.get('role') == 'tool'
                    and str(row.get('source_turn_id', '')).startswith('native-observation:')):
                direct.setdefault(row['source_turn_id'], []).append(row)
        # Already ranked originals use slots too. With the host's five-row
        # packet this still fetches at most five rows, including the sentinel.
        set_limit = spare + len(direct)
        if current_candidates(ledger, baseline, **scope) != baseline:
            return pack_memory_context(baseline, limit=limit, max_chars=max_chars)
        with closing(ledger._connect()) as conn:
            parent = conn.execute('''SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?
                AND (scope='person' OR session_id=?)''',
                (origin['source_id'], contact_id, session_id)).fetchone()
            if parent is None or origin not in ledger.source_references([origin['source_id']], **scope):
                continue
            messages = json.loads(parent['messages_json'])
            if (len(messages) != 1 or messages[0].get('role') != 'user'
                    or not isinstance(messages[0].get('content'), str)
                    or canonical_turn_digest(messages) != origin['source_version']):
                continue
            rows = conn.execute('''SELECT * FROM turn_sources
                WHERE contact_id=? AND json_extract(messages_json, '$[0]._observation_sources[0].source_id')=?
                AND json_extract(messages_json, '$[0]._native_tool_observation')='native-tool-observation-v1'
                AND (scope='person' OR session_id=?)
                AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=turn_sources.turn_id)
                ORDER BY turn_id LIMIT ?''',
                (contact_id, origin['source_id'], session_id, set_limit + 1)).fetchall()
        if not rows:
            continue
        candidates = []
        try:
            for source in rows:
                stored = json.loads(source['messages_json'])
                if len(stored) != 1 or stored[0].get('role') != 'tool':
                    raise ValueError('invalid_observation')
                message = stored[0]
                provenance = message['provenance']
                observation = ToolObservation(native=provenance['native'], content=message['content'],
                    reason=provenance['selection']['reason'], origin=message['_observation_sources'][0],
                    sources=message['_observation_sources'][1:])
                native = observation.native.model_dump()
                reconstructed = 'task-instruction:' + hashlib.sha256(json.dumps(
                    [contact_id, source['session_id'], native['turn_id'], messages[0]['content']],
                    sort_keys=True).encode()).hexdigest()
                if (message['_native_tool_observation'] != VERSION or provenance['kind'] != VERSION
                        or provenance['selection']['author'] != 'model'
                        or reconstructed != origin['source_id'] or observation.origin.model_dump() != origin
                        or source['session_id'] != parent['session_id'] or native['session_id'] != source['session_id']
                        or identity_id(native) != source['turn_id']):
                    raise ValueError('invalid_observation_origin')
                refs = [*message['_observation_sources'],
                        {'source_id': source['turn_id'], 'source_version': canonical_turn_digest(stored)}]
                current = ledger.source_references([r['source_id'] for r in refs], **scope)
                if any(ref not in current for ref in refs):
                    raise ValueError('observation_dependency_changed')
                row = source_candidates([{**dict(source), 'role': 'tool', 'content': message['content'],
                    'source_message_hash': source_message_hash(source['session_id'], message)}])[0]
                row.update(atomic_evidence=True, linked_inclusion_policy=LINKED_INCLUSION_POLICY,
                    linked_observation_of=origin['source_id'], history_scope='retained_observations_only')
                candidates.append(row)
        except (KeyError, TypeError, ValueError):
            # A malformed/changed origin cannot establish a partial relationship.
            continue
        expanded = expand(ledger, candidates, **scope)
        if len(expanded) != len(candidates) or len(current_candidates(ledger, expanded, **scope)) != len(expanded):
            continue
        refs, membership, notes = {}, {}, set()
        for row in expanded:
            refs.update((r['source_id'], r) for r in row.get('_annotation_source_refs', []))
            notes.update(row.get('_annotation_ids', []))
            for identifier, hashes in row.get('_annotation_message_hashes', {}).items():
                membership.setdefault(identifier, set()).update(hashes)
        if len(refs) > 512 or any(row['source_turn_id'] not in refs for row in candidates):
            continue
        snapshot = dict(_annotation_source_refs=list(refs.values()), _annotation_ids=tuple(sorted(notes)),
                        _annotation_message_hashes={key: sorted(value) for key, value in membership.items()})
        # Every added member carries the whole set's revision/correction snapshot.
        # A concurrent change to any member drops the complete added set.
        expanded = [dict(row, **snapshot) for row in expanded]
        missing = [row for row in expanded if row['source_turn_id'] not in direct]
        partial = any(
            not any(not prior.get('excerpt_truncated') and prior.get('content') == row['content']
                    and prior.get('source_message_hash') == row.get('source_message_hash')
                    for prior in direct[row['source_turn_id']])
            for row in expanded if row['source_turn_id'] in direct)
        proposed, text = pack_memory_context(result + missing, limit=limit, max_chars=max_chars)
        if len(rows) <= set_limit and not partial and proposed == result + missing:
            result, body = proposed, text
        else:
            notice = {'id': 'linked-observations:' + origin['source_id'], 'kind': 'source_quote',
                'epistemic_state': 'incomplete_linked_evidence', 'atomic_evidence': True,
                'linked_inclusion_policy': LINKED_INCLUSION_POLICY, 'linked_observation_of': origin['source_id'],
                'history_scope': 'retained_observations_only', 'source_turn_ids': list(refs),
                'source_anchors': [{'source_id': row['source_turn_id']} for row in candidates],
                'content': ('Incomplete retained-observation context. Some originals are omitted or only partially shown here. '
                    'These are opening references, not task outcomes or a complete execution history. '
                    'Open the sources with their recalled revisions before interpreting an outcome.'), **snapshot}
            proposed, text = pack_memory_context(result + [notice], limit=limit, max_chars=max_chars)
            if proposed == result + [notice]:
                result, body = proposed, text
        if current_candidates(ledger, baseline, **scope) != baseline:
            return pack_memory_context(baseline, limit=limit, max_chars=max_chars)
    return result, body
