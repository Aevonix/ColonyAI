"""Colony Vector Store — LanceDB wrapper.

Provides CRUD + ANN search across typed collections.  Each
``Collection`` maps to a separate LanceDB table.  All vector
operations degrade gracefully when the store is not initialized.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

import pyarrow as pa

from colony_sidecar.vector.collections import Collection
from colony_sidecar.vector.query import VectorItem, VectorResult

logger = logging.getLogger(__name__)

# LanceDB's json_extract() requires LargeBinary but our metadata column is Utf8.
# Detect filters that use json_extract(metadata, ...) so we can apply them in Python.
_METADATA_JSON_FILTER_RE = re.compile(
    r"\bjson_extract\s*\(\s*metadata\b", re.IGNORECASE
)

# Parse individual json_extract(metadata, '$.key') <op> <value> conditions.
_JSON_COND_RE = re.compile(
    r"json_extract\s*\(\s*metadata\s*,\s*['\"]?\$\.(\w+)['\"]?\s*\)"
    r"\s*(>=|<=|!=|<>|>|<|=|LIKE)"
    r"\s*(%?'[^']*'%?|%?\"[^\"]*\"%?|[-\d.]+)",
    re.IGNORECASE,
)


def _eval_metadata_filter(meta_str: str, filter_expr: str) -> bool:
    """Evaluate a filter expression containing json_extract(metadata,...) in Python.

    Handles the patterns actually used in the codebase:
    - ``json_extract(metadata, '$.key') >= <number>``
    - ``json_extract(metadata, '$.key') LIKE '%value%'``
    Multiple conditions are treated as AND (all must pass).
    """
    try:
        meta = json.loads(meta_str) if meta_str else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}

    for match in _JSON_COND_RE.finditer(filter_expr):
        key = match.group(1)
        op = match.group(2).upper()
        raw_val = match.group(3).strip()

        meta_val = meta.get(key)

        if op == "LIKE":
            # Strip surrounding % and quotes for a substring match
            pattern = raw_val.strip("'\"").replace("%", "")
            if pattern not in str(meta_val if meta_val is not None else ""):
                return False
            continue

        # Numeric or string comparison — strip quotes first
        val_str = raw_val.strip("'\"")
        try:
            num_val = float(val_str)
            num_meta = float(meta_val) if meta_val is not None else 0.0
            if op == ">=" and not (num_meta >= num_val):
                return False
            elif op == "<=" and not (num_meta <= num_val):
                return False
            elif op == ">" and not (num_meta > num_val):
                return False
            elif op == "<" and not (num_meta < num_val):
                return False
            elif op == "=" and not (num_meta == num_val):
                return False
            elif op in ("!=", "<>") and not (num_meta != num_val):
                return False
        except (ValueError, TypeError):
            str_meta = str(meta_val if meta_val is not None else "")
            if op == "=" and str_meta != val_str:
                return False
            elif op in ("!=", "<>") and str_meta == val_str:
                return False

    return True


def _base_schema(dims: int) -> pa.Schema:
    """Build the Arrow schema shared by all collection tables."""
    return pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), dims)),
        pa.field("metadata", pa.utf8()),
        pa.field("modality", pa.utf8()),
        pa.field("created_at", pa.float64()),
        pa.field("updated_at", pa.float64()),
    ])


class VectorStore:
    """LanceDB-backed vector store.  One table per Collection."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._db = None
        self._dims: int | None = None

    async def connect(self, dimensions: int) -> None:
        """Open (or create) the LanceDB database directory."""
        import lancedb

        os.makedirs(self._data_dir, exist_ok=True)
        self._db = await lancedb.connect_async(self._data_dir)
        self._dims = dimensions
        logger.info("VectorStore connected (path=%s, dims=%d)", self._data_dir, dimensions)

    async def ensure_collections(self, dimensions: int) -> None:
        """Create any missing collection tables."""
        if self._db is None:
            await self.connect(dimensions)

        existing = set(await self._db.table_names())
        schema = _base_schema(dimensions)
        for col in Collection:
            if col.value not in existing:
                await self._db.create_table(col.value, schema=schema)
                logger.info("Created vector collection: %s", col.value)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add(
        self,
        collection: Collection,
        id: str,
        text: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a single vector entry."""
        now = time.time()
        meta = metadata or {}
        meta.setdefault("embedded_at", now)
        meta.setdefault("modality", "text")

        # Dimension validation
        if self._dims and len(vector) != self._dims:
            raise ValueError(
                f"Vector dimension mismatch: table expects {self._dims}, got {len(vector)}. "
                "Run 'colony migrate-tier' to re-embed with the current model."
            )

        table = await self._db.open_table(collection.value)
        await table.add([{
            "id": id,
            "text": text,
            "vector": vector,
            "metadata": json.dumps(meta),
            "modality": meta.get("modality", "text"),
            "image_hash": meta.get("image_hash", ""),
            "image_ref": meta.get("image_ref", ""),
            "thumbnail_ref": meta.get("thumbnail_ref", ""),
            "caption": meta.get("caption", ""),
            "created_at": now,
            "updated_at": now,
        }])

    async def add_batch(
        self,
        collection: Collection,
        items: list[VectorItem],
    ) -> None:
        """Add a batch of vector entries."""
        if not items:
            return
        now = time.time()

        # Dimension validation on first item
        if self._dims and items and len(items[0].vector) != self._dims:
            raise ValueError(
                f"Vector dimension mismatch: table expects {self._dims}, got {len(items[0].vector)}. "
                "Run 'colony migrate-tier' to re-embed with the current model."
            )

        table = await self._db.open_table(collection.value)
        rows = []
        for item in items:
            meta = item.metadata or {}
            meta.setdefault("embedded_at", now)
            meta.setdefault("modality", "text")
            rows.append({
                "id": item.id,
                "text": item.text,
                "vector": item.vector,
                "metadata": json.dumps(meta),
                "modality": meta.get("modality", "text"),
                "image_hash": meta.get("image_hash", ""),
                "image_ref": meta.get("image_ref", ""),
                "thumbnail_ref": meta.get("thumbnail_ref", ""),
                "caption": meta.get("caption", ""),
                "created_at": now,
                "updated_at": now,
            })
        await table.add(rows)

    async def search(
        self,
        collection: Collection,
        query_vector: list[float],
        limit: int = 10,
        filter: Optional[str] = None,
        min_score: float = 0.0,
    ) -> list[VectorResult]:
        """ANN search on a collection.  Returns results sorted by score descending."""
        table = await self._db.open_table(collection.value)
        query = table.vector_search(query_vector).distance_type("cosine")

        if filter and _METADATA_JSON_FILTER_RE.search(filter):
            # json_extract(metadata, ...) requires LargeBinary but the column is
            # Utf8 — the SQL planner rejects it.  Fetch a larger candidate set
            # without the metadata filter and apply it in Python instead.
            query = query.limit(max(limit * 20, 200))
            raw = await query.to_pandas()
            mask = raw["metadata"].apply(
                lambda m: _eval_metadata_filter(str(m) if m is not None else "{}", filter)
            )
            results = raw[mask].head(limit)
        else:
            query = query.limit(limit)
            if filter:
                query = query.where(filter)
            results = await query.to_pandas()

        out: list[VectorResult] = []
        for _, row in results.iterrows():
            # LanceDB returns _distance (cosine distance); convert to similarity
            score = 1.0 - float(row.get("_distance", 0.0))
            if score < min_score:
                continue
            meta_str = row.get("metadata", "{}")
            try:
                meta = json.loads(meta_str) if isinstance(meta_str, str) else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            out.append(VectorResult(
                id=str(row["id"]),
                score=score,
                text=str(row.get("text", "")),
                metadata=meta,
            ))

        return out

    async def search_cross_modal(
        self,
        collection: Collection,
        query_vector: list[float],
        limit: int = 10,
        filter_modality: Optional[str] = None,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Cross-modal search — text query finds images, image query finds text.

        Works because multimodal models produce vectors in the same
        embedding space regardless of input type.

        Parameters
        ----------
        filter_modality : str, optional
            Only return results of this modality ("text" or "image").
            None = return all modalities.
        """
        filter_clause = None
        if filter_modality:
            filter_clause = f"modality = '{filter_modality}'"

        results = await self.search(
            collection, query_vector, limit=limit,
            filter=filter_clause, min_score=min_score,
        )

        # Enrich with modality-specific fields
        enriched = []
        for r in results:
            meta = r.metadata or {}
            entry = {
                "id": r.id,
                "score": r.score,
                "text": r.text,
                "modality": meta.get("modality", "text"),
                "image_ref": meta.get("image_ref", ""),
                "image_hash": meta.get("image_hash", ""),
                "thumbnail_ref": meta.get("thumbnail_ref", ""),
                "caption": meta.get("caption", ""),
                "metadata": meta,
            }
            enriched.append(entry)
        return enriched

    async def search_by_image_hash(self, collection: Collection, image_hash: str) -> Optional[VectorResult]:
        """Find an existing vector by image hash (for dedup)."""
        try:
            table = await self._db.open_table(collection.value)
            results = await table.search().where(f"image_hash = '{image_hash}'").limit(1).to_pandas()
            if results.empty:
                return None
            row = results.iloc[0]
            meta_str = row.get("metadata", "{}")
            try:
                meta = json.loads(meta_str) if isinstance(meta_str, str) else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            return VectorResult(
                id=str(row["id"]),
                score=1.0,
                text=str(row.get("text", "")),
                metadata=meta,
            )
        except Exception:
            return None

    async def delete(self, collection: Collection, id: str) -> None:
        """Delete a single entry by ID."""
        table = await self._db.open_table(collection.value)
        await table.delete(f"id = '{id}'")

    async def update(
        self,
        collection: Collection,
        id: str,
        text: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update an entry (delete + re-add)."""
        await self.delete(collection, id)
        await self.add(collection, id, text, vector, metadata)

    async def get(self, collection: Collection, id: str) -> Optional[VectorResult]:
        """Fetch a single entry by ID."""
        table = await self._db.open_table(collection.value)
        results = await table.search().where(f"id = '{id}'").limit(1).to_pandas()
        if results.empty:
            return None
        row = results.iloc[0]
        meta_str = row.get("metadata", "{}")
        try:
            meta = json.loads(meta_str) if isinstance(meta_str, str) else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return VectorResult(
            id=str(row["id"]),
            score=1.0,
            text=str(row.get("text", "")),
            metadata=meta,
        )

    async def count(self, collection: Collection) -> int:
        """Return the number of entries in a collection."""
        table = await self._db.open_table(collection.value)
        return await table.count_rows()

    async def scan_all(self, collection: Collection) -> list[dict[str, Any]]:
        """Return all rows from a collection as raw dicts."""
        table = await self._db.open_table(collection.value)
        df = await table.to_pandas()
        if df.empty:
            return []
        return df.to_dict(orient="records")

    async def get_stored_models(self) -> list[str]:
        """Return unique model_id values across all collections."""
        models: set[str] = set()
        for col in Collection:
            try:
                rows = await self.scan_all(col)
                for row in rows:
                    meta_str = row.get("metadata", "{}")
                    try:
                        meta = json.loads(meta_str) if isinstance(meta_str, str) else (meta_str or {})
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    model_id = meta.get("model_id", "")
                    if model_id:
                        models.add(model_id)
            except Exception:
                pass
        return sorted(models)

    async def close(self) -> None:
        """Release database resources."""
        self._db = None
