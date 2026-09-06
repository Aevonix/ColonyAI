"""Colony Vector Store — LanceDB wrapper.

Provides CRUD + ANN search across typed collections.  Each
``Collection`` maps to a separate LanceDB table.  All vector
operations degrade gracefully when the store is not initialized.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
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
        pa.field("scope_key", pa.utf8()),
        pa.field("modality", pa.utf8()),
        pa.field("image_hash", pa.utf8()),
        pa.field("image_ref", pa.utf8()),
        pa.field("thumbnail_ref", pa.utf8()),
        pa.field("caption", pa.utf8()),
        pa.field("created_at", pa.float64()),
        pa.field("updated_at", pa.float64()),
    ])


class VectorStore:
    """LanceDB-backed vector store.  One table per Collection."""

    def __init__(self, data_dir: str, *, identity=None, catalog=None) -> None:
        self._data_dir = data_dir
        self._db = None
        self._dims: int | None = None
        self.identity = identity
        self.catalog = catalog
        if (identity is None) != (catalog is None):
            raise ValueError('Managed indexes require both embedding identity and source-ledger catalog')
        self._generation_dbs = {}

    async def connect(self, dimensions: int) -> None:
        """Open (or create) the LanceDB database directory."""
        import lancedb

        os.makedirs(self._data_dir, exist_ok=True)
        self._db = await lancedb.connect_async(self._data_dir)
        self._dims = dimensions
        if self.identity is not None and self.identity.dimensions != dimensions:
            raise ValueError('Embedding identity dimension disagrees with selected pipeline')
        if self.catalog is not None:
            self.catalog.initialize(self.identity, legacy_exists=bool(await self._db.table_names()))
            self._generation_dbs['legacy'] = self._db
        logger.info("VectorStore connected (path=%s, dims=%d)", self._data_dir, dimensions)

    async def ensure_collections(self, dimensions: int) -> None:
        """Create any missing collection tables."""
        if self._db is None:
            await self.connect(dimensions)

        db = self._db
        if self.catalog is not None:
            active = self.catalog.active()
            if active['fingerprint'] != self.identity.fingerprint:
                return  # Never relabel or overwrite an existing unknown index.
            db = await self._generation_db(active)
        existing = set(await db.table_names())
        schema = _base_schema(dimensions)
        for col in Collection:
            if col.value not in existing:
                await db.create_table(col.value, schema=schema)
                logger.info("Created vector collection: %s", col.value)

    async def _generation_db(self, generation):
        key = generation['id']
        if key not in self._generation_dbs:
            import lancedb
            if not re.fullmatch(r'[0-9a-f]{32}', key):
                raise ValueError('Invalid vector generation identifier')
            path = Path(self._data_dir) / 'generations' / key
            path.mkdir(parents=True, exist_ok=True)
            self._generation_dbs[key] = await lancedb.connect_async(str(path))
        return self._generation_dbs[key]

    async def _table(self, collection, *, write=False, semantic=False, generation=None):
        if self.catalog is None:
            return await self._db.open_table(collection.value)
        if generation is None:
            generation = (self.catalog.write_generation(self.identity) if write else
                          self.catalog.read_generation(self.identity) if semantic else self.catalog.active())
        db = await self._generation_db(generation)
        if write and collection.value not in await db.table_names():
            await db.create_table(collection.value, schema=_base_schema(generation['identity']['dimensions']), exist_ok=True)
        return await db.open_table(collection.value)

    def _eligible(self, collection, entry_id, metadata):
        return self.catalog is None or not (
            self.catalog.deleted(collection.value, str(entry_id)) or self.catalog.source_erased(metadata))

    def _validate_vector(self, vector):
        if self._dims and len(vector) != self._dims:
            raise ValueError(f'Vector dimension mismatch: expected {self._dims}, got {len(vector)}; rebuild a separate index generation')
        if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
            raise ValueError('Vector must contain finite nonzero values')

    @staticmethod
    def _quoted(value):
        return "'" + str(value).replace("'", "''") + "'"

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
        self._validate_vector(vector)
        if not self._eligible(collection, id, meta):
            return
        if self.identity is not None:
            meta = {**meta, 'embedding_fingerprint': self.identity.fingerprint,
                    'requested_model': self.identity.requested_model,
                    'served_model': self.identity.served_model,
                    'declared_revision': self.identity.declared_revision}

        table = await self._table(collection, write=True)
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
        if not self._eligible(collection, id, meta):
            await table.delete('id = ' + self._quoted(id))

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
        for item in items:
            self._validate_vector(item.vector)

        table = await self._table(collection, write=True)
        rows = []
        for item in items:
            meta = item.metadata or {}
            if not self._eligible(collection, item.id, meta):
                continue
            if self.identity is not None:
                meta = {**meta, 'embedding_fingerprint': self.identity.fingerprint}
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
        if rows:
            await table.add(rows)
            for item in items:
                if not self._eligible(collection, item.id, item.metadata):
                    await table.delete('id = ' + self._quoted(item.id))

    async def search(
        self,
        collection: Collection,
        query_vector: list[float],
        limit: int = 10,
        filter: Optional[str] = None,
        min_score: float = 0.0,
    ) -> list[VectorResult]:
        """ANN search on a collection.  Returns results sorted by score descending."""
        self._validate_vector(query_vector)
        table = await self._table(collection, semantic=True)
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
            if not self._eligible(collection, row['id'], meta):
                continue
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
            table = await self._table(collection, semantic=True)
            results = await table.query().where('image_hash = ' + self._quoted(image_hash)).limit(1).to_pandas()
            if results.empty:
                return None
            row = results.iloc[0]
            meta_str = row.get("metadata", "{}")
            try:
                meta = json.loads(meta_str) if isinstance(meta_str, str) else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if not self._eligible(collection, row['id'], meta):
                return None
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
        if self.catalog is None:
            table = await self._table(collection)
            await table.delete('id = ' + self._quoted(id))
            return
        self.catalog.delete(collection.value, id)
        # Exact ID removal covers active, staged, unknown legacy and retained generations.
        for generation in self.catalog.generations():
            db = await self._generation_db(generation)
            if collection.value in await db.table_names():
                await (await db.open_table(collection.value)).delete('id = ' + self._quoted(id))

    async def update(
        self,
        collection: Collection,
        id: str,
        text: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Validate first, then atomically replace an entry in one generation."""
        self._validate_vector(vector)
        meta = dict(metadata or {})
        if not self._eligible(collection, id, meta):
            return
        now = time.time()
        meta.setdefault('embedded_at', now)
        if self.identity is not None:
            meta['embedding_fingerprint'] = self.identity.fingerprint
        row = {'id': id, 'text': text, 'vector': vector, 'metadata': json.dumps(meta),
               'modality': meta.get('modality', 'text'), 'image_hash': meta.get('image_hash', ''),
               'image_ref': meta.get('image_ref', ''), 'thumbnail_ref': meta.get('thumbnail_ref', ''),
               'caption': meta.get('caption', ''), 'created_at': now, 'updated_at': now}
        table = await self._table(collection, write=True)
        await table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([row])
        if not self._eligible(collection, id, meta):
            await table.delete('id = ' + self._quoted(id))

    async def get(self, collection: Collection, id: str) -> Optional[VectorResult]:
        """Fetch a single entry by ID."""
        table = await self._table(collection)
        results = await table.query().where('id = ' + self._quoted(id)).limit(1).to_pandas()
        if results.empty:
            return None
        row = results.iloc[0]
        meta_str = row.get("metadata", "{}")
        try:
            meta = json.loads(meta_str) if isinstance(meta_str, str) else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not self._eligible(collection, row['id'], meta):
            return None
        return VectorResult(
            id=str(row["id"]),
            score=1.0,
            text=str(row.get("text", "")),
            metadata=meta,
        )

    async def count(self, collection: Collection) -> int:
        """Return the number of entries in a collection."""
        table = await self._table(collection)
        return await table.count_rows()

    async def list_ids(self, collection: Collection) -> list[str]:
        """Return all row ids in a collection via a projected query.

        Unlike ``scan_all`` this never materializes the vector column — an
        id-only projection, cheap enough to run against a large store (used
        by the orphan-vector vacuum to diff against graph node ids).
        """
        table = await self._table(collection)
        df = await table.query().select(["id"]).to_pandas()
        if df.empty:
            return []
        return [str(x) for x in df["id"].tolist()]

    async def scan_all(self, collection: Collection) -> list[dict[str, Any]]:
        """Return all rows from a collection as raw dicts."""
        table = await self._table(collection)
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
        self._generation_dbs.clear()

    async def erase_source_projections(self, turn_ids):
        """Remove exact linked rows even when the old graph row is already gone."""
        if self.catalog is None:
            return 0
        selected = {'turn:' + value for value in turn_ids}
        deleted = set()
        for generation in self.catalog.generations():
            db = await self._generation_db(generation)
            names = await db.table_names()
            for collection in Collection:
                if collection.value not in names:
                    continue
                table = await db.open_table(collection.value)
                rows = await table.query().select(['id', 'metadata']).to_list()
                for row in rows:
                    meta = json.loads(row['metadata'] or '{}')
                    if meta.get('source_uri') in selected and self.catalog.source_erased(meta):
                        self.catalog.delete(collection.value, row['id'])
                        await table.delete('id = ' + self._quoted(row['id']))
                        deleted.add((collection.value, row['id']))
        return len(deleted)
