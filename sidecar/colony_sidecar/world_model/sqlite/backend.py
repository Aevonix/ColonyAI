"""SQLite storage backend for the Colony World Model."""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite


def _fts_escape(q: str, max_len: int = 200) -> str:
    """Strip FTS5 operators and special chars; keep only word chars and spaces.

    Caps input at max_len before processing to prevent regex DoS on huge inputs.
    """
    safe = re.sub(r'[^\w\s]', '', q[:max_len], flags=re.UNICODE)
    return safe.strip()

from ..constants import (
    ENTITY_ID_PREFIX,
    RELATIONSHIP_ID_PREFIX,
    OBSERVATION_ID_PREFIX,
    MERGE_PROPOSAL_ID_PREFIX,
    MERGE_AUDIT_ID_PREFIX,
)
from ..entities import BaseEntity, ENTITY_CLASS_MAP, entity_from_dict
from ..relationships import WorldRelationship


def _generate_id(prefix: str) -> str:
    """Generate a world model ID: prefix-<unix_ms>-<rand12>."""
    import secrets
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(6)  # 12 hex chars, 48 bits of CSPRNG entropy
    return f"{prefix}-{ts}-{rand}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entity_to_row(entity: BaseEntity) -> Dict[str, Any]:
    """Flatten a BaseEntity to a dict suitable for SQLite insertion."""
    import dataclasses

    row: Dict[str, Any] = {
        "id": entity.id,
        "name": entity.name,
        "entity_type": entity.entity_type,
        "aliases": json.dumps(entity.aliases),
        "external_ids": json.dumps(entity.external_ids),
        "confidence": entity.confidence,
        "properties": json.dumps(_collect_type_properties(entity)),
        "first_seen": (
            entity.first_seen.strftime("%Y-%m-%dT%H:%M:%SZ")
            if entity.first_seen
            else _now_iso()
        ),
        "last_seen": (
            entity.last_seen.strftime("%Y-%m-%dT%H:%M:%SZ")
            if entity.last_seen
            else _now_iso()
        ),
        "created_at": (
            entity.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if entity.created_at
            else _now_iso()
        ),
        "updated_at": _now_iso(),
    }
    return row


def _collect_type_properties(entity: BaseEntity) -> Dict[str, Any]:
    """Merge entity.properties with type-specific fields into a single dict."""
    import dataclasses
    from ..entities import BaseEntity

    base_fields = {f.name for f in dataclasses.fields(BaseEntity)}
    extra: Dict[str, Any] = dict(entity.properties)
    for f in dataclasses.fields(entity):
        if f.name not in base_fields:
            v = getattr(entity, f.name)
            if v is not None:
                extra[f.name] = v
    return extra


def _row_to_entity(row: aiosqlite.Row) -> BaseEntity:
    """Reconstruct an entity from a SQLite row."""
    d = dict(row)
    d["aliases"] = json.loads(d.get("aliases") or "[]")
    d["external_ids"] = json.loads(d.get("external_ids") or "{}")
    props = json.loads(d.get("properties") or "{}")
    d["properties"] = {}

    entity_type = d.get("entity_type", "")
    cls = ENTITY_CLASS_MAP.get(entity_type, BaseEntity)
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(cls)}

    # Pull type-specific fields out of properties into top-level keys
    for k, v in props.items():
        if k in valid_fields:
            d[k] = v
        else:
            d["properties"][k] = v

    # Parse datetime fields
    for dt_field in ("first_seen", "last_seen", "created_at", "updated_at"):
        val = d.get(dt_field)
        if val and isinstance(val, str):
            try:
                d[dt_field] = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                d[dt_field] = None

    filtered = {k: v for k, v in d.items() if k in valid_fields}
    return cls(**filtered)


def _row_to_relationship(row: aiosqlite.Row) -> WorldRelationship:
    """Reconstruct a WorldRelationship from a SQLite row."""
    d = dict(row)
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


_SCHEMA_DIR = Path(__file__).parent


