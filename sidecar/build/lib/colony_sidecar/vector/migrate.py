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
