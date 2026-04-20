"""World Model primary store interface.

Backed by SQLite (default) or PostgreSQL. All callers must use this interface
and never access the backend directly.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from collections import deque

from .config import WorldModelConfig
from .entities import BaseEntity
from .relationships import WorldRelationship
from .sqlite.backend import SQLiteBackend

logger = logging.getLogger(__name__)


@dataclass
class GraphNeighborhoodResult:
    center: BaseEntity
    reachable: List[BaseEntity]
    edges: List[WorldRelationship]
    hop_counts: Dict[str, int]       # entity_id → min hops from center
    truncated: bool = False          # True if max_nodes reached


@dataclass
class WorldModelStats:
    total_entities: int
    entities_by_type: Dict[str, int]
    total_relationships: int
    active_relationships: int
    total_observations: int
    merge_proposals_pending: int


class WorldModelStore:
    """Primary interface to the Colony World Model entity graph.

    Backed by Neo4j when available; falls back to SQLite automatically.
    All methods are async. Callers MUST NOT access the backing store directly.
    """

    def __init__(self, config: Optional[WorldModelConfig] = None) -> None:
        self._config = config or WorldModelConfig()
        self._backend: Optional[SQLiteBackend] = None

    async def connect(self) -> None:
        """Initialize and connect to the storage backend."""
        if self._config.backend == "postgres":
            try:
                from colony_sidecar.world_model.postgres.backend import PostgresBackend
                pg_conn = os.environ.get("WORLD_MODEL_PG_CONNECTION", "")
                if pg_conn:
                    self._backend = PostgresBackend(pg_conn)
                else:
                    logger.warning("WORLD_MODEL_PG_CONNECTION not set — falling back to sqlite")
                    self._backend = SQLiteBackend(self._config.sqlite_path)
            except ImportError:
                logger.warning("asyncpg not installed — falling back to sqlite")
                self._backend = SQLiteBackend(self._config.sqlite_path)
        elif self._config.backend == "sqlite":
            self._backend = SQLiteBackend(self._config.sqlite_path)
        else:
            logger.warning("WorldModel backend '%s' not supported — defaulting to sqlite", self._config.backend)
            self._backend = SQLiteBackend(self._config.sqlite_path)
        await self._backend.connect()

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()

    async def __aenter__(self) -> "WorldModelStore":
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    # ── Entity reads ──────────────────────────────────────────────────────────

    async def get_entity(
        self,
        entity_id: str,
        min_confidence: float = 0.30,
    ) -> Optional[BaseEntity]:
        """Fetch a single entity by ID.

        Returns None if the entity does not exist or falls below min_confidence.
        """
        entity = await self._backend.get_entity(entity_id)
        if entity and entity.confidence >= min_confidence:
            return entity
        return None

    async def find_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        min_confidence: float = 0.30,
        limit: int = 20,
    ) -> List[BaseEntity]:
        """Full-text search across entity names and aliases."""
        return await self._backend.find_entities(
            query=query,
            entity_type=entity_type,
            min_confidence=min_confidence,
            limit=limit,
        )

    async def get_entity_by_external_id(
        self,
        key: str,
        value: str,
    ) -> Optional[BaseEntity]:
        """Look up an entity by external ID key/value pair."""
        return await self._backend.get_entity_by_external_id(key, value)

    # ── Graph traversal ───────────────────────────────────────────────────────

    async def get_neighborhood(
        self,
        entity_id: str,
        max_hops: int = 2,
        relationship_types: Optional[List[str]] = None,
        min_confidence: float = 0.30,
        max_nodes: int = 200,
    ) -> GraphNeighborhoodResult:
        """BFS neighborhood traversal up to max_hops from entity_id.

        MUST NOT exceed max_hops = 5 regardless of caller input.
        """
        max_hops = min(max_hops, self._config.max_graph_hops)
        center = await self._backend.get_entity(entity_id)
        if not center:
            return GraphNeighborhoodResult(
                center=None, reachable=[], edges=[], hop_counts={}, truncated=False
            )

        visited: Dict[str, int] = {entity_id: 0}
        frontier = deque([entity_id])
        reachable: List[BaseEntity] = []
        edges: List[WorldRelationship] = []
        truncated = False

        while frontier:
            current_id = frontier.popleft()
            current_hop = visited[current_id]
            if current_hop >= max_hops:
                continue

            neighbors = await self._backend.get_neighbors(
                current_id, min_confidence=min_confidence,
                relationship_types=relationship_types,
            )
            for neighbor, rel in neighbors:
                edges.append(rel)
                if neighbor.id not in visited:
                    hop = current_hop + 1
                    visited[neighbor.id] = hop
                    reachable.append(neighbor)
                    if len(reachable) >= max_nodes:
                        truncated = True
                        break
                    frontier.append(neighbor.id)
            if truncated:
                break

        hop_counts = {eid: h for eid, h in visited.items() if eid != entity_id}
        return GraphNeighborhoodResult(
            center=center,
            reachable=reachable,
            edges=edges,
            hop_counts=hop_counts,
            truncated=truncated,
        )

    async def find_path(
        self,
        source_id: str,
        target_id: str,
        max_hops: int = 5,
        min_confidence: float = 0.30,
    ) -> Optional[List[WorldRelationship]]:
        """Find shortest path between two entities via BFS.

        Returns ordered list of relationships, or None if no path exists.
        """
        max_hops = min(max_hops, self._config.max_graph_hops)
        if source_id == target_id:
            return []

        # BFS with path tracking
        visited: Dict[str, Optional[tuple]] = {source_id: None}  # id → (prev_id, rel)
        frontier = deque([(source_id, 0)])

        while frontier:
            current_id, hops = frontier.popleft()
            if hops >= max_hops:
                continue
            neighbors = await self._backend.get_neighbors(
                current_id, min_confidence=min_confidence
            )
            for neighbor, rel in neighbors:
                if neighbor.id not in visited:
                    visited[neighbor.id] = (current_id, rel)
                    if neighbor.id == target_id:
                        # Reconstruct path
                        path = []
                        node = target_id
                        while visited[node] is not None:
                            prev_id, edge = visited[node]
                            path.append(edge)
                            node = prev_id
                        path.reverse()
                        return path
                    frontier.append((neighbor.id, hops + 1))
        return None

    async def find_common_neighbors(
        self,
        entity_id_a: str,
        entity_id_b: str,
        max_hops: int = 1,
    ) -> List[BaseEntity]:
        """Return entities reachable from both A and B within max_hops."""
        result_a = await self.get_neighborhood(entity_id_a, max_hops=max_hops)
        result_b = await self.get_neighborhood(entity_id_b, max_hops=max_hops)
        ids_a = {e.id for e in result_a.reachable}
        ids_b = {e.id for e in result_b.reachable}
        shared_ids = ids_a & ids_b
        return [e for e in result_a.reachable if e.id in shared_ids]

    # ── Relationship reads ────────────────────────────────────────────────────

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
        """Query relationships with flexible filtering."""
        return await self._backend.query_relationships(
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type,
            target_types=target_types,
            active_only=active_only,
            min_confidence=min_confidence,
            limit=limit,
        )

    async def query_at_time(
        self,
        entity_id: str,
        as_of: str,
        relationship_types: Optional[List[str]] = None,
    ) -> List[WorldRelationship]:
        """Return relationships active at a specific point in time."""
        return await self._backend.query_at_time(
            entity_id=entity_id, as_of=as_of, relationship_types=relationship_types
        )

    # ── Entity writes ─────────────────────────────────────────────────────────

    async def upsert_entity(self, entity: BaseEntity) -> BaseEntity:
        """Insert or update an entity. Returns the surviving entity."""
        return await self._backend.upsert_entity(entity)

    async def update_entity_property(
        self,
        entity_id: str,
        property_key: str,
        property_value: Any,
        confidence: float,
    ) -> None:
        """Update a single property if the new confidence is higher."""
        await self._backend.update_entity_property(
            entity_id, property_key, property_value, confidence
        )

    async def add_entity_alias(self, entity_id: str, alias: str) -> None:
        """Add an alias to an entity's alias list if not already present."""
        await self._backend.add_entity_alias(entity_id, alias)

    # ── Relationship writes ───────────────────────────────────────────────────

    async def upsert_relationship(
        self, relationship: WorldRelationship
    ) -> WorldRelationship:
        """Insert or update a relationship."""
        return await self._backend.upsert_relationship(relationship)

    async def close_relationship(
        self, relationship_id: str, valid_to: str
    ) -> None:
        """Mark a relationship as ended by setting valid_to."""
        await self._backend.close_relationship(relationship_id, valid_to)

    # ── Observations ─────────────────────────────────────────────────────────

    async def add_observation(
        self,
        entity_id: Optional[str],
        relationship_id: Optional[str],
        observation: str,
        source: str,
    ) -> str:
        """Record a raw observation string for an entity or relationship."""
        return await self._backend.add_observation(
            entity_id, relationship_id, observation, source
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> WorldModelStats:
        """Return summary statistics for the world model graph."""
        raw = await self._backend.get_stats()
        return WorldModelStats(
            total_entities=raw["total_entities"],
            entities_by_type=raw["entities_by_type"],
            total_relationships=raw["total_relationships"],
            active_relationships=raw["active_relationships"],
            total_observations=raw["total_observations"],
            merge_proposals_pending=raw["merge_proposals_pending"],
        )