class SQLiteBackend:
    """Async SQLite backend for the World Model.

    All methods are async and safe for concurrent use within a single process.
    """

    MIGRATIONS = [
        ("wm-001", "Create base entity tables and FTS index"),
        ("wm-002", "Create relationship table"),
        ("wm-003", "Create observations table"),
        ("wm-004", "Create merge proposals and audit log"),
        ("wm-005", "Create schema migrations table"),
    ]

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open database connection and apply schema."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._apply_schema()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _apply_schema(self) -> None:
        schema_path = _SCHEMA_DIR / "schema.sql"
        sql = schema_path.read_text()
        await self._db.executescript(sql)
        await self._db.commit()
        await self._record_migrations()

    async def _record_migrations(self) -> None:
        for version, description in self.MIGRATIONS:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO wm_schema_migrations(version, description)
                VALUES (?, ?)
                """,
                (version, description),
            )
        await self._db.commit()

    # ── Entity CRUD ───────────────────────────────────────────────────────────

    async def get_entity(self, entity_id: str) -> Optional[BaseEntity]:
        async with self._db.execute(
            "SELECT * FROM wm_entities WHERE id = ?", (entity_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_entity(row) if row else None

    async def get_entity_by_external_id(
        self, key: str, value: str
    ) -> Optional[BaseEntity]:
        """Find entity where external_ids JSON contains {key: value}."""
        # SQLite JSON1 function to extract the key
        async with self._db.execute(
            """
            SELECT * FROM wm_entities
            WHERE json_extract(external_ids, '$.' || ?) = ?
            LIMIT 1
            """,
            (key, value),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_entity(row) if row else None

    async def find_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        min_confidence: float = 0.30,
        limit: int = 20,
    ) -> List[BaseEntity]:
        """Full-text search on name + aliases."""
        rows = []
        if query:
            sql = """
                SELECT e.* FROM wm_entities e
                JOIN wm_entities_fts fts ON fts.id = e.id
                WHERE wm_entities_fts MATCH ?
                  AND e.confidence >= ?
            """
            params: list = [_fts_escape(query) + "*", min_confidence]
            if entity_type:
                sql += " AND e.entity_type = ?"
                params.append(entity_type)
            sql += " LIMIT ?"
            params.append(limit)
            async with self._db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        else:
            sql = "SELECT * FROM wm_entities WHERE confidence >= ?"
            params = [min_confidence]
            if entity_type:
                sql += " AND entity_type = ?"
                params.append(entity_type)
            sql += " ORDER BY confidence DESC LIMIT ?"
            params.append(limit)
            async with self._db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_entity(r) for r in rows]

    async def upsert_entity(self, entity: BaseEntity) -> BaseEntity:
        """Insert or replace an entity row."""
        if not entity.id:
            entity.id = _generate_id(ENTITY_ID_PREFIX)
        row = _entity_to_row(entity)
        await self._db.execute(
            """
            INSERT INTO wm_entities
              (id, name, entity_type, aliases, external_ids, properties,
               confidence, first_seen, last_seen, created_at, updated_at)
            VALUES
              (:id, :name, :entity_type, :aliases, :external_ids, :properties,
               :confidence, :first_seen, :last_seen, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
              name        = excluded.name,
              aliases     = excluded.aliases,
              external_ids = excluded.external_ids,
              properties  = excluded.properties,
              confidence  = excluded.confidence,
              last_seen   = excluded.last_seen,
              updated_at  = excluded.updated_at
            """,
            row,
        )
        await self._db.commit()
        return entity

    async def update_entity_property(
        self, entity_id: str, property_key: str, property_value: Any, confidence: float
    ) -> None:
        """Update a property only if new confidence is higher than existing."""
        entity = await self.get_entity(entity_id)
        if not entity:
            return
        import dataclasses
        existing_fields = {f.name for f in dataclasses.fields(type(entity))}
        if property_key in existing_fields:
            # Update the field directly
            setattr(entity, property_key, property_value)
            if confidence > entity.confidence:
                entity.confidence = confidence
            await self.upsert_entity(entity)
        else:
            # Store in properties JSON
            if confidence >= entity.properties.get(f"_conf_{property_key}", 0.0):
                entity.properties[property_key] = property_value
                entity.properties[f"_conf_{property_key}"] = confidence
                await self.upsert_entity(entity)

    async def add_entity_alias(self, entity_id: str, alias: str) -> None:
        entity = await self.get_entity(entity_id)
        if entity and alias not in entity.aliases:
            entity.aliases.append(alias)
            await self._db.execute(
                "UPDATE wm_entities SET aliases = ?, updated_at = ? WHERE id = ?",
                (json.dumps(entity.aliases), _now_iso(), entity_id),
            )
            await self._db.commit()

    async def delete_entity(self, entity_id: str) -> None:
        await self._db.execute("DELETE FROM wm_entities WHERE id = ?", (entity_id,))
        await self._db.commit()

    # ── Relationship CRUD ─────────────────────────────────────────────────────

    async def get_relationship(self, rel_id: str) -> Optional[WorldRelationship]:
        async with self._db.execute(
            "SELECT * FROM wm_relationships WHERE id = ?", (rel_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_relationship(row) if row else None

    async def upsert_relationship(self, rel: WorldRelationship) -> WorldRelationship:
        if not rel.id:
            rel.id = _generate_id(RELATIONSHIP_ID_PREFIX)
        now = _now_iso()
        await self._db.execute(
            """
            INSERT INTO wm_relationships
              (id, source_id, target_id, relationship_type, confidence,
               valid_from, valid_to, properties, source_observation_id,
               created_at, updated_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              confidence  = excluded.confidence,
              valid_from  = excluded.valid_from,
              valid_to    = excluded.valid_to,
              properties  = excluded.properties,
              updated_at  = excluded.updated_at
            """,
            (
                rel.id,
                rel.source_id,
                rel.target_id,
                rel.relationship_type,
                rel.confidence,
                rel.valid_from,
                rel.valid_to,
                json.dumps(rel.properties),
                rel.source_observation_id,
                rel.created_at or now,
                now,
            ),
        )
        await self._db.commit()
        return rel

    async def close_relationship(self, rel_id: str, valid_to: str) -> None:
        await self._db.execute(
            "UPDATE wm_relationships SET valid_to = ?, updated_at = ? WHERE id = ?",
            (valid_to, _now_iso(), rel_id),
        )
        await self._db.commit()

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
        sql = "SELECT r.* FROM wm_relationships r"
        conditions = ["r.confidence >= ?"]
        params: list = [min_confidence]

        if target_types:
            sql += " JOIN wm_entities te ON te.id = r.target_id"
            placeholders = ",".join("?" * len(target_types))
            conditions.append(f"te.entity_type IN ({placeholders})")
            params.extend(target_types)

        if source_id:
            conditions.append("r.source_id = ?")
            params.append(source_id)
        if target_id:
            conditions.append("r.target_id = ?")
            params.append(target_id)
        if relationship_type:
            conditions.append("r.relationship_type = ?")
            params.append(relationship_type)
        if active_only:
            conditions.append("r.valid_to IS NULL")

        sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY r.confidence DESC LIMIT ?"
        params.append(limit)

        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_relationship(r) for r in rows]

    async def query_at_time(
        self,
        entity_id: str,
        as_of: str,
        relationship_types: Optional[List[str]] = None,
    ) -> List[WorldRelationship]:
        """Relationships for entity active at as_of datetime."""
        sql = """
            SELECT * FROM wm_relationships
            WHERE (source_id = ? OR target_id = ?)
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to > ?)
        """
        params: list = [entity_id, entity_id, as_of, as_of]
        if relationship_types:
            placeholders = ",".join("?" * len(relationship_types))
            sql += f" AND relationship_type IN ({placeholders})"
            params.extend(relationship_types)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_relationship(r) for r in rows]

    # ── Graph Traversal ───────────────────────────────────────────────────────

    async def get_neighbors(
        self,
        entity_id: str,
        min_confidence: float = 0.30,
        relationship_types: Optional[List[str]] = None,
    ) -> List[tuple]:
        """Return (neighbor_entity, relationship) pairs for direct neighbors."""
        sql = """
            SELECT e.*, r.id as rel_id, r.source_id, r.target_id,
                   r.relationship_type, r.confidence as rel_confidence,
                   r.valid_from, r.valid_to, r.properties as rel_props,
                   r.source_observation_id, r.created_at as rel_created_at,
                   r.updated_at as rel_updated_at
            FROM wm_relationships r
            JOIN wm_entities e ON (
                (r.source_id = ? AND e.id = r.target_id) OR
                (r.target_id = ? AND e.id = r.source_id)
            )
            WHERE r.confidence >= ? AND e.confidence >= ?
        """
        params: list = [entity_id, entity_id, min_confidence, min_confidence]
        if relationship_types:
            placeholders = ",".join("?" * len(relationship_types))
            sql += f" AND r.relationship_type IN ({placeholders})"
            params.extend(relationship_types)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()

        results = []
        for row in rows:
            d = dict(row)
            entity = _row_to_entity(row)
            rel = WorldRelationship(
                id=d["rel_id"],
                source_id=d["source_id"],
                target_id=d["target_id"],
                relationship_type=d["relationship_type"],
                confidence=d["rel_confidence"],
                valid_from=d.get("valid_from"),
                valid_to=d.get("valid_to"),
                properties=json.loads(d.get("rel_props") or "{}"),
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
        await self._db.execute(
            """
            INSERT INTO wm_observations(id, entity_id, relationship_id, observation, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (obs_id, entity_id, relationship_id, observation, source),
        )
        await self._db.commit()
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
        await self._db.execute(
            """
            INSERT INTO wm_merge_proposals
              (id, candidate_id, existing_id, match_confidence, match_reason, evidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                candidate_id,
                existing_id,
                confidence,
                reason,
                json.dumps(evidence or {}),
            ),
        )
        await self._db.commit()
        return proposal_id

    async def get_merge_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        async with self._db.execute(
            "SELECT * FROM wm_merge_proposals WHERE id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["evidence"] = json.loads(d.get("evidence") or "{}")
        return d

    async def update_merge_proposal_status(
        self, proposal_id: str, status: str
    ) -> None:
        await self._db.execute(
            """
            UPDATE wm_merge_proposals
            SET status = ?, resolved_at = ?
            WHERE id = ?
            """,
            (status, _now_iso(), proposal_id),
        )
        await self._db.commit()

    async def get_pending_merge_proposals(self) -> List[Dict[str, Any]]:
        async with self._db.execute(
            "SELECT * FROM wm_merge_proposals WHERE status = 'pending'"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["evidence"] = json.loads(d.get("evidence") or "{}")
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
        """Repoint all relationships from retired_id to surviving_id."""
        # Count relationships to update
        async with self._db.execute(
            "SELECT COUNT(*) FROM wm_relationships WHERE source_id = ? OR target_id = ?",
            (retired_id, retired_id),
        ) as cur:
            row = await cur.fetchone()
            rel_count = row[0] if row else 0

        # Repoint source references
        await self._db.execute(
            "UPDATE wm_relationships SET source_id = ? WHERE source_id = ?",
            (surviving_id, retired_id),
        )
        # Repoint target references
        await self._db.execute(
            "UPDATE wm_relationships SET target_id = ? WHERE target_id = ?",
            (surviving_id, retired_id),
        )

        # Merge aliases: add retired_id as alias and copy aliases
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
            await self._db.execute(
                "UPDATE wm_entities SET aliases = ?, updated_at = ? WHERE id = ?",
                (json.dumps(new_aliases), _now_iso(), surviving_id),
            )
            props_updated = len(new_aliases) - len(surviving.aliases)

        # Delete retired entity (CASCADE handles obs/proposals)
        await self._db.execute(
            "DELETE FROM wm_entities WHERE id = ?", (retired_id,)
        )

        # Write audit record
        audit_id = _generate_id(MERGE_AUDIT_ID_PREFIX)
        await self._db.execute(
            """
            INSERT INTO wm_merge_log
              (id, surviving_id, retired_id, relationships_repointed,
               properties_updated, executed_by, merge_proposal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                surviving_id,
                retired_id,
                rel_count,
                props_updated,
                executed_by,
                proposal_id,
            ),
        )
        await self._db.commit()
        return audit_id

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        async with self._db.execute(
            "SELECT entity_type, COUNT(*) as cnt FROM wm_entities GROUP BY entity_type"
        ) as cur:
            type_rows = await cur.fetchall()

        async with self._db.execute("SELECT COUNT(*) FROM wm_entities") as cur:
            row = await cur.fetchone()
            total_entities = row[0] if row else 0

        async with self._db.execute("SELECT COUNT(*) FROM wm_relationships") as cur:
            row = await cur.fetchone()
            total_rels = row[0] if row else 0

        async with self._db.execute(
            "SELECT COUNT(*) FROM wm_relationships WHERE valid_to IS NULL"
        ) as cur:
            row = await cur.fetchone()
            active_rels = row[0] if row else 0

        async with self._db.execute("SELECT COUNT(*) FROM wm_observations") as cur:
            row = await cur.fetchone()
            total_obs = row[0] if row else 0

        async with self._db.execute(
            "SELECT COUNT(*) FROM wm_merge_proposals WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
            pending_proposals = row[0] if row else 0

        return {
            "total_entities": total_entities,
            "entities_by_type": {r["entity_type"]: r["cnt"] for r in type_rows},
            "total_relationships": total_rels,
            "active_relationships": active_rels,
            "total_observations": total_obs,
            "merge_proposals_pending": pending_proposals,
        }
