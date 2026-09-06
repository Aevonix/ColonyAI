"""Embedding identity and rebuild metadata in the existing source ledger.

Lance files are projections. The small active pointer and deletion fences live
with canonical source state, so retaining an old index does not undo forgetting.
No identity field asserts that remotely served weights have been inspected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import uuid


class IncompatibleIndex(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingIdentity:
    requested_model: str
    served_model: str
    declared_revision: str
    dimensions: int
    document_format: str = "raw-text-v1"
    query_format: str = "raw-text-v1"
    normalization: str = "unspecified"
    quantization: str = "unspecified"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def from_pipeline(cls, pipeline):
        provider = pipeline._provider
        config = provider._config
        return cls(
            requested_model=config.model_id,
            served_model=getattr(provider, "_served_model", "") or "unknown",
            declared_revision=getattr(config, "revision", None) or "unknown",
            dimensions=pipeline.dimensions,
            query_format='prefix-v1:' + pipeline.query_instruction,
            normalization="provider-l2" if config.provider in {"cpu", "cuda", "mlx"} else "unspecified",
            quantization=config.quantization or "unspecified",
        )


class IndexCatalog:
    """One deployment's primary vector index, backed by its source ledger."""

    def __init__(self, ledger):
        self.ledger = ledger
        with ledger._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS vector_generations (
                    id TEXT PRIMARY KEY, fingerprint TEXT, identity_json TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS vector_active (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), generation_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vector_projection_deletions (
                    collection TEXT NOT NULL, entry_id TEXT NOT NULL,
                    PRIMARY KEY(collection, entry_id));
            ''')

    @staticmethod
    def _record(row):
        if row is None:
            return None
        result = dict(row)
        result['identity'] = json.loads(result.pop('identity_json') or 'null')
        return result

    def active(self):
        with self.ledger._connect() as conn:
            return self._record(conn.execute('''SELECT g.* FROM vector_generations g
                JOIN vector_active a ON a.generation_id=g.id WHERE a.slot=1''').fetchone())

    def generations(self):
        with self.ledger._connect() as conn:
            return [self._record(row) for row in conn.execute('SELECT * FROM vector_generations ORDER BY created_at,id')]

    def initialize(self, identity: EmbeddingIdentity, *, legacy_exists: bool):
        with self.ledger._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM vector_active').fetchone():
                return
            if legacy_exists:
                generation, fingerprint, payload, status = 'legacy', None, None, 'unverified'
            else:
                generation = uuid.uuid4().hex
                fingerprint, payload, status = identity.fingerprint, json.dumps(asdict(identity)), 'ready'
            conn.execute('INSERT INTO vector_generations(id,fingerprint,identity_json,status) VALUES(?,?,?,?)',
                         (generation, fingerprint, payload, status))
            conn.execute('INSERT INTO vector_active(slot,generation_id) VALUES(1,?)', (generation,))

    def begin(self, identity: EmbeddingIdentity):
        with self.ledger._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM vector_generations WHERE fingerprint=? AND status='building'",
                               (identity.fingerprint,)).fetchone()
            if row is not None:
                return self._record(row)
            generation = uuid.uuid4().hex
            conn.execute('INSERT INTO vector_generations(id,fingerprint,identity_json,status) VALUES(?,?,?,?)',
                         (generation, identity.fingerprint, json.dumps(asdict(identity)), 'building'))
            return self._record(conn.execute('SELECT * FROM vector_generations WHERE id=?', (generation,)).fetchone())

    def write_generation(self, identity: EmbeddingIdentity):
        # New writes follow a matching rebuild, including a same-model rebuild.
        # This keeps new arrivals in the generation that will be promoted.
        with self.ledger._connect() as conn:
            row = conn.execute("SELECT * FROM vector_generations WHERE fingerprint=? AND status='building'",
                               (identity.fingerprint,)).fetchone()
        if row is not None:
            return self._record(row)
        active = self.active()
        if active and active['fingerprint'] == identity.fingerprint:
            return active
        raise IncompatibleIndex('Embedding identity has no compatible index; rebuild before semantic use')

    def read_generation(self, identity: EmbeddingIdentity):
        active = self.active()
        if not active or active['fingerprint'] != identity.fingerprint:
            raise IncompatibleIndex('Active embedding index is unverified or incompatible; lexical recall remains available')
        return active

    def finish(self, generation_id: str, *, error: str):
        with self.ledger._connect() as conn:
            conn.execute("UPDATE vector_generations SET error=? WHERE id=? AND status='building'",
                         (error, generation_id))

    def promote(self, generation_id: str, identity: EmbeddingIdentity):
        with self.ledger._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM vector_generations WHERE id=?', (generation_id,)).fetchone()
            if row is None or row['status'] != 'building' or row['fingerprint'] != identity.fingerprint:
                raise IncompatibleIndex('Only a complete generation matching the selected embedding identity can be promoted')
            # The caller has finished writing all collections. Completion and
            # pointer movement are one commit: interruption cannot strand a
            # ready generation containing arrivals absent from the old index.
            conn.execute("UPDATE vector_generations SET status='ready',error=NULL WHERE id=?", (generation_id,))
            conn.execute("UPDATE vector_generations SET status='retained' WHERE id=(SELECT generation_id FROM vector_active WHERE slot=1) AND id<>?", (generation_id,))
            conn.execute('UPDATE vector_active SET generation_id=? WHERE slot=1', (generation_id,))

    def delete(self, collection: str, entry_id: str):
        with self.ledger._connect() as conn:
            conn.execute('INSERT OR IGNORE INTO vector_projection_deletions VALUES(?,?)', (collection, entry_id))

    def deleted(self, collection: str, entry_id: str) -> bool:
        with self.ledger._connect() as conn:
            return conn.execute('SELECT 1 FROM vector_projection_deletions WHERE collection=? AND entry_id=?',
                                (collection, entry_id)).fetchone() is not None

    def source_erased(self, metadata) -> bool:
        if (metadata or {}).get('source_projection'):
            from colony_sidecar.turns.source_vectors import hydrate
            return hydrate(self.ledger, metadata) is None
        source = (metadata or {}).get('source_uri', '')
        return isinstance(source, str) and source.startswith('turn:') and self.ledger.is_projection_erased(source[5:])
