"""Neo4j storage backend for the Colony World Model.

Implements the same interface as SQLiteBackend but backed by Neo4j,
which provides native graph traversal (Cypher) for relationship queries,
neighborhood traversal, and path finding.

Requires: ``neo4j`` driver (pip install neo4j)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..constants import (
    ENTITY_ID_PREFIX,
    RELATIONSHIP_ID_PREFIX,
    OBSERVATION_ID_PREFIX,
    MERGE_PROPOSAL_ID_PREFIX,
    MERGE_AUDIT_ID_PREFIX,
)
from ..entities import BaseEntity, ENTITY_CLASS_MAP, entity_from_dict
from ..relationships import WorldRelationship

logger = logging.getLogger(__name__)


def _generate_id(prefix: str) -> str:
    import secrets
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(6)
    return f"{prefix}-{ts}-{rand}"


def _entity_to_props(entity: BaseEntity) -> Dict[str, Any]:
    """Convert an entity dataclass to a flat dict for Neo4j node properties."""
    import dataclasses
    props = {}
    for f in dataclasses.fields(entity):
        val = getattr(entity, f.name)
        if val is None:
            continue
        if isinstance(val, (list, dict)):
            props[f.name] = json.dumps(val)
        elif isinstance(val, datetime):
            props[f.name] = val.isoformat()
        else:
            props[f.name] = val
    return props


def _props_to_entity(props: Dict[str, Any]) -> BaseEntity:
    """Convert Neo4j node properties back to an entity dataclass."""
    data = dict(props)
    # Deserialize JSON fields
    for key in ("aliases", "external_ids", "properties"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except (json.JSONDecodeError, TypeError):
                data[key] = [] if key == "aliases" else {}
    # Parse datetime fields
    for key in ("first_seen", "last_seen", "created_at", "updated_at"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except (ValueError, TypeError):
                data[key] = None
    return entity_from_dict(data)


def _rel_to_props(rel: WorldRelationship) -> Dict[str, Any]:
    """Convert a WorldRelationship to a flat dict for Neo4j edge properties."""
    import dataclasses
    props = {}
    for f in dataclasses.fields(rel):
        val = getattr(rel, f.name)
        if val is None:
            continue
        if isinstance(val, dict):
            props[f.name] = json.dumps(val)
        else:
            props[f.name] = val
    return props


def _props_to_rel(props: Dict[str, Any]) -> WorldRelationship:
    """Convert Neo4j edge properties back to a WorldRelationship."""
    data = dict(props)
    if "properties" in data and isinstance(data["properties"], str):
        try:
            data["properties"] = json.loads(data["properties"])
        except (json.JSONDecodeError, TypeError):
            data["properties"] = {}
    return WorldRelationship(**{k: v for k, v in data.items()
                                 if k in {f.name for f in __import__("dataclasses").fields(WorldRelationship)}})


class Neo4jBackend:
    """Neo4j-backed storage for the Colony World Model.

    Node labels: ``Entity`` (all entities share this label, with ``entity_type`` as a property)
    Relationship types: prefixed ``WM_`` types from constants
    """

    def __init__(self, uri: str, database: str = "colony",
                 username: str = "neo4j", password: str = "") -> None:
        self._uri = uri
        self._database = database
        self._username = username
        self._password = password
        self._driver = None

    async def connect(self) -> None:
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError:
            raise ImportError("neo4j driver not installed. Run: pip install neo4j")

        self._driver = AsyncGraphDatabase.driver(
            self._uri,
            auth=(self._username, self._password),
        )
        # Verify connectivity
        async with self._driver.session(database=self._database) as session:
            await session.run("RETURN 1").consume()
        logger.info("Neo4j backend connected: %s (db=%s)", self._uri, self._database)
        await self._apply_schema()

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def _apply_schema(self) -> None:
        """Create indexes and constraints for optimal query performance."""
        async with self._driver.session(database=self._database) as session:
            # Unique constraint on entity ID
            try:
                await session.run(
                    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
                    "FOR (e:Entity) REQUIRE e.id IS UNIQUE"
                ).consume()
            except Exception:
                pass  # Constraint may already exist

            # Index on entity_type for type-filtered queries
            try:
                await session.run(
                    "CREATE INDEX entity_type_index IF NOT EXISTS "
                    "FOR (e:Entity) ON (e.entity_type)"
                ).consume()
            except Exception:
                pass

            # Index on name for full-text search
            try:
                await session.run(
                    "CREATE INDEX entity_name_index IF NOT EXISTS "
                    "FOR (e:Entity) ON (e.name)"
                ).consume()
            except Exception:
                pass

            # Index on relationship ID
            try:
                await session.run(
                    "CREATE CONSTRAINT rel_id_unique IF NOT EXISTS "
                    "FOR ()-[r:WM_RELATED_TO]-() REQUIRE r.id IS UNIQUE"
                ).consume()
            except Exception:
                pass

            # Full-text index for entity search
            try:
                await session.run(
                    "CREATE FULLTEXT INDEX entity_search IF NOT EXISTS "
                    "FOR (e:Entity) ON EACH [e.name, e.aliases]"
                ).consume()
            except Exception:
                pass

    # ── Entity reads ──────────────────────────────────────────────────────

    async def get_entity(self, entity_id: str) -> Optional[BaseEntity]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) RETURN e",
                id=entity_id,
            )
            record = await result.single()
            if record is None:
                return None
            return _props_to_entity(dict(record["e"]))

    async def get_entity_by_external_id(
        self, key: str, value: str
    ) -> Optional[BaseEntity]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.external_ids CONTAINS $kv RETURN e",
                kv=json.dumps({key: value}),
            )
            record = await result.single()
            if record is None:
                return None
            return _props_to_entity(dict(record["e"]))

    async def find_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        min_confidence: float = 0.30,
        limit: int = 20,
    ) -> List[BaseEntity]:
        """Full-text search across entity names and aliases."""
        import re
        safe_query = re.sub(r'[^\w\s]', '', query[:200]).strip()
        if not safe_query:
            return []

        async with self._driver.session(database=self._database) as session:
            # Try full-text index first
            try:
                cypher = (
                    "CALL db.index.fulltext.queryNodes('entity_search', $query) "
                    "YIELD node, score "
                    "WHERE node.confidence >= $min_conf "
                )
                params = {"query": safe_query, "min_conf": min_confidence, "limit": limit}
                if entity_type:
                    cypher += "AND node.entity_type = $etype "
                    params["etype"] = entity_type
                cypher += "RETURN node LIMIT $limit"

                result = await session.run(cypher, params)
                records = await result.data()
                return [_props_to_entity(dict(r["node"])) for r in records]
            except Exception:
                # Fallback to CONTAINS search
                cypher = (
                    "MATCH (e:Entity) "
                    "WHERE e.name CONTAINS $query AND e.confidence >= $min_conf "
                )
                params = {"query": safe_query, "min_conf": min_confidence, "limit": limit}
                if entity_type:
                    cypher += "AND e.entity_type = $etype "
                    params["etype"] = entity_type
                cypher += "RETURN e LIMIT $limit"

                result = await session.run(cypher, params)
                records = await result.data()
                return [_props_to_entity(dict(r["e"])) for r in records]

    # ── Entity writes ─────────────────────────────────────────────────────

    async def upsert_entity(self, entity: BaseEntity) -> BaseEntity:
        props = _entity_to_props(entity)
        now = datetime.now(timezone.utc).isoformat()
        if "created_at" not in props:
            props["created_at"] = now
        props["updated_at"] = now

        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MERGE (e:Entity {id: $id}) "
                "SET e += $props "
                "RETURN e",
                id=entity.id,
                props=props,
            )
            record = await result.single()
            return _props_to_entity(dict(record["e"]))

    async def update_entity_property(
        self,
        entity_id: str,
        property_key: str,
        property_value: Any,
        confidence: float,
    ) -> None:
        async with self._driver.session(database=self._database) as session:
            # Only update if new confidence is higher
            await session.run(
                "MATCH (e:Entity {id: $id}) "
                "WHERE e.confidence <= $conf "
                "SET e.properties = CASE WHEN e.properties IS NULL THEN $prop "
                "     ELSE apoc.coll.setProperty(e.properties, $key, $value) END, "
                "    e.confidence = $conf, e.updated_at = $now",
                id=entity_id,
                key=property_key,
                value=json.dumps(property_value) if isinstance(property_value, (dict, list)) else property_value,
                prop=json.dumps({property_key: property_value}),
                conf=confidence,
                now=datetime.now(timezone.utc).isoformat(),
            ).consume()

    async def add_entity_alias(self, entity_id: str, alias: str) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run(
                "MATCH (e:Entity {id: $id}) "
                "SET e.aliases = CASE WHEN e.aliases IS NULL THEN [$alias] "
                "     WHEN NOT $alias IN e.aliases THEN e.aliases + [$alias] "
                "     ELSE e.aliases END, "
                "    e.updated_at = $now",
                id=entity_id,
                alias=alias,
                now=datetime.now(timezone.utc).isoformat(),
            ).consume()

    async def delete_entity(self, entity_id: str) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run(
                "MATCH (e:Entity {id: $id}) DETACH DELETE e",
                id=entity_id,
            ).consume()

    # ── Relationship reads ────────────────────────────────────────────────

    async def get_relationship(self, rel_id: str) -> Optional[WorldRelationship]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id RETURN r",
                id=rel_id,
            )
            record = await result.single()
            if record is None:
                return None
            return _props_to_rel(dict(record["r"]))

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
        clauses = []
        params: Dict[str, Any] = {"min_conf": min_confidence, "limit": limit}

        if source_id:
            clauses.append("(s:Entity {id: $src})-[r]->(t:Entity)")
            params["src"] = source_id
        elif target_id:
            clauses.append("(s:Entity)-[r]->(t:Entity {id: $tgt})")
            params["tgt"] = target_id
        else:
            clauses.append("(s:Entity)-[r]->(t:Entity)")

        where_parts = ["r.confidence >= $min_conf"]
        if relationship_type:
            # Need to use specific relationship type in MATCH
            pass  # Will handle via dynamic Cypher below
        if active_only:
            where_parts.append("r.valid_to IS NULL")
        if target_types:
            where_parts.append("t.entity_type IN $tgt_types")
            params["tgt_types"] = target_types

        match_clause = clauses[0] if not relationship_type else \
            clauses[0].replace("-[r]->", f"-[r:{relationship_type}]->")

        cypher = f"MATCH {match_clause} WHERE {' AND '.join(where_parts)} RETURN r LIMIT $limit"

        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, params)
            records = await result.data()
            return [_props_to_rel(dict(r["r"])) for r in records]

    async def query_at_time(
        self,
        entity_id: str,
        as_of: str,
        relationship_types: Optional[List[str]] = None,
    ) -> List[WorldRelationship]:
        clauses = [
            "r.confidence >= 0.30",
            "(r.valid_from IS NULL OR r.valid_from <= $as_of)",
            "(r.valid_to IS NULL OR r.valid_to > $as_of)",
        ]
        params = {"id": entity_id, "as_of": as_of}

        rel_match = "-[r]->" if not relationship_types else \
            "-[r:" + "|".join(relationship_types) + "]->"

        cypher = (
            f"MATCH (s:Entity {{id: $id}}){rel_match}(t:Entity) "
            f"WHERE {' AND '.join(clauses)} RETURN r"
        )

        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, params)
            records = await result.data()
            return [_props_to_rel(dict(r["r"])) for r in records]

    async def get_neighbors(
        self,
        entity_id: str,
        min_confidence: float = 0.30,
        relationship_types: Optional[List[str]] = None,
    ) -> List[Tuple[BaseEntity, WorldRelationship]]:
        """Get neighboring entities and the relationships to them."""
        rel_match = "-[r]->" if not relationship_types else \
            "-[r:" + "|".join(relationship_types) + "]->"

        cypher = (
            f"MATCH (s:Entity {{id: $id}}){rel_match}(t:Entity) "
            "WHERE r.confidence >= $min_conf "
            "RETURN t, r"
        )

        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, id=entity_id, min_conf=min_confidence)
            records = await result.data()
            neighbors = []
            for rec in records:
                entity = _props_to_entity(dict(rec["t"]))
                rel = _props_to_rel(dict(rec["r"]))
                neighbors.append((entity, rel))
            return neighbors

    # ── Relationship writes ───────────────────────────────────────────────

    async def upsert_relationship(self, rel: WorldRelationship) -> WorldRelationship:
        props = _rel_to_props(rel)
        now = datetime.now(timezone.utc).isoformat()
        if "created_at" not in props:
            props["created_at"] = now
        props["updated_at"] = now

        rel_type = rel.relationship_type
        # Sanitize for Cypher (only alphanumeric + underscore)
        import re
        safe_type = re.sub(r'[^A-Z_0-9]', '', rel_type.upper())
        if not safe_type:
            safe_type = "WM_RELATED_TO"

        async with self._driver.session(database=self._database) as session:
            # Check if relationship already exists
            existing = await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id RETURN r",
                id=rel.id,
            )
            existing_record = await existing.single()

            if existing_record:
                # Update existing
                await session.run(
                    "MATCH ()-[r]->() WHERE r.id = $id SET r += $props",
                    id=rel.id,
                    props=props,
                ).consume()
            else:
                # Create new relationship
                await session.run(
                    f"MATCH (s:Entity {{id: $src}}), (t:Entity {{id: $tgt}}) "
                    f"CREATE (s)-[r:{safe_type}]->(t) "
                    "SET r += $props",
                    src=rel.source_id,
                    tgt=rel.target_id,
                    props=props,
                ).consume()

        return rel

    async def close_relationship(self, rel_id: str, valid_to: str) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id "
                "SET r.valid_to = $valid_to, r.updated_at = $now",
                id=rel_id,
                valid_to=valid_to,
                now=datetime.now(timezone.utc).isoformat(),
            ).consume()

    # ── Observations ──────────────────────────────────────────────────────

    async def add_observation(
        self,
        entity_id: Optional[str],
        relationship_id: Optional[str],
        observation: str,
        source: str,
    ) -> str:
        obs_id = _generate_id(OBSERVATION_ID_PREFIX)
        now = datetime.now(timezone.utc).isoformat()

        async with self._driver.session(database=self._database) as session:
            if entity_id:
                await session.run(
                    "MATCH (e:Entity {id: $eid}) "
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  entity_id: $eid, created_at: $now}) "
                    "CREATE (e)-[:HAS_OBSERVATION]->(o)",
                    eid=entity_id,
                    id=obs_id,
                    text=observation,
                    source=source,
                    now=now,
                ).consume()
            elif relationship_id:
                await session.run(
                    "MATCH ()-[r]->() WHERE r.id = $rid "
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  relationship_id: $rid, created_at: $now})",
                    rid=relationship_id,
                    id=obs_id,
                    text=observation,
                    source=source,
                    now=now,
                ).consume()
            else:
                await session.run(
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  created_at: $now})",
                    id=obs_id,
                    text=observation,
                    source=source,
                    now=now,
                ).consume()
        return obs_id

    # ── Merge proposals (simplified for Neo4j) ────────────────────────────

    async def create_merge_proposal(
        self,
        winner_id: str,
        loser_id: str,
        reason: str,
        confidence: float,
    ) -> str:
        proposal_id = _generate_id(MERGE_PROPOSAL_ID_PREFIX)
        now = datetime.now(timezone.utc).isoformat()

        async with self._driver.session(database=self._database) as session:
            await session.run(
                "CREATE (mp:MergeProposal {id: $id, winner_id: $winner, loser_id: $loser, "
                "  reason: $reason, confidence: $conf, status: 'pending', created_at: $now})",
                id=proposal_id,
                winner=winner_id,
                loser=loser_id,
                reason=reason,
                conf=confidence,
                now=now,
            ).consume()
        return proposal_id

    async def get_merge_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (mp:MergeProposal {id: $id}) RETURN mp",
                id=proposal_id,
            )
            record = await result.single()
            if record is None:
                return None
            return dict(record["mp"])

    async def update_merge_proposal_status(
        self, proposal_id: str, status: str
    ) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run(
                "MATCH (mp:MergeProposal {id: $id}) SET mp.status = $status",
                id=proposal_id,
                status=status,
            ).consume()

    async def get_pending_merge_proposals(self) -> List[Dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (mp:MergeProposal {status: 'pending'}) RETURN mp"
            )
            records = await result.data()
            return [dict(r["mp"]) for r in records]

    async def execute_merge(
        self,
        winner_id: str,
        loser_id: str,
        merge_properties: bool = True,
    ) -> None:
        """Merge loser entity into winner. Re-points all relationships."""
        async with self._driver.session(database=self._database) as session:
            if merge_properties:
                # Copy loser's properties to winner where winner lacks them
                await session.run(
                    "MATCH (w:Entity {id: $winner}), (l:Entity {id: $loser}) "
                    "WITH w, l "
                    "SET w.aliases = CASE WHEN w.aliases IS NULL THEN l.aliases "
                    "     ELSE apoc.coll.union(w.aliases, l.aliases) END",
                    winner=winner_id,
                    loser=loser_id,
                ).consume()

            # Re-point incoming relationships
            await session.run(
                "MATCH (other)-[r]->(loser:Entity {id: $loser}) "
                "WITH other, r, loser "
                "MATCH (winner:Entity {id: $winner}) "
                "CREATE (other)-[r2:WM_RELATED_TO]->(winner) "
                "SET r2 += properties(r) "
                "DELETE r",
                loser=loser_id,
                winner=winner_id,
            ).consume()

            # Re-point outgoing relationships
            await session.run(
                "MATCH (loser:Entity {id: $loser})-[r]->(other) "
                "WITH loser, r, other "
                "MATCH (winner:Entity {id: $winner}) "
                "CREATE (winner)-[r2:WM_RELATED_TO]->(other) "
                "SET r2 += properties(r) "
                "DELETE r",
                loser=loser_id,
                winner=winner_id,
            ).consume()

            # Delete loser
            await session.run(
                "MATCH (l:Entity {id: $loser}) DETACH DELETE l",
                loser=loser_id,
            ).consume()

    # ── Stats ─────────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        async with self._driver.session(database=self._database) as session:
            ent_result = await session.run(
                "MATCH (e:Entity) RETURN count(e) as total, "
                "collect(DISTINCT e.entity_type) as types"
            )
            ent_record = await ent_result.single()

            # Count by type
            type_result = await session.run(
                "MATCH (e:Entity) RETURN e.entity_type as type, count(e) as cnt"
            )
            type_records = await type_result.data()
            entities_by_type = {r["type"]: r["cnt"] for r in type_records}

            rel_result = await session.run(
                "MATCH ()-[r]->() RETURN count(r) as total"
            )
            rel_record = await rel_result.single()

            active_rel_result = await session.run(
                "MATCH ()-[r]->() WHERE r.valid_to IS NULL RETURN count(r) as total"
            )
            active_rel_record = await active_rel_result.single()

            obs_result = await session.run(
                "MATCH (o:Observation) RETURN count(o) as total"
            )
            obs_record = await obs_result.single()

            merge_result = await session.run(
                "MATCH (mp:MergeProposal {status: 'pending'}) RETURN count(mp) as total"
            )
            merge_record = await merge_result.single()

            return {
                "total_entities": ent_record["total"] if ent_record else 0,
                "entities_by_type": entities_by_type,
                "total_relationships": rel_record["total"] if rel_record else 0,
                "active_relationships": active_rel_record["total"] if active_rel_record else 0,
                "total_observations": obs_record["total"] if obs_record else 0,
                "merge_proposals_pending": merge_record["total"] if merge_record else 0,
            }
