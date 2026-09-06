"""Colony Vector Store — tier migration.

Migrates all vectors from an old embedding model to a new one.
Re-embeds every vector with the current pipeline, updates metadata,
and optionally updates the .env file with the new configuration.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    """Result of a tier migration run."""

    collections_migrated: int = 0
    vectors_migrated: int = 0
    vectors_failed: int = 0
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    generation_id: str = ""
    fingerprint: str = ""


@dataclass
class MigrationState:
    """Persisted state of an in-progress or completed migration."""

    from_model: str = ""
    to_model: str = ""
    collections_done: list[str] = field(default_factory=list)
    collections_remaining: list[str] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "from_model": self.from_model,
            "to_model": self.to_model,
            "collections_done": self.collections_done,
            "collections_remaining": self.collections_remaining,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MigrationState:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _migration_state_path() -> Path:
    state_dir = os.environ.get("COLONY_STATE_DIR", ".")
    return Path(state_dir) / "migration_state.json"


def load_migration_state() -> Optional[MigrationState]:
    """Load persisted migration state, or None if no migration in progress."""
    path = _migration_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return MigrationState.from_dict(data)
    except Exception as exc:
        logger.warning("Failed to load migration state: %s", exc)
        return None


def save_migration_state(state: MigrationState) -> None:
    """Persist migration state to disk."""
    path = _migration_state_path()
    try:
        path.write_text(json.dumps(state.to_dict(), indent=2))
    except Exception as exc:
        logger.warning("Failed to save migration state: %s", exc)


def clear_migration_state() -> None:
    """Remove migration state file."""
    path = _migration_state_path()
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


async def migrate_tier(
    store,
    pipeline,
    old_model_id: Optional[str] = None,
    batch_size: int = 64,
    graph=None,
) -> MigrationResult:
    """Migrate all vectors to the current embedding model.

    Parameters
    ----------
    store : VectorStore
        The LanceDB vector store.
    pipeline : EmbeddingPipeline
        The new embedding pipeline to use.
    old_model_id : str, optional
        The old model ID to migrate from. If None, re-embeds all vectors
        regardless of their current model_id.
    batch_size : int
        Number of rows to embed per batch.

    Returns
    -------
    MigrationResult
    """
    if getattr(store, 'catalog', None) is not None:
        return await _migrate_generation(store, pipeline, graph=graph,
                                         old_model_id=old_model_id, batch_size=batch_size)
    from colony_sidecar.vector.collections import Collection
    from colony_sidecar.vector.backfill import _backfill_collection

    start = time.monotonic()
    result = MigrationResult()

    # Determine current model
    current_model_id = ""
    if hasattr(pipeline, "_provider") and hasattr(pipeline._provider, "_config"):
        current_model_id = pipeline._provider._config.model_id

    # Set up migration state
    state = MigrationState(
        from_model=old_model_id or "unknown",
        to_model=current_model_id,
        collections_remaining=[c.value for c in Collection],
        started_at=start,
    )
    save_migration_state(state)

    for col in Collection:
        try:
            # Use backfill logic with forced re-embedding
            rows = await store.scan_all(col)
            to_embed = []
            for row in rows:
                meta_str = row.get("metadata", "{}")
                try:
                    meta = json.loads(meta_str) if isinstance(meta_str, str) else (meta_str or {})
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                # If old_model_id specified, only migrate vectors from that model
                if old_model_id and meta.get("model_id") != old_model_id:
                    continue
                to_embed.append(row)

            # Re-embed in batches
            now = time.time()
            for i in range(0, len(to_embed), batch_size):
                batch = to_embed[i : i + batch_size]
                texts = [row.get("text", "") for row in batch]

                try:
                    vectors = await pipeline.embed_batch(texts)
                except Exception as exc:
                    result.vectors_failed += len(batch)
                    result.errors.append(f"embed batch failed: {exc}")
                    continue

                for row, vector in zip(batch, vectors):
                    try:
                        meta_str = row.get("metadata", "{}")
                        try:
                            meta = json.loads(meta_str) if isinstance(meta_str, str) else (meta_str or {})
                        except (json.JSONDecodeError, TypeError):
                            meta = {}
                        meta["model_id"] = current_model_id
                        meta["embedded_at"] = now

                        await store.update(col, id=row["id"], text=row.get("text", ""), vector=vector, metadata=meta)
                        result.vectors_migrated += 1
                    except Exception as exc:
                        result.vectors_failed += 1
                        result.errors.append(f"update {row.get('id', '?')} failed: {exc}")

            result.collections_migrated += 1
            state.collections_done.append(col.value)
            state.collections_remaining.remove(col.value)
            save_migration_state(state)

        except Exception as exc:
            msg = f"Collection {col.value} migration failed: {exc}"
            logger.error(msg)
            result.errors.append(msg)

    state.completed_at = time.monotonic()
    save_migration_state(state)

    # Update .env if migration succeeded
    if result.vectors_migrated > 0 and result.errors == []:
        _update_env(current_model_id, pipeline)

    result.duration_s = time.monotonic() - start
    return result


async def _migrate_generation(store, pipeline, *, graph, old_model_id, batch_size):
    """Rebuild from retained evidence; the selected generation is never rewritten."""
    from colony_sidecar.vector.collections import Collection
    from colony_sidecar.vector.indexes import IncompatibleIndex
    if store.identity != pipeline.index_identity:
        raise IncompatibleIndex('The embedding pipeline and index identity do not match')
    if old_model_id:
        raise ValueError('A generation rebuild must include all retained rows, not one model subset')
    if not 1 <= batch_size <= 128:
        raise ValueError('Rebuild batch size must be between 1 and 128')
    started = time.monotonic()
    generation = store.catalog.begin(store.identity)
    result = MigrationResult(generation_id=generation['id'], fingerprint=store.identity.fingerprint)
    source_generation = store.catalog.active()

    async def source_rows(collection):
        if collection == Collection.MEMORIES and graph is not None:
            # The graph is authoritative for memories, including rows missed by
            # the old index during embedding outages. This is not a restore.
            async for row in graph.iter_indexable_memories():
                yield row
        else:
            db = await store._generation_db(source_generation)
            if collection.value in await db.table_names():
                table = await db.open_table(collection.value)
                for row in (await table.query().select(['id', 'text', 'metadata']).to_pandas()).to_dict('records'):
                    yield row

    async def write_batch(collection, rows):
        vectors = await pipeline.embed_batch([row['text'] for row in rows])
        if len(vectors) != len(rows):
            raise ValueError('Embedding batch response does not match retained inputs')
        for vector in vectors:
            store._validate_vector(vector)
        table = await store._table(collection, write=True, generation=generation)
        projected_rows, eligible, projected_ids = [], [], set()
        for row, vector in zip(rows, vectors):
            meta = row['metadata']
            if row['id'] in projected_ids or not store._eligible(collection, row['id'], meta):
                continue
            now = time.time()
            meta = {**meta, 'embedding_fingerprint': store.identity.fingerprint,
                    'model_id': store.identity.requested_model, 'served_model': store.identity.served_model,
                    'declared_revision': store.identity.declared_revision, 'embedded_at': now}
            projected = {'id': row['id'], 'text': row['text'], 'vector': vector,
                         'metadata': json.dumps(meta, default=str),
                         'scope_key': meta.get('scope_key'),
                         'modality': meta.get('modality', 'text'), 'image_hash': meta.get('image_hash', ''),
                         'image_ref': meta.get('image_ref', ''), 'thumbnail_ref': meta.get('thumbnail_ref', ''),
                         'caption': meta.get('caption', ''), 'created_at': now, 'updated_at': now}
            projected_rows.append(projected)
            eligible.append((row['id'], meta))
            # Legacy Lance collections can contain repeated IDs. Preserve the
            # first eligible row, as the earlier per-row insert-only merge did.
            projected_ids.add(row['id'])
        if not projected_rows:
            return
        # One Lance commit per embedding batch. Existing staged rows include
        # concurrent live writes and survive the insert-only merge unchanged.
        await table.merge_insert('id').when_not_matched_insert_all().execute(projected_rows)
        for entry_id, meta in eligible:
            # A source can be erased while the batch commit is in flight.
            if not store._eligible(collection, entry_id, meta):
                await table.delete('id = ' + store._quoted(entry_id))
            else:
                result.vectors_migrated += 1

    try:
        for collection in Collection:
            table = await store._table(collection, write=True, generation=generation)
            present = {str(row['id']) for row in await table.query().select(['id']).to_list()}
            batch = []
            async for row in source_rows(collection):
                meta = row.get('metadata') or {}
                if isinstance(meta, str):
                    meta = json.loads(meta)
                if row['id'] in present or not row.get('text') or not store._eligible(collection, row['id'], meta):
                    continue
                # Text re-embedding cannot recreate an image vector space.
                # Preserve the old generation; require a qualified multimodal
                # rebuild rather than silently encoding an empty/caption text.
                if meta.get('modality') == 'image':
                    raise ValueError('Retained image vectors require a qualified multimodal reindex')
                batch.append({**row, 'metadata': meta})
                if len(batch) >= batch_size:
                    await write_batch(collection, batch)
                    batch = []
            if batch:
                await write_batch(collection, batch)
            result.collections_migrated += 1
        store.catalog.promote(generation['id'], store.identity)
    except Exception as exc:
        # A retry resumes this generation. No deleted row is restored, and the
        # active pointer has not moved on a partial rebuild.
        message = f'{type(exc).__name__}: {exc}'
        result.errors.append(message)
        result.vectors_failed += len(locals().get('batch', []))
        store.catalog.finish(generation['id'], error=message)
    result.duration_s = time.monotonic() - started
    return result


def _update_env(model_id: str, pipeline) -> None:
    """Update .env file with the new model configuration."""
    env_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".env"
    if not env_path.exists():
        return

    try:
        lines = env_path.read_text().splitlines()
        updated = {}
        for i, line in enumerate(lines):
            if line.startswith("COLONY_EMBED_MODEL="):
                lines[i] = f"COLONY_EMBED_MODEL={model_id}"
                updated["model"] = True
            elif line.startswith("COLONY_EMBED_DIMS="):
                dims = pipeline.dimensions if hasattr(pipeline, "dimensions") else ""
                lines[i] = f"COLONY_EMBED_DIMS={dims}"
                updated["dims"] = True

        env_path.write_text("\n".join(lines) + "\n")
        if updated:
            logger.info("Updated .env with new model config: %s", updated)
    except Exception as exc:
        logger.warning("Failed to update .env: %s", exc)
