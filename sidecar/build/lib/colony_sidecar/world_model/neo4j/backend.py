"""Neo4j storage backend for the Colony World Model.

Implements the same interface as SQLiteBackend but backed by Neo4j,
which provides native graph traversal (Cypher) for relationship queries,
neighborhood traversal, and path finding.

Requires: ``neo4j`` driver (pip install neo4j)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
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
    for key in ("aliases", "external_ids", "properties"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except (json.JSONDecodeError, TypeError):
                data[key] = [] if key == "aliases" else {}
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
    import dataclasses
    data = dict(props)
    if "properties" in data and isinstance(data["properties"], str):
        try:
            data["properties"] = json.loads(data["properties"])
        except (json.JSONDecodeError, TypeError):
            data["properties"] = {}
    valid = {f.name for f in dataclasses.fields(WorldRelationship)}
    return WorldRelationship(**{k: v for k, v in data.items() if k in valid})


def _sanitize_rel_type(rel_type: str) -> str:
    """Sanitize a relationship type for use in Cypher (alphanumeric + underscore)."""
    safe = re.sub(r'[^A-Z_0-9]', '', rel_type.upper())
    return safe or "WM_RELATED_TO"


def _node_to_dict(node) -> Dict[str, Any]:
    """Convert a Neo4j Node to a plain dict. Handles both Node objects and pre-converted dicts."""
    if isinstance(node, dict):
        return node
    try:
        return dict(node.items())
    except (TypeError, AttributeError):
        return dict(node)


def _rel_to_dict(rel) -> Dict[str, Any]:
    """Convert a Neo4j Relationship to a plain dict.
    
    When using result.data(), relationships come as tuples:
        (start_node_dict, rel_type_str, end_node_dict)
    When using result.single(), they come as Relationship objects.
    """
    if isinstance(rel, tuple):
        # result.data() format: (start_node_dict, rel_type_str, end_node_dict)
        # This is unreliable — callers should use properties(r) instead
        return {}
    if isinstance(rel, dict):
        return rel
    try:
        return dict(rel.items())
    except (TypeError, AttributeError):
        return dict(rel)


class Neo4jBackend:
    """Neo4j-backed storage for the Colony World Model.

    Node labels: ``Entity`` (all entity types, with ``entity_type`` as property)
    Relationship types: prefixed ``WM_`` types from constants
    """

    def __init__(self, uri: str, database: str = "neo4j",
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
        async with self._driver.session(database=self._database) as session:
            result = await session.run("RETURN 1")
            await result.consume()
        logger.info("Neo4j backend connected: %s (db=%s)", self._uri, self._database)
        await self._apply_schema()

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def _apply_schema(self) -> None:
        """Create indexes and constraints for optimal query performance."""
        async with self._driver.session(database=self._database) as session:
            for cypher in [
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
                "CREATE INDEX entity_type_index IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
                "CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name)",
                "CREATE FULLTEXT INDEX entity_search IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.aliases]",
            ]:
                try:
                    result = await session.run(cypher)
                    await result.consume()
                except Exception:
                    pass  # May already exist

    # ── Entity reads ──────────────────────────────────────────────────────

    async def get_entity(self, entity_id: str) -> Optional[BaseEntity]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) RETURN e", id=entity_id,
            )
            record = await result.single()
            if record is None:
                return None
            return _props_to_entity(_node_to_dict(record["e"]))

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
            return _props_to_entity(_node_to_dict(record["e"]))

    async def find_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        min_confidence: float = 0.30,
        limit: int = 20,
    ) -> List[BaseEntity]:
        safe_query = re.sub(r'[^\w\s]', '', query[:200]).strip()
        if not safe_query:
            return []

        async with self._driver.session(database=self._database) as session:
            try:
                cypher = (
                    "CALL db.index.fulltext.queryNodes('entity_search', $query) "
                    "YIELD node, score "
                    "WHERE node.confidence >= $min_conf "
                )
                params: Dict[str, Any] = {"query": safe_query, "min_conf": min_confidence, "limit": limit}
                if entity_type:
                    cypher += "AND node.entity_type = $etype "
                    params["etype"] = entity_type
                cypher += "RETURN node LIMIT $limit"
                result = await session.run(cypher, params)
                records = await result.data()
                return [_props_to_entity(_node_to_dict(r["node"])) for r in records]
            except Exception:
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
                return [_props_to_entity(_node_to_dict(r["e"])) for r in records]

    # ── Entity writes ─────────────────────────────────────────────────────

    async def upsert_entity(self, entity: BaseEntity) -> BaseEntity:
        props = _entity_to_props(entity)
        now = datetime.now(timezone.utc).isoformat()
        if "created_at" not in props:
            props["created_at"] = now
        props["updated_at"] = now

        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MERGE (e:Entity {id: $id}) SET e += $props RETURN e",
                id=entity.id, props=props,
            )
            record = await result.single()
            return _props_to_entity(_node_to_dict(record["e"]))

    async def update_entity_property(
        self, entity_id: str, property_key: str, property_value: Any, confidence: float,
    ) -> None:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) "
                "SET e.confidence = $conf, e.updated_at = $now",
                id=entity_id, conf=confidence,
                now=datetime.now(timezone.utc).isoformat(),
            )
            await result.consume()

    async def add_entity_alias(self, entity_id: str, alias: str) -> None:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) "
                "SET e.aliases = CASE WHEN e.aliases IS NULL THEN [$alias] "
                "     WHEN NOT $alias IN e.aliases THEN e.aliases + [$alias] "
                "     ELSE e.aliases END, "
                "    e.updated_at = $now",
                id=entity_id, alias=alias,
                now=datetime.now(timezone.utc).isoformat(),
            )
            await result.consume()

    async def delete_entity(self, entity_id: str) -> None:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) DETACH DELETE e", id=entity_id,
            )
            await result.consume()

    # ── Relationship reads ────────────────────────────────────────────────

    async def get_relationship(self, rel_id: str) -> Optional[WorldRelationship]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id RETURN r", id=rel_id,
            )
            record = await result.single()
            if record is None:
                return None
            return _props_to_rel(_rel_to_dict(record["r"]))

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
        if active_only:
            where_parts.append("r.valid_to IS NULL")
        if target_types:
            where_parts.append("t.entity_type IN $tgt_types")
            params["tgt_types"] = target_types

        match_clause = clauses[0] if not relationship_type else \
            clauses[0].replace("-[r]->", f"-[r:{_sanitize_rel_type(relationship_type)}]->")

        cypher = f"MATCH {match_clause} WHERE {' AND '.join(where_parts)} RETURN s.id AS source_id, t.id AS target_id, type(r) AS rel_type, properties(r) AS rel_props LIMIT $limit"

        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, params)
            records = await result.data()
            rels = []
            for r in records:
                props = r["rel_props"] or {}
                props["source_id"] = r["source_id"]
                props["target_id"] = r["target_id"]
                props.setdefault("relationship_type", r["rel_type"])
                rels.append(_props_to_rel(props))
            return rels

    async def query_at_time(
        self, entity_id: str, as_of: str,
        relationship_types: Optional[List[str]] = None,
    ) -> List[WorldRelationship]:
        clauses = [
            "r.confidence >= 0.30",
            "(r.valid_from IS NULL OR r.valid_from <= $as_of)",
            "(r.valid_to IS NULL OR r.valid_to > $as_of)",
        ]
        params = {"id": entity_id, "as_of": as_of}

        rel_match = "-[r]->" if not relationship_types else \
            "-[r:" + "|".join(_sanitize_rel_type(t) for t in relationship_types) + "]->"

        cypher = (
            f"MATCH (s:Entity {{id: $id}}){rel_match}(t:Entity) "
            f"WHERE {' AND '.join(clauses)} RETURN s.id AS source_id, t.id AS target_id, type(r) AS rel_type, properties(r) AS rel_props"
        )

        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, params)
            records = await result.data()
            rels = []
            for r in records:
                props = r["rel_props"] or {}
                props["source_id"] = r["source_id"]
                props["target_id"] = r["target_id"]
                props.setdefault("relationship_type", r["rel_type"])
                rels.append(_props_to_rel(props))
            return rels

    async def get_neighbors(
        self, entity_id: str, min_confidence: float = 0.30,
        relationship_types: Optional[List[str]] = None,
    ) -> List[Tuple[BaseEntity, WorldRelationship]]:
        # Bidirectional: both outgoing and incoming relationships
        if not relationship_types:
            rel_type_filter = ""
        else:
            rel_type_filter = ":" + "|".join(_sanitize_rel_type(t) for t in relationship_types)

        # Outgoing: (center)-[r]->(other)
        cypher_out = (
            f"MATCH (s:Entity {{id: $id}})-[r{rel_type_filter}]->(t:Entity) "
            "WHERE r.confidence >= $min_conf RETURN s.id AS source_id, t.id AS target_id, "
            "type(r) AS rel_type, properties(r) AS rel_props, t"
        )
        # Incoming: (other)-[r]->(center)
        cypher_in = (
            f"MATCH (t:Entity)-[r{rel_type_filter}]->(s:Entity {{id: $id}}) "
            "WHERE r.confidence >= $min_conf RETURN t.id AS source_id, s.id AS target_id, "
            "type(r) AS rel_type, properties(r) AS rel_props, t"
        )

        neighbors = []
        seen_ids = set()
        async with self._driver.session(database=self._database) as session:
            for cypher in [cypher_out, cypher_in]:
                result = await session.run(cypher, id=entity_id, min_conf=min_confidence)
                records = await result.data()
                for rec in records:
                    nid = rec["target_id"] if rec["source_id"] == entity_id else rec["source_id"]
                    if nid in seen_ids or nid == entity_id:
                        continue
                    seen_ids.add(nid)
                    entity = _props_to_entity(_node_to_dict(rec["t"]))
                    props = rec["rel_props"] or {}
                    props["source_id"] = rec["source_id"]
                    props["target_id"] = rec["target_id"]
                    props.setdefault("relationship_type", rec["rel_type"])
                    rel = _props_to_rel(props)
                    neighbors.append((entity, rel))
        return neighbors

    # ── Relationship writes ───────────────────────────────────────────────

    async def upsert_relationship(self, rel: WorldRelationship) -> WorldRelationship:
        props = _rel_to_props(rel)
        now = datetime.now(timezone.utc).isoformat()
        if "created_at" not in props:
            props["created_at"] = now
        props["updated_at"] = now

        safe_type = _sanitize_rel_type(rel.relationship_type)

        async with self._driver.session(database=self._database) as session:
            existing_result = await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id RETURN r", id=rel.id,
            )
            existing_record = await existing_result.single()

            if existing_record:
                result = await session.run(
                    "MATCH ()-[r]->() WHERE r.id = $id SET r += $props RETURN r",
                    id=rel.id, props=props,
                )
                await result.consume()
            else:
                result = await session.run(
                    f"MATCH (s:Entity {{id: $src}}), (t:Entity {{id: $tgt}}) "
                    f"CREATE (s)-[r:{safe_type}]->(t) SET r += $props",
                    src=rel.source_id, tgt=rel.target_id, props=props,
                )
                await result.consume()

        return rel

    async def close_relationship(self, rel_id: str, valid_to: str) -> None:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH ()-[r]->() WHERE r.id = $id "
                "SET r.valid_to = $valid_to, r.updated_at = $now",
                id=rel_id, valid_to=valid_to,
                now=datetime.now(timezone.utc).isoformat(),
            )
            await result.consume()

    # ── Observations ──────────────────────────────────────────────────────

    async def add_observation(
        self, entity_id: Optional[str], relationship_id: Optional[str],
        observation: str, source: str,
    ) -> str:
        obs_id = _generate_id(OBSERVATION_ID_PREFIX)
        now = datetime.now(timezone.utc).isoformat()

        async with self._driver.session(database=self._database) as session:
            if entity_id:
                result = await session.run(
                    "MATCH (e:Entity {id: $eid}) "
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  entity_id: $eid, created_at: $now}) "
                    "CREATE (e)-[:HAS_OBSERVATION]->(o)",
                    eid=entity_id, id=obs_id, text=observation,
                    source=source, now=now,
                )
            elif relationship_id:
                result = await session.run(
                    "MATCH ()-[r]->() WHERE r.id = $rid "
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  relationship_id: $rid, created_at: $now})",
                    rid=relationship_id, id=obs_id, text=observation,
                    source=source, now=now,
                )
            else:
                result = await session.run(
                    "CREATE (o:Observation {id: $id, text: $text, source: $source, "
                    "  created_at: $now})",
                    id=obs_id, text=observation, source=source, now=now,
                )
            await result.consume()
        return obs_id

    # ── Merge proposals ───────────────────────────────────────────────────

    async def create_merge_proposal(
        self, winner_id: str, loser_id: str, reason: str, confidence: float,
    ) -> str:
        proposal_id = _generate_id(MERGE_PROPOSAL_ID_PREFIX)
        now = datetime.now(timezone.utc).isoformat()

        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "CREATE (mp:MergeProposal {id: $id, winner_id: $winner, loser_id: $loser, "
                "  reason: $reason, confidence: $conf, status: 'pending', created_at: $now})",
                id=proposal_id, winner=winner_id, loser=loser_id,
                reason=reason, conf=confidence, now=now,
            )
            await result.consume()
        return proposal_id

    async def get_merge_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (mp:MergeProposal {id: $id}) RETURN mp", id=proposal_id,
            )
            record = await result.single()
            if record is None:
                return None
            return _node_to_dict(record["mp"])

    async def update_merge_proposal_status(self, proposal_id: str, status: str) -> None:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (mp:MergeProposal {id: $id}) SET mp.status = $status",
                id=proposal_id, status=status,
            )
            await result.consume()

    async def get_pending_merge_proposals(self) -> List[Dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (mp:MergeProposal {status: 'pending'}) RETURN mp"
            )
            records = await result.data()
            return [_node_to_dict(r["mp"]) for r in records]

    async def execute_merge(
        self, winner_id: str, loser_id: str, merge_properties: bool = True,
    ) -> None:
        async with self._driver.session(database=self._database) as session:
            if merge_properties:
                result = await session.run(
                    "MATCH (w:Entity {id: $winner}), (l:Entity {id: $loser}) "
                    "WITH w, l "
                    "SET w.aliases = CASE WHEN w.aliases IS NULL THEN l.aliases "
                    "     ELSE apoc.coll.union(w.aliases, l.aliases) END",
                    winner=winner_id, loser=loser_id,
                )
                await result.consume()

            # Re-point incoming relationships
            result = await session.run(
                "MATCH (other)-[r]->(loser:Entity {id: $loser}) "
                "WITH other, r, loser, type(r) AS rtype, properties(r) AS rprops "
                "MATCH (winner:Entity {id: $winner}) "
                "CALL apoc.create.relationship(other, rtype, rprops, winner) YIELD rel "
                "DELETE r",
                loser=loser_id, winner=winner_id,
            )
            await result.consume()

            # Re-point outgoing relationships
            result = await session.run(
                "MATCH (loser:Entity {id: $loser})-[r]->(other) "
                "WITH loser, r, other, type(r) AS rtype, properties(r) AS rprops "
                "MATCH (winner:Entity {id: $winner}) "
                "CALL apoc.create.relationship(winner, rtype, rprops, other) YIELD rel "
                "DELETE r",
                loser=loser_id, winner=winner_id,
            )
            await result.consume()

            # Delete loser
            result = await session.run(
                "MATCH (l:Entity {id: $loser}) DETACH DELETE l", loser=loser_id,
            )
            await result.consume()

    # ── Stats ─────────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        async with self._driver.session(database=self._database) as session:
            type_result = await session.run(
                "MATCH (e:Entity) WHERE e.entity_type IS NOT NULL RETURN e.entity_type AS type, count(e) AS cnt"
            )
            type_records = await type_result.data()
            entities_by_type = {r["type"]: r["cnt"] for r in type_records}

            total_result = await session.run("MATCH (e:Entity) RETURN count(e) AS total")
            total_record = await total_result.single()

            rel_result = await session.run("MATCH ()-[r]->() RETURN count(r) AS total")
            rel_record = await rel_result.single()

            active_result = await session.run(
                "MATCH ()-[r]->() WHERE r.valid_to IS NULL RETURN count(r) AS total"
            )
            active_record = await active_result.single()

            obs_result = await session.run("MATCH (o:Observation) RETURN count(o) AS total")
            obs_record = await obs_result.single()

            merge_result = await session.run(
                "MATCH (mp:MergeProposal {status: 'pending'}) RETURN count(mp) AS total"
            )
            merge_record = await merge_result.single()

            return {
                "total_entities": total_record["total"] if total_record else 0,
                "entities_by_type": entities_by_type,
                "total_relationships": rel_record["total"] if rel_record else 0,
                "active_relationships": active_record["total"] if active_record else 0,
                "total_observations": obs_record["total"] if obs_record else 0,
                "merge_proposals_pending": merge_record["total"] if merge_record else 0,
            }
