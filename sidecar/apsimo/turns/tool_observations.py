"""Explicit host-retained original tool evidence, using the canonical source ledger."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'native-tool-observation-v1'
MAX_BYTES = 16384
MAX_OPENING_CANDIDATES = 64
# Matches source_observation_origin, including its partial-index predicate.
ORIGIN_QUERY = """SELECT * FROM turn_sources
    WHERE contact_id=? AND json_extract(messages_json, '$[0]._observation_sources[0].source_id')=?
      AND json_extract(messages_json, '$[0]._native_tool_observation') = 'native-tool-observation-v1'
      AND (scope='person' OR session_id=?)
    ORDER BY turn_id LIMIT ?"""


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


def retained_for_origin(ledger, conn, origin, *, contact_id, session_id):
    """Bounded reference discovery, never a claim that a task succeeded.

    The current v1 writer stores its validated origin first. Other dependencies,
    common sessions and the model's nomination reason cannot establish origin.
    None denotes an over-limit cohort, not an empty or complete directory.
    """
    from .idempotency import canonical_turn_digest, source_message_hash
    scope = {'contact_id': contact_id, 'session_id': session_id}
    parent = conn.execute('SELECT session_id,messages_json FROM turn_sources WHERE turn_id=?',
                          (origin['source_id'],)).fetchone()
    messages = json.loads(parent['messages_json']) if parent else []
    if (origin not in ledger.source_references([origin['source_id']], **scope)
            or len(messages) != 1 or messages[0].get('role') != 'user'
            or canonical_turn_digest(messages) != origin['source_version']):
        raise ValueError('source_observations_require_current_instruction')
    rows = conn.execute(ORIGIN_QUERY, (contact_id, origin['source_id'], session_id,
                                      MAX_OPENING_CANDIDATES + 1)).fetchall()
    if len(rows) > MAX_OPENING_CANDIDATES:
        return None
    retained = []
    for row in rows:
        try:
            messages = json.loads(row['messages_json'])
            if len(messages) != 1 or messages[0].get('role') != 'tool':
                continue
            message = messages[0]
            provenance = message['provenance']
            dependencies = message['_observation_sources']
            if (not dependencies or dependencies[0] != origin or provenance['kind'] != VERSION
                    or provenance['selection']['author'] != 'model'):
                continue
            observation = ToolObservation(native=provenance['native'], content=message['content'],
                reason=provenance['selection']['reason'], origin=dependencies[0], sources=dependencies[1:])
            native = observation.native.model_dump()
            ref = {'source_id': row['turn_id'], 'source_version': canonical_turn_digest(messages)}
            current = ledger.source_references([ref['source_id'], *(r['source_id'] for r in dependencies)], **scope)
            if (identity_id(native) != row['turn_id'] or native['session_id'] != row['session_id']
                    or native['session_id'] != parent['session_id'] or ref not in current
                    or any(r not in current for r in dependencies)):
                continue
            retained.append({'reference': ref,
                'message_hash': source_message_hash(row['session_id'], message),
                'entry': {**ref, 'tool_name': native['tool_name'], 'tool_call_id': native['tool_call_id'],
                    'observed_at': datetime.fromtimestamp(native['timestamp'], timezone.utc).isoformat(),
                    'recorded_at': row['ingested_at'],
                    'selection_reason': {'author': 'model', 'reason': observation.reason}}})
        except (KeyError, TypeError, ValueError):
            # Non-v1 or incomplete originals have no trustworthy opening link.
            continue
    return retained


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
