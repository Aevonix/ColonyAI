"""Explicit host-retained original tool evidence, using the canonical source ledger."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'native-tool-observation-v1'
MAX_BYTES = 16384


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
