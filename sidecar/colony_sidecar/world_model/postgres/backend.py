"""PostgreSQL storage backend for the Colony World Model.

Requires asyncpg. Install with: pip install colony-sidecar[postgres]

Same interface as SQLiteBackend — drop-in replacement for larger deployments.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..constants import (
    ENTITY_ID_PREFIX,
    RELATIONSHIP_ID_PREFIX,
    OBSERVATION_ID_PREFIX,
    MERGE_PROPOSAL_ID_PREFIX,
    MERGE_AUDIT_ID_PREFIX,
)
from ..entities import BaseEntity, ENTITY_CLASS_MAP
from ..relationships import WorldRelationship

logger = logging.getLogger(__name__)

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False


def _generate_id(prefix: str) -> str:
    import secrets
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(6)
    return f"{prefix}-{ts}-{rand}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entity_to_params(entity: BaseEntity) -> dict:
    """Flatten a BaseEntity to a dict suitable for Postgres insertion."""
    import dataclasses
    from ..entities import BaseEntity as BaseEntityClass

    base_fields = {f.name for f in dataclasses.fields(BaseEntityClass)}
    extra: Dict[str, Any] = dict(entity.properties)
    for f in dataclasses.fields(entity):
        if f.name not in base_fields:
            v = getattr(entity, f.name)
            if v is not None:
                extra[f.name] = v

    return {
        "id": entity.id,
        "name": entity.name,
        "entity_type": entity.entity_type,
        "aliases": json.dumps(entity.aliases),
        "external_ids": json.dumps(entity.external_ids),
        "confidence": entity.confidence,
        "properties": json.dumps(extra),
        "first_seen": entity.first_seen.strftime("%Y-%m-%dT%H:%M:%SZ") if entity.first_seen else _now_iso(),
        "last_seen": entity.last_seen.strftime("%Y-%m-%dT%H:%M:%SZ") if entity.last_seen else _now_iso(),
        "created_at": entity.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if entity.created_at else _now_iso(),
        "updated_at": _now_iso(),
    }


def _record_to_entity(record) -> BaseEntity:
    """Reconstruct an entity from a Postgres record."""
    d = dict(record)
    # Handle asyncpg Record objects
    if not isinstance(d, dict):
        d = {k: v for k, v in record.items()}

    d["aliases"] = json.loads(d.get("aliases") or "[]")
    d["external_ids"] = json.loads(d.get("external_ids") or "{}")
    props = json.loads(d.get("properties") or "{}")
    d["properties"] = {}

    entity_type = d.get("entity_type", "")
    cls = ENTITY_CLASS_MAP.get(entity_type, BaseEntity)
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(cls)}

    for k, v in props.items():
        if k in valid_fields:
            d[k] = v
        else:
            d["properties"][k] = v

    for dt_field in ("first_seen", "last_seen", "created_at", "updated_at"):
        val = d.get(dt_field)
        if val and isinstance(val, str):
            try:
                d[dt_field] = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                d[dt_field] = None
        elif val and isinstance(val, datetime):
            pass  # Already a datetime

    filtered = {k: v for k, v in d.items() if k in valid_fields}
    return cls(**filtered)


def _record_to_relationship(record) -> WorldRelationship:
    d = dict(record)
    if not isinstance(d, dict):
        d = {k: v for k, v in record.items()}
    d["properties"] = json.loads(d.get("properties") or "{}")
    return WorldRelationship(
        id=d["id"],
        source_id=d["source_id"],
        target_id=d["target_id"],
        relationship_type=d["relationship_type"],
        confidence=d.get("confidence", 0.5),
        valid_from=d.get("valid_from"),
        valid_to=d.get("valid_to"),
        properties=d["properties"],
        source_observation_id=d.get("source_observation_id"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _fts_escape(q: str, max_len: int = 200) -> str:
    safe = re.sub(r'[^\w\s]', '', q[:max_len], flags=re.UNICODE)
    return safe.strip()


class PostgresBackend:
    """Async PostgreSQL backend for the World Model.

    Requires asyncpg and a running Postgres instance.
    Configure via WORLD_MODEL_PG_CONNECTION environment variable.
    """

    def __init__(self, connection_string: str) -> None:
        if not HAS_ASYNCPG:
            raise ImportError("asyncpg is required for the Postgres backend. Install with: pip install colony-sidecar[postgres]")
        self._conn_string = connection_string
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._conn_string, min_size=2, max_size=10)
        await self._apply_schema()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def _apply_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wm_entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    aliases JSONB DEFAULT '[]',
                    external_ids JSONB DEFAULT '{}',
                    confidence REAL DEFAULT 0.5,
                    properties JSONB DEFAULT '{}',
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_seen TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_wm_entities_type ON wm_entities(entity_type);
                CREATE INDEX IF NOT EXISTS idx_wm_entities_name ON wm_entities USING gin(to_tsvector('english', name));

                CREATE TABLE IF NOT EXISTS wm_relationships (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL REFERENCES wm_entities(id) ON DELETE CASCADE,
                    relationship_type TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    valid_from TIMESTAMPTZ,
                    valid_to TIMESTAMPTZ,
                    properties JSONB DEFAULT '{}',
                    source_observation_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_wm_rels_source ON wm_relationships(source_id);
                CREATE INDEX IF NOT EXISTS idx_wm_rels_target ON wm_relationships(target_id);
                CREATE INDEX IF NOT EXISTS idx_wm_rels_type ON wm_relationships(relationship_type);

                CREATE TABLE IF NOT EXISTS wm_observations (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT REFERENCES wm_entities(id) ON DELETE SET NULL,
                    relationship_id TEXT REFERENCES wm_relationships(id) ON DELETE SET NULL,
                    observation TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS wm_merge_proposals (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    existing_id TEXT NOT NULL,
                    match_confidence REAL DEFAULT 0.0,
                    match_reason TEXT DEFAULT '',
                    evidence JSONB DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    resolved_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS wm_merge_log (
                    id TEXT PRIMARY KEY,
                    surviving_id TEXT NOT NULL,
                    retired_id TEXT NOT NULL,
                    relationships_repointed INTEGER DEFAULT 0,
                    properties_updated INTEGER DEFAULT 0,
                    executed_by TEXT DEFAULT '',
                    merge_proposal_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS wm_schema_migrations (
                    version TEXT PRIMARY KEY,
                    description TEXT DEFAULT '',
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Record migrations
            for version, desc in [
                ("wm-001", "Create base entity tables and GIN index"),
                ("wm-002", "Create relationship table"),
                ("wm-003", "Create observations table"),
                ("wm-004", "Create merge proposals and audit log"),
                ("wm-005", "Create schema migrations table"),
            ]:
                await conn.execute(
                    "INSERT INTO wm_schema_migrations (version, description) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    version, desc,
                )

    # ── Entity CRUD ───────────────────────────────────────────────────────────

    async def get_entity(self, entity_id: str) -> Optional[BaseEntity]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM wm_entities WHERE id = $1", entity_id)
        return _record_to_entity(row) if row else None

    async def get_entity_by_external_id(self, key: str, value: str) -> Optional[BaseEntity]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wm_entities WHERE external_ids->>$1 = $2 LIMIT 1",
                key, value,
            )
        return _record_to_entity(row) if row else None

    async def find_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        min_confidence: float = 0.30,
        limit: int = 20,
    ) -> List[BaseEntity]:
        async with self._pool.acquire() as conn:
            if query:
                sql = """
                    SELECT * FROM wm_entities
                    WHERE to_tsvector('english', name) @@ to_tsquery('english', $1)
                      AND confidence >= $2
                """
                # Convert to tsquery format
                ts_query = " | ".join(_fts_escape(query).split())
                params = [ts_query, min_confidence]
                param_idx = 3
                if entity_type:
                    sql += f" AND entity_type = ${param_idx}"
                    params.append(entity_type)
                    param_idx += 1
                sql += f" ORDER BY confidence DESC LIMIT ${param_idx}"
                params.append(limit)
                rows = await conn.fetch(sql, *params)
            else:
                sql = "SELECT * FROM wm_entities WHERE confidence >= $1"
                params = [min_confidence]
                param_idx = 2
                if entity_type:
                    sql += f" AND entity_type = ${param_idx}"
                    params.append(entity_type)
                    param_idx += 1
                sql += f" ORDER BY confidence DESC LIMIT ${param_idx}"
                params.append(limit)
                rows = await conn.fetch(sql, *params)
        return [_record_to_entity(r) for r in rows]

    async def upsert_entity(self, entity: BaseEntity) -> BaseEntity:
        if not entity.id:
            entity.id = _generate_id(ENTITY_ID_PREFIX)
        p = _entity_to_params(entity)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO wm_entities
                  (id, name, entity_type, aliases, external_ids, properties,
                   confidence, first_seen, last_seen, created_at, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb,
                        $7, $8, $9, $10, $11)
                ON CONFLICT(id) DO UPDATE SET
                  name = EXCLUDED.name,
                  aliases = EXCLUDED.aliases,
                  external_ids = EXCLUDED.external_ids,
                  properties = EXCLUDED.properties,
                  confidence = EXCLUDED.confidence,
                  last_seen = EXCLUDED.last_seen,
                  updated_at = EXCLUDED.updated_at
            """, p["id"], p["name"], p["entity_type"], p["aliases"], p["external_ids"],
                 p["properties"], p["confidence"], p["first_seen"], p["last_seen"],
                 p["created_at"], p["updated_at"])
        return entity

    async def update_entity_property(
        self, entity_id: str, property_key: str, property_value: Any, confidence: float
    ) -> None:
        entity = await self.get_entity(entity_id)
        if not entity:
            return
        import dataclasses
        existing_fields = {f.name for f in dataclasses.fields(type(entity))}
        if property_key in existing_fields:
            setattr(entity, property_key, property_value)
            if confidence > entity.confidence:
                entity.confidence = confidence
            await self.upsert_entity(entity)
        else:
            if confidence >= entity.properties.get(f"_conf_{property_key}", 0.0):
                entity.properties[property_key] = property_value
                entity.properties[f"_conf_{property_key}"] = confidence
                await self.upsert_entity(entity)

    async def add_entity_alias(self, entity_id: str, alias: str) -> None:
        entity = await self.get_entity(entity_id)
        if entity and alias not in entity.aliases:
            entity.aliases.append(alias)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE wm_entities SET aliases = $1::jsonb, updated_at = $2 WHERE id = $3",
                    json.dumps(entity.aliases), _now_iso(), entity_id,
                )

    async def delete_entity(self, entity_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM wm_entities WHERE id = $1", entity_id)

    # ── Relationship CRUD ─────────────────────────────────────────────────────

    async def get_relationship(self, rel_id: str) -> Optional[WorldRelationship]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM wm_relationships WHERE id = $1", rel_id)
        return _record_to_relationship(row) if row else None

    async def upsert_relationship(self, rel: WorldRelationship) -> WorldRelationship:
        if not rel.id:
            rel.id = _generate_id(RELATIONSHIP_ID_PREFIX)
        now = _now_iso()
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO wm_relationships
                  (id, source_id, target_id, relationship_type, confidence,
                   valid_from, valid_to, properties, source_observation_id,
                   created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
                ON CONFLICT(id) DO UPDATE SET
                  confidence = EXCLUDED.confidence,
                  valid_from = EXCLUDED.valid_from,
                  valid_to = EXCLUDED.valid_to,
                  properties = EXCLUDED.properties,
                  updated_at = EXCLUDED.updated_at
            """, rel.id, rel.source_id, rel.target_id, rel.relationship_type,
                 rel.confidence, rel.valid_from, rel.valid_to,
                 json.dumps(rel.properties), rel.source_observation_id,
                 rel.created_at or now, now)
        return rel

    async def close_relationship(self, rel_id: str, valid_to: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE wm_relationships SET valid_to = $1, updated_at = $2 WHERE id = $3",
                valid_to, _now_iso(), rel_id,
            )

    async def query_relationships(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        relationship_type: Optional[str] = None,
        target_types: Optional[List[str]] = None,
        active_only: bool = False,
        min_confidence: float = 0.30,
        limit: int = 100,
    ) -> List[WorldRelationship]:
        conditions = ["r.confidence >= $1"]
        params: list = [min_confidence]
        idx = 2

        sql = "SELECT r.* FROM wm_relationships r"

        if target_types:
            sql += " JOIN wm_entities te ON te.id = r.target_id"
            placeholders = ", ".join(f"${idx + i}" for i in range(len(target_types)))
            conditions.append(f"te.entity_type IN ({placeholders})")
            params.extend(target_types)
            idx += len(target_types)

        if source_id:
            conditions.append(f"r.source_id = ${idx}")
            params.append(source_id)
            idx += 1
        if target_id:
            conditions.append(f"r.target_id = ${idx}")
            params.append(target_id)
            idx += 1
        if relationship_type:
            conditions.append(f"r.relationship_type = ${idx}")
            params.append(relationship_type)
            idx += 1
        if active_only:
            conditions.append("r.valid_to IS NULL")

        sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY r.confidence DESC LIMIT ${idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_record_to_relationship(r) for r in rows]

    async def query_at_time(
        self,
        entity_id: str,
        as_of: str,
        relationship_types: Optional[List[str]] = None,
    ) -> List[WorldRelationship]:
        sql = """
            SELECT * FROM wm_relationships
            WHERE (source_id = $1 OR target_id = $2)
              AND (valid_from IS NULL OR valid_from <= $3)
              AND (valid_to IS NULL OR valid_to > $4)
        """
        params: list = [entity_id, entity_id, as_of, as_of]
        if relationship_types:
            placeholders = ", ".join(f"${5 + i}" for i in range(len(relationship_types)))
            sql += f" AND relationship_type IN ({placeholders})"
            params.extend(relationship_types)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_record_to_relationship(r) for r in rows]

    # ── Graph Traversal ───────────────────────────────────────────────────────

    async def get_neighbors(
        self,
        entity_id: str,
        min_confidence: float = 0.30,
        relationship_types: Optional[List[str]] = None,
    ) -> List[tuple]:
        sql = """
            SELECT e.*, r.id as rel_id, r.source_id, r.target_id,
                   r.relationship_type, r.confidence as rel_confidence,
                   r.valid_from, r.valid_to, r.properties as rel_props,
                   r.source_observation_id, r.created_at as rel_created_at,
                   r.updated_at as rel_updated_at
            FROM wm_relationships r
            JOIN wm_entities e ON (
                (r.source_id = $1 AND e.id = r.target_id) OR
                (r.target_id = $2 AND e.id = r.source_id)
            )
            WHERE r.confidence >= $3 AND e.confidence >= $4
        """
        params: list = [entity_id, entity_id, min_confidence, min_confidence]
        if relationship_types:
            placeholders = ", ".join(f"${5 + i}" for i in range(len(relationship_types)))
            sql += f" AND r.relationship_type IN ({placeholders})"
            params.extend(relationship_types)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        results = []
        for row in rows:
            entity = _record_to_entity(row)
            d = dict(row) if isinstance(row, dict) else {k: v for k, v in row.items()}
            rel = WorldRelationship(
                id=d["rel_id"],
                source_id=d["source_id"],
                target_id=d["target_id"],
                relationship_type=d["relationship_type"],
                confidence=d["rel_confidence"],
                valid_from=d.get("valid_from"),
                valid_to=d.get("valid_to"),
                properties=json.loads(d.get("rel_props") or "{}") if isinstance(d.get("rel_props"), str) else (d.get("rel_props") or {}),
                source_observation_id=d.get("source_observation_id"),
                created_at=d.get("rel_created_at"),
                updated_at=d.get("rel_updated_at"),
            )
            results.append((entity, rel))
        return results

    # ── Observations ──────────────────────────────────────────────────────────

    async def add_observation(
        self,
        entity_id: Optional[str],
        relationship_id: Optional[str],
        observation: str,
        source: str,
    ) -> str:
        obs_id = _generate_id(OBSERVATION_ID_PREFIX)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO wm_observations (id, entity_id, relationship_id, observation, source) VALUES ($1, $2, $3, $4, $5)",
                obs_id, entity_id, relationship_id, observation, source,
            )
        return obs_id

    # ── Merge Proposals ───────────────────────────────────────────────────────

    async def create_merge_proposal(
        self,
        candidate_id: str,
        existing_id: str,
        confidence: float,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> str:
        proposal_id = _generate_id(MERGE_PROPOSAL_ID_PREFIX)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO wm_merge_proposals (id, candidate_id, existing_id, match_confidence, match_reason, evidence) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                proposal_id, candidate_id, existing_id, confidence, reason, json.dumps(evidence or {}),
            )
        return proposal_id

    async def get_merge_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM wm_merge_proposals WHERE id = $1", proposal_id)
        if not row:
            return None
        d = {k: v for k, v in row.items()}
        d["evidence"] = json.loads(d.get("evidence") or "{}") if isinstance(d.get("evidence"), str) else (d.get("evidence") or {})
        return d

    async def update_merge_proposal_status(self, proposal_id: str, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE wm_merge_proposals SET status = $1, resolved_at = $2 WHERE id = $3",
                status, _now_iso(), proposal_id,
            )

    async def get_pending_merge_proposals(self) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM wm_merge_proposals WHERE status = 'pending'")
        result = []
        for row in rows:
            d = {k: v for k, v in row.items()}
            d["evidence"] = json.loads(d.get("evidence") or "{}") if isinstance(d.get("evidence"), str) else (d.get("evidence") or {})
            result.append(d)
        return result

    # ── Merge Execution ───────────────────────────────────────────────────────

    async def execute_merge(
        self,
        surviving_id: str,
        retired_id: str,
        executed_by: str,
        proposal_id: Optional[str] = None,
    ) -> str:
        async with self._pool.acquire() as conn:
            # Count relationships to repoint
            rel_count = await conn.fetchval(
                "SELECT COUNT(*) FROM wm_relationships WHERE source_id = $1 OR target_id = $1",
                retired_id,
            )

            # Repoint
            await conn.execute("UPDATE wm_relationships SET source_id = $1 WHERE source_id = $2", surviving_id, retired_id)
            await conn.execute("UPDATE wm_relationships SET target_id = $1 WHERE target_id = $2", surviving_id, retired_id)

            # Merge aliases
            surviving = await self.get_entity(surviving_id)
            retired = await self.get_entity(retired_id)
            props_updated = 0
            if surviving and retired:
                new_aliases = surviving.aliases.copy()
                if retired_id not in new_aliases:
                    new_aliases.append(retired_id)
                for alias in retired.aliases:
                    if alias not in new_aliases:
                        new_aliases.append(alias)
                props_updated = len(new_aliases) - len(surviving.aliases)
                await conn.execute(
                    "UPDATE wm_entities SET aliases = $1::jsonb, updated_at = $2 WHERE id = $3",
                    json.dumps(new_aliases), _now_iso(), surviving_id,
                )

            # Delete retired entity
            await conn.execute("DELETE FROM wm_entities WHERE id = $1", retired_id)

            # Audit record
            audit_id = _generate_id(MERGE_AUDIT_ID_PREFIX)
            await conn.execute(
                "INSERT INTO wm_merge_log (id, surviving_id, retired_id, relationships_repointed, properties_updated, executed_by, merge_proposal_id) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                audit_id, surviving_id, retired_id, rel_count, props_updated, executed_by, proposal_id,
            )
        return audit_id

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        async with self._pool.acquire() as conn:
            type_rows = await conn.fetch("SELECT entity_type, COUNT(*) as cnt FROM wm_entities GROUP BY entity_type")
            total_entities = await conn.fetchval("SELECT COUNT(*) FROM wm_entities")
            total_rels = await conn.fetchval("SELECT COUNT(*) FROM wm_relationships")
            active_rels = await conn.fetchval("SELECT COUNT(*) FROM wm_relationships WHERE valid_to IS NULL")
            total_obs = await conn.fetchval("SELECT COUNT(*) FROM wm_observations")
            pending_proposals = await conn.fetchval("SELECT COUNT(*) FROM wm_merge_proposals WHERE status = 'pending'")

        return {
            "total_entities": total_entities,
            "entities_by_type": {r["entity_type"]: r["cnt"] for r in type_rows},
            "total_relationships": total_rels,
            "active_relationships": active_rels,
            "total_observations": total_obs,
            "merge_proposals_pending": pending_proposals,
        }
