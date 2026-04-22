"""Colony Vector Store — backfill and re-embedding.

Re-embeds all vectors in one or more collections using the current
embedding pipeline.  Skips rows already tagged with the current model
(idempotent).  Marks every re-embedded vector with model_id and
embedded_at metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    """Result of a backfill run."""

    total: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0  # already had current model_id
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)


async def backfill(
    store,
    pipeline,
    collection: Optional[str] = None,
    batch_size: int = 64,
    current_model_id: Optional[str] = None,
) -> BackfillResult:
    """Re-embed vectors in the store using the current pipeline.

    Parameters
    ----------
    store : VectorStore
        The LanceDB vector store.
    pipeline : EmbeddingPipeline
        The current embedding pipeline.
    collection : str, optional
        Specific collection to backfill.  If None, backfill all.
    batch_size : int
        Number of rows to embed per batch.
    current_model_id : str, optional
        The model_id of the current embedder.  Rows with this model_id
        in their metadata are skipped (idempotent).

    Returns
    -------
    BackfillResult
    """
    from colony_sidecar.vector.collections import Collection

    start = time.monotonic()
    result = BackfillResult()

    # Resolve current model_id from pipeline if not provided
    if current_model_id is None:
        if hasattr(pipeline, "_provider") and hasattr(pipeline._provider, "_config"):
            current_model_id = pipeline._provider._config.model_id
        else:
            current_model_id = ""

    # Determine collections to process
    if collection is not None:
        try:
            collections = [Collection(collection)]
        except ValueError:
            result.errors.append(f"Unknown collection: {collection}")
            result.duration_s = time.monotonic() - start
            return result
    else:
        collections = list(Collection)

    for col in collections:
        try:
            col_result = await _backfill_collection(
                store, pipeline, col, batch_size, current_model_id
            )
            result.total += col_result.total
            result.processed += col_result.processed
            result.failed += col_result.failed
            result.skipped += col_result.skipped
            result.errors.extend(col_result.errors)
        except Exception as exc:
            msg = f"Collection {col.value} backfill failed: {exc}"
            logger.error(msg)
            result.errors.append(msg)

    result.duration_s = time.monotonic() - start
    return result


async def _backfill_collection(
    store,
    pipeline,
    collection,
    batch_size: int,
    current_model_id: str,
) -> BackfillResult:
    """Backfill a single collection."""
    result = BackfillResult()

    # Scan all rows
    try:
        rows = await store.scan_all(collection)
    except Exception as exc:
        result.errors.append(f"scan failed: {exc}")
        return result

    result.total = len(rows)

    # Separate into skip vs re-embed
    to_embed: list[dict] = []
    for row in rows:
        meta_str = row.get("metadata", "{}")
        try:
            meta = json.loads(meta_str) if isinstance(meta_str, str) else (meta_str or {})
        except (json.JSONDecodeError, TypeError):
            meta = {}

        if current_model_id and meta.get("model_id") == current_model_id:
            result.skipped += 1
            continue
        to_embed.append(row)

    # Batch re-embed
    now = time.time()
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [row.get("text", "") for row in batch]

        try:
            vectors = await pipeline.embed_batch(texts)
        except Exception as exc:
            result.failed += len(batch)
            result.errors.append(f"embed batch failed: {exc}")
            continue

        # Upsert each row with new vector + metadata
        for row, vector in zip(batch, vectors):
            try:
                meta_str = row.get("metadata", "{}")
                try:
                    meta = json.loads(meta_str) if isinstance(meta_str, str) else (meta_str or {})
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["model_id"] = current_model_id
                meta["embedded_at"] = now

                await store.update(
                    collection,
                    id=row["id"],
                    text=row.get("text", ""),
                    vector=vector,
                    metadata=meta,
                )
                result.processed += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"update {row.get('id', '?')} failed: {exc}")

    return result
