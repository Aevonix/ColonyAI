"""Colony Graph Memory System — Neo4j async client.

Replaces Hermes MEMORY.md with a persistent graph database that models
relationships, events, and behavioral patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver
except ImportError:
    pass
from pydantic import SecretStr

if TYPE_CHECKING:
    from colony_sidecar.vector.store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class GraphConfig:
    """Connection settings for the Neo4j graph database."""

    uri: str = "bolt://localhost:7687"
    database: str = "colony"
    auth: Optional[tuple[str, SecretStr]] = None  # (user, password) — password masked in logs
    max_pool_size: int = 50
    connection_timeout_secs: float = 10.0
    max_retry_secs: float = 30.0


class ColonyGraph:
    """Neo4j graph memory system replacing Hermes MEMORY.md.

    Provides:
    - Memory storage with automatic entity linking
    - Semantic recall via vector index + strength decay
    - Ebbinghaus forgetting‐curve decay
    - Pruning of weak / stale memories
    - Multi-hop traversal across memory connections
    """

    def __init__(self, config: GraphConfig) -> None:
        self._config = config
        driver_auth = (
            (config.auth[0], config.auth[1].get_secret_value())
            if config.auth is not None
            else None
        )
        self.driver: AsyncDriver = AsyncGraphDatabase.driver(
            config.uri,
            auth=driver_auth,
            max_connection_pool_size=config.max_pool_size,
            connection_timeout=config.connection_timeout_secs,
            max_transaction_retry_time=config.max_retry_secs,
            keep_alive=True,
        )
        self.database: str = config.database
        self._embed_fn: Optional[
            Callable[[str], Coroutine[Any, Any, List[float]]]
        ] = None
        self._vector_store: Optional["VectorStore"] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Verify connectivity to Neo4j."""
        await self.driver.verify_connectivity()

    async def close(self) -> None:
        """Cleanly shut down the driver."""
        await self.driver.close()

    # ------------------------------------------------------------------
    # Embedding helper
    # ------------------------------------------------------------------

    def set_embed_fn(
        self,
        fn: Callable[[str], Coroutine[Any, Any, List[float]]],
    ) -> None:
        """Register an async embedding function used by *recall*.

        Args:
            fn: async callable that maps a string to a float vector.
        """
        self._embed_fn = fn

    def set_vector_store(self, store: "VectorStore") -> None:
        """Register a VectorStore for ANN search (replaces Neo4j vector index)."""
        self._vector_store = store

    async def _embed(self, text: str) -> List[float]:
        """Produce an embedding vector for *text*.

        Raises:
            RuntimeError: If no embedding function has been registered.
        """
        if self._embed_fn is None:
            raise RuntimeError(
                "No embedding function registered. Call set_embed_fn() first."
            )
        return await self._embed_fn(text)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def store_memory(
        self,
        content: str,
        memory_type: str,
        entities: List[str],
        metadata: Dict[str, Any] | None = None,
        importance: float = 1.0,
        person_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Store a memory with automatic entity linking.

        Creates a :Memory node and :MENTIONS edges to each :Entity.
        If an embedding function is registered the memory's vector is stored
        on the node so it participates in semantic search.

        Args:
            content: Memory content text
            memory_type: Type of memory (episodic, semantic, procedural, identity)
            entities: Named entities to link to this memory
            metadata: Optional key-value metadata
            importance: Initial importance / strength (0-1, default 1.0)
            person_id: Optional person ID to link this memory to via (Memory)-[:ABOUT]->(Person)

        Returns:
            The UUID of the newly created Memory node.
        """
        metadata = metadata or {}
        importance = max(0.0, min(1.0, importance))
        # Resolve person_id from explicit arg or metadata fallback
        person_id = person_id or metadata.get("person_id")
        # Resolve session_id from explicit arg or metadata fallback
        session_id = session_id or (metadata.get("session_id") if metadata else None)

        # Compute embedding if available
        embedding: Optional[List[float]] = None
        if self._embed_fn is not None:
            embedding = await self._embed(content)

        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                CREATE (m:Memory {
                    id: randomUUID(),
                    content: $content,
                    type: $memory_type,
                    importance: $importance,
                    strength: $importance,
                    recalls: 0,
                    created_at: datetime(),
                    accessed_at: datetime(),
                    embedding: $embedding,
                    metadata: $metadata,
                    session_id: $session_id
                })
                WITH m
                FOREACH (entity_name IN $entities |
                    MERGE (e:Entity {name: entity_name})
                    CREATE (m)-[:MENTIONS]->(e)
                )
                WITH m
                FOREACH (_ IN CASE WHEN $person_id IS NOT NULL THEN [1] ELSE [] END |
                    MERGE (p:Person {id: $person_id})
                    CREATE (m)-[:ABOUT]->(p)
                )
                RETURN m.id AS id
                """,
                content=content,
                memory_type=memory_type,
                importance=importance,
                entities=entities,
                embedding=embedding,
                metadata=str(metadata),
                person_id=person_id,
                session_id=session_id,
            )
            record = await result.single()
            if record is None:
                raise RuntimeError("Failed to create memory node")
            memory_id = record["id"]

        # Write to LanceDB vector store (if configured)
        if self._vector_store is not None and embedding is not None:
            try:
                from colony_sidecar.vector.collections import Collection
                await self._vector_store.add(
                    collection=Collection.MEMORIES,
                    id=memory_id,
                    text=content,
                    vector=embedding,
                    metadata={
                        "memory_id": memory_id,
                        "type": memory_type,
                        "strength": importance,
                        "importance": importance,
                        "person_id": metadata.get("person_id"),
                        "tags": metadata.get("tags", []),
                        "created_at": metadata.get("created_at"),
                        "session_id": session_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to write memory to vector store: %s", exc)

        return memory_id

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]:
        """Retrieve memories by semantic similarity with strength decay.

        Uses LanceDB for ANN search, then hydrates full Memory nodes from
        Neo4j with entity mentions.  Falls back to graph-only keyword
        recall if no vector store or embedding function is configured.

        Returns:
            A list of memory dicts, each annotated with ``entities`` and
            sorted by relevance descending.
        """
        # Vector search path: embed query → LanceDB ANN → Neo4j hydration
        if self._vector_store is not None and self._embed_fn is not None:
            try:
                embedding = await self._embed(query)
                from colony_sidecar.vector.collections import Collection
                results = await self._vector_store.search(
                    collection=Collection.MEMORIES,
                    query_vector=embedding,
                    limit=limit,
                    # metadata is stored as a JSON string (pa.utf8()); LanceDB's
                    # filter dialect does not support json_extract on utf8 columns.
                    # Strength filtering is applied post-hydration from Neo4j below.
                    filter=None,
                )
                if results:
                    memory_ids = [r.id for r in results]
                    score_map = {r.id: r.score for r in results}

                    # Hydrate from Neo4j
                    async with self.driver.session(database=self.database) as session:
                        result = await session.run(
                            """
                            MATCH (m:Memory) WHERE m.id IN $ids
                            OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
                            WITH m, collect(e.name) AS entity_names
                            RETURN m {.*, entities: entity_names} AS memory
                            """,
                            ids=memory_ids,
                        )
                        memories = []
                        async for record in result:
                            mem = record["memory"]
                            mid = mem.get("id", "")
                            vector_score = score_map.get(mid, 0.0)
                            strength = float(mem.get("strength", 1.0))
                            if strength < min_strength:
                                continue
                            mem["relevance"] = vector_score * strength
                            memories.append(mem)

                    memories.sort(key=lambda m: m.get("relevance", 0), reverse=True)
                    # Fire-and-forget touch_memory for each recalled result
                    for mem in memories:
                        mid = mem.get("id")
                        if mid:
                            asyncio.create_task(self._touch_memory_safe(mid))
                    return memories
            except Exception as exc:
                logger.warning("Vector recall failed, falling back to graph-only: %s", exc)

        # Fallback: graph-only keyword/entity recall
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory)
                WHERE m.strength >= $min_strength
                  AND toLower(m.content) CONTAINS toLower($search_text)
                OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
                WITH m, collect(e.name) AS entity_names
                RETURN m {.*, entities: entity_names} AS memory,
                       m.strength AS relevance
                ORDER BY relevance DESC
                LIMIT $limit
                """,
                search_text=query,
                limit=limit,
                min_strength=min_strength,
            )
            memories = []
            async for record in result:
                mem = record["memory"]
                mem["relevance"] = record.get("relevance", mem.get("strength", 0.5))
                memories.append(mem)
        # Fire-and-forget touch_memory for each recalled result
        for mem in memories:
            mid = mem.get("id")
            if mid:
                asyncio.create_task(self._touch_memory_safe(mid))
        return memories

    async def _touch_memory_safe(self, memory_id: str) -> None:
        """Touch a memory, logging but not raising on failure."""
        try:
            await self.touch_memory(memory_id)
        except Exception as exc:
            logger.debug("touch_memory failed for %s: %s", memory_id, exc)

    # ------------------------------------------------------------------
    # Decay & pruning
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_decay_factor(
        importance: float,
        days_elapsed: float,
        recalls: int,
        half_life_days: float,
        memory_type: str = "episodic",
    ) -> float:
        """Compute Ebbinghaus decay for a memory.

        Formula: strength = importance * e^(-lambda * days) * (1 + recalls * 0.2)

        Where lambda = ln(2) / half_life_days.  Identity memories never decay;
        procedural memories decay at half the normal rate.  Result is capped at 1.0.

        Args:
            importance: Initial importance value (0-1)
            days_elapsed: Days since last access
            recalls: Number of times the memory has been recalled
            half_life_days: Days for strength to halve (default 7)
            memory_type: One of "identity", "procedural", "episodic", "semantic"

        Returns:
            New strength value in [0, 1].
        """
        if memory_type == "identity":
            return float(importance)

        lambda_base = math.log(2) / max(half_life_days, 0.001)
        # Procedural memories decay at half the normal rate
        lambda_val = lambda_base / 2 if memory_type == "procedural" else lambda_base

        strength = importance * math.exp(-lambda_val * max(days_elapsed, 0)) * (1.0 + recalls * 0.2)
        return min(1.0, max(0.0, strength))

    async def decay_memories(self, half_life_days: float = 7.0) -> None:
        """Apply Ebbinghaus forgetting curve to all non-identity memories.

        Formula: strength = importance * e^(-lambda * days) * (1 + recalls * 0.2)

        Where lambda = ln(2) / half_life_days.
        - Identity memories are skipped (never decay).
        - Procedural memories use lambda / 2 (half rate).
        - Result is capped at 1.0.

        Args:
            half_life_days: Number of days for strength to halve (default 7).
        """
        lambda_normal = math.log(2) / max(half_life_days, 0.001)
        lambda_procedural = lambda_normal / 2

        async with self.driver.session(database=self.database) as session:
            await session.run(
                """
                MATCH (m:Memory)
                WHERE m.type <> 'identity'
                WITH m,
                     toFloat(duration.between(m.accessed_at, datetime()).days) AS days_since,
                     CASE WHEN m.type = 'procedural'
                          THEN $lambda_proc
                          ELSE $lambda_norm
                     END AS lam
                WITH m,
                     coalesce(m.importance, 1.0) *
                         exp(-lam * days_since) *
                         (1.0 + coalesce(m.recalls, 0) * 0.2) AS new_strength
                SET m.strength = CASE
                    WHEN new_strength > 1.0 THEN 1.0
                    WHEN new_strength < 0.0 THEN 0.0
                    ELSE new_strength
                END
                """,
                lambda_norm=lambda_normal,
                lambda_proc=lambda_procedural,
            )

    async def prune_weak_memories(
        self,
        threshold: float = 0.05,
    ) -> int:
        """Delete memories whose strength has decayed below *threshold*.

        Returns:
            The number of pruned Memory nodes.
        """
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory)
                WHERE m.strength < $threshold
                DETACH DELETE m
                RETURN count(m) AS pruned
                """,
                threshold=threshold,
            )
            record = await result.single()
            return record["pruned"] if record else 0

    async def touch_memory(self, memory_id: str) -> None:
        """Record a memory recall, incrementing its recall counter and updating accessed_at.

        Calling this after retrieval ensures the recall bonus in the Ebbinghaus
        formula is applied on the next decay pass, slowing future decay.

        Args:
            memory_id: UUID of the Memory node to touch.
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                SET m.recalls = coalesce(m.recalls, 0) + 1,
                    m.accessed_at = datetime()
                """,
                memory_id=memory_id,
            )

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    # Allowlist of permitted Cypher templates.  Each entry is an exact string
    # match against the cypher argument.  This prevents run_query from being
    # used as a Cypher injection sink while retaining the escape hatch for
    # known safe ad-hoc queries added here explicitly.
    _ALLOWED_CYPHER: frozenset = frozenset({
        # StrategyAdjuster._adjust_threshold — persist similarity threshold
        """MERGE (c:Config {key: "similarity_threshold"})
                   SET c.value = $threshold, c.updated_at = datetime()
                   RETURN c.value AS new_value""",
        # StrategyAdjuster._recalibrate_baselines — recalculate baseline signals
        """MATCH (p:Person)-[:EXHIBITED]->(s:Signal)
                   WHERE s.timestamp >= datetime() - duration({days: 30})
                   WITH p.id AS pid, s.signal_type AS stype, avg(s.normalized_value) AS baseline_val
                   MERGE (b:Baseline {person_id: pid, signal_type: stype})
                   SET b.value = baseline_val, b.updated_at = datetime()
                   RETURN count(b) AS updated_baselines""",
        # Neo4jCognitionSeeder.seed — upsert BootstrapEvent node on first boot
        """
            MERGE (b:BootstrapEvent {colony_id: $colony_id})
            SET b.colony_name      = $colony_name,
                b.colony_version   = $colony_version,
                b.network_id       = $network_id,
                b.corpus_version   = $corpus_version,
                b.bootstrapped_at  = $bootstrapped_at,
                b.layer_count      = $layer_count,
                b.endpoint_count   = $endpoint_count
            RETURN b.colony_id
            """,

        # ConnectionDiscoverer._find_temporal_patterns (with person_id)
        "MATCH (m1:Memory)-[:BELONGS_TO]->(p:Person {id: $person_id})\n"
        "MATCH (m2:Memory)-[:BELONGS_TO]->(p)\n"
        "WHERE m1.id < m2.id\n"
        "  AND abs(duration.between(m1.created_at, m2.created_at).hours) <= $window_hours\n"
        "  AND m1.created_at >= datetime() - duration({days: $lookback_days})\n"
        "WITH m1, m2, count(*) AS co_occurrences\n"
        "WHERE co_occurrences >= $min_count\n"
        "RETURN m1.id AS source_id, m2.id AS target_id,\n"
        "       m1.type AS source_type, m2.type AS target_type,\n"
        "       m1.metadata AS source_meta, m2.metadata AS target_meta,\n"
        "       co_occurrences,\n"
        "       toFloat(co_occurrences) / $lookback_days AS daily_rate\n"
        "ORDER BY daily_rate DESC\n"
        "LIMIT 20",
        # ConnectionDiscoverer._find_temporal_patterns (without person_id)
        "MATCH (m1:Memory), (m2:Memory)\n"
        "WHERE m1.id < m2.id\n"
        "  AND abs(duration.between(m1.created_at, m2.created_at).hours) <= $window_hours\n"
        "  AND m1.created_at >= datetime() - duration({days: $lookback_days})\n"
        "WITH m1, m2, count(*) AS co_occurrences\n"
        "WHERE co_occurrences >= $min_count\n"
        "RETURN m1.id AS source_id, m2.id AS target_id,\n"
        "       m1.type AS source_type, m2.type AS target_type,\n"
        "       m1.metadata AS source_meta, m2.metadata AS target_meta,\n"
        "       co_occurrences,\n"
        "       toFloat(co_occurrences) / $lookback_days AS daily_rate\n"
        "ORDER BY daily_rate DESC\n"
        "LIMIT 20",
# ConnectionDiscoverer._find_entity_patterns
        "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory)\n"
        "WHERE ($person_id IS NULL OR (m)-[:BELONGS_TO]->(:Person {id: $person_id}))\n"
        "WITH e, collect(DISTINCT m.id) AS mems\n"
        "WITH e, mems, size(mems) AS mem_count\n"
        "WHERE mem_count >= 2\n"
        "RETURN e.name AS entity_name,\n"
        "       mem_count AS occurrence_count,\n"
        "       mems[0..5] AS evidence_sample\n"
        "ORDER BY mem_count DESC\n"
        "LIMIT 20",
                # ConnectionDiscoverer._find_behavioral_patterns
        "MATCH (p:Person {id: $person_id})-[:EXHIBITED]->(s1:Signal)\n"
        "MATCH (p)-[:EXHIBITED]->(s2:Signal)\n"
        "WHERE s1.signal_type <> s2.signal_type\n"
        "  AND s2.timestamp > s1.timestamp\n"
        "  AND duration.between(s1.timestamp, s2.timestamp).hours <= $window_hours\n"
        "WITH s1.signal_type AS type_a, s2.signal_type AS type_b,\n"
        "     count(*) AS occurrences,\n"
        "     avg(s2.normalized_value - s1.normalized_value) AS avg_delta,\n"
        "     collect(s1.id)[0..5] AS evidence\n"
        "WHERE occurrences >= $min_occurrences\n"
        "RETURN type_a, type_b, occurrences, avg_delta, evidence\n"
        "ORDER BY occurrences DESC\n"
        "LIMIT 15",
        # Consolidator._detect_conflicts
        "MATCH (m1:Memory)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m2:Memory) "
        "WHERE id(m1) < id(m2) "
        "AND NOT (m1)-[:MERGED_INTO]-() AND NOT (m2)-[:MERGED_INTO]-() "
        "RETURN m1.id AS id_a, m2.id AS id_b, e.name AS entity, "
        "       m1.content AS content_a, m2.content AS content_b",
    })

    async def run_query(self, cypher: str, params: dict) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results as dicts.

        WARNING: The ``cypher`` argument must never be constructed from
        user-controlled input.  Only queries listed in ``_ALLOWED_CYPHER``
        are permitted; all others raise ``ValueError``.  To add a new query,
        add its exact string to ``_ALLOWED_CYPHER`` after security review.

        Args:
            cypher: Exact Cypher query string (must be in _ALLOWED_CYPHER).
            params: Dict of $param bindings — always parameterized, never interpolated.

        Returns:
            List of result records as plain dicts.

        Raises:
            ValueError: If ``cypher`` is not in the allowlist.
        """
        if cypher not in self._ALLOWED_CYPHER:
            raise ValueError(
                "run_query: cypher string not in allowlist. "
                "Add to GraphClient._ALLOWED_CYPHER after security review."
            )
        async with self.driver.session(database=self.database) as session:
            result = await session.run(cypher, **params)
            return [dict(r) async for r in result]

    # GRAPH-01: server-enforced maximum traversal depth
    MAX_GRAPH_DEPTH = 10

    async def traverse_memory_connections(
        self,
        memory_id: str,
        max_depth: int = 3,
        min_strength: float = 0.3,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Walk multi-hop causal / supporting chains from a memory.

        Uses ``CAUSED_BY``, ``LED_TO``, and ``SUPPORTS`` edge types up to
        *max_depth* hops, filtering out nodes below *min_strength*.

        Returns:
            A list of dicts with ``memory``, ``distance``, and
            ``path_weight`` keys.
        """
        # GRAPH-01: clamp depth to server-enforced maximum
        max_depth = min(int(max_depth), self.MAX_GRAPH_DEPTH)
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH path = (m1:Memory)-[:CAUSED_BY|LED_TO|SUPPORTS*1..$max_depth]->(m2:Memory)
                WHERE m1.id = $memory_id
                AND all(node IN nodes(path) WHERE node.strength >= $min_strength)
                RETURN m2 {.*} AS memory,
                       length(path) AS distance,
                       reduce(w = 1.0, r IN relationships(path) | w * r.weight) AS path_weight
                ORDER BY path_weight DESC
                LIMIT $limit
                """,
                memory_id=memory_id,
                max_depth=max_depth,
                min_strength=min_strength,
                limit=limit,
            )
            return [
                {
                    "memory": record["memory"],
                    "distance": record["distance"],
                    "path_weight": record["path_weight"],
                }
                async for record in result
            ]


    # ------------------------------------------------------------------
    # Baseline methods (GraphBaselineStore)
    # ------------------------------------------------------------------

    async def _run_get_baseline(self, person_id: str) -> dict | None:
        """Read baseline properties from a Person node. Returns None if not found."""
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(GET_BASELINE, person_id=person_id)
                record = await result.single()
                if record is None:
                    return None
                return dict(record)
        except Exception as exc:
            logger.debug("_run_get_baseline failed for %s: %s", person_id, exc)
            return None

    async def _run_update_baseline(
        self,
        person_id: str,
        msg_count: int,
        length_mean: float,
        length_m2: float,
        length_std: float,
        hour_histogram: str,
    ) -> None:
        """Write updated baseline properties to a Person node."""
        try:
            async with self.driver.session(database=self.database) as session:
                await session.run(
                    UPDATE_BASELINE,
                    person_id=person_id,
                    msg_count=msg_count,
                    length_mean=length_mean,
                    length_m2=length_m2,
                    length_std=length_std,
                    hour_histogram=hour_histogram,
                )
        except Exception as exc:
            logger.debug("_run_update_baseline failed for %s: %s", person_id, exc)

    async def list_person_ids(self) -> List[str]:
        """Return all person IDs that have a Person node in the graph."""
        query = "MATCH (p:Person) RETURN p.id AS id"
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query)
            records = await result.values()
            return [r[0] for r in records if r[0]]

    # ------------------------------------------------------------------
    # Signal & relationship methods (required by SignalCollector,
    # BaselineStore, and RelationshipScorer)
    # ------------------------------------------------------------------

    async def store_signal(self, signal: Any) -> str:
        """Persist a behavioral signal to the graph.

        Creates a :Signal node linked to the :Person via [:EXHIBITED].
        """
        from colony_sidecar.intelligence.graph.queries import STORE_SIGNAL, GET_BASELINE, UPDATE_BASELINE

        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    STORE_SIGNAL,
                    person_id=signal.person_id,
                    signal_type=signal.signal_type,
                    raw_value=signal.raw_value,
                    normalized_value=signal.normalized_value,
                    timestamp=signal.timestamp.isoformat(),
                    source=signal.source,
                )
                record = await result.single()
                sid = record["id"] if record else ""
                logger.debug(
                    "store_signal person=%s type=%s %.1fms",
                    signal.person_id, signal.signal_type, (time.monotonic() - t0) * 1000,
                )
                return sid
        except Exception as exc:
            logger.error("store_signal failed for person %s: %s", signal.person_id, exc)
            return ""

    async def get_recent_signals(
        self, person_id: str, hours: int = 24, signal_type: Optional[str] = None
    ) -> List[Any]:
        """Fetch recent signals for a person within a time window.

        Returns a list of Signal dataclass instances from the
        signal_collector module.
        """
        from colony_sidecar.intelligence.mind_model.signal_collector import Signal
        from datetime import timedelta

        cutoff = (self._utcnow() - timedelta(hours=hours)).isoformat()

        cypher = (
            "MATCH (p:Person {id: $person_id})-[:EXHIBITED]->(s:Signal)\n"
            "WHERE s.timestamp >= datetime($cutoff)\n"
        )
        if signal_type:
            cypher += "AND s.signal_type = $signal_type\n"
        cypher += (
            "RETURN s.signal_type AS signal_type,\n"
            "       s.raw_value AS raw_value,\n"
            "       s.normalized_value AS normalized_value,\n"
            "       s.timestamp AS timestamp,\n"
            "       s.source AS source\n"
            "ORDER BY s.timestamp DESC"
        )

        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    cypher,
                    person_id=person_id,
                    cutoff=cutoff,
                    signal_type=signal_type,
                )
                signals = []
                async for record in result:
                    ts = record["timestamp"]
                    if hasattr(ts, "to_native"):
                        ts = ts.to_native()
                    signals.append(Signal(
                        signal_type=record["signal_type"],
                        raw_value=float(record["raw_value"]),
                        normalized_value=float(record["normalized_value"]),
                        timestamp=ts,
                        person_id=person_id,
                        source=record["source"] or "message",
                    ))
                logger.debug(
                    "get_recent_signals person=%s count=%d %.1fms",
                    person_id, len(signals), (time.monotonic() - t0) * 1000,
                )
                return signals
        except Exception as exc:
            logger.error("get_recent_signals failed for person %s: %s", person_id, exc)
            return []

    async def get_all_people(self) -> List[Dict[str, Any]]:
        """Return all Person nodes with their current scores."""
        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (p:Person) "
                    "RETURN p.id AS id, p.name AS name, "
                    "       coalesce(p.score, 50.0) AS score, "
                    "       coalesce(p.tier, 'regular') AS tier, "
                    "       p.lastInteraction AS last_interaction"
                )
                people = [dict(r) async for r in result]
                logger.debug("get_all_people count=%d %.1fms", len(people), (time.monotonic() - t0) * 1000)
                return people
        except Exception as exc:
            logger.error("get_all_people failed: %s", exc)
            return []

    async def record_score_change(
        self,
        person_id: str,
        new_score: float,
        new_tier: str,
        old_score: float,
        reason: str,
    ) -> None:
        """Persist a relationship score change with audit trail."""
        from colony_sidecar.intelligence.graph.queries import RECORD_SCORE_CHANGE

        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                await session.run(
                    RECORD_SCORE_CHANGE,
                    person_id=person_id,
                    new_score=new_score,
                    new_tier=new_tier,
                    delta=new_score - old_score,
                    reason=reason,
                )
            logger.debug(
                "record_score_change person=%s %.1f→%.1f (%s) %.1fms",
                person_id, old_score, new_score, new_tier, (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            logger.error("record_score_change failed for person %s: %s", person_id, exc)

    async def get_person(self, person_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a Person node with all properties."""
        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (p:Person {id: $person_id}) RETURN p {.*} AS person",
                    person_id=person_id,
                )
                record = await result.single()
                logger.debug("get_person %s %.1fms", person_id, (time.monotonic() - t0) * 1000)
                return dict(record["person"]) if record else None
        except Exception as exc:
            logger.error("get_person failed for %s: %s", person_id, exc)
            return None

    async def update_person(self, person_id: str, **props: Any) -> None:
        """Update arbitrary properties on a Person node.

        Only called from trusted internal code (BaselineStore).
        All values are passed as Neo4j parameters — no string interpolation.
        """
        if not props:
            return
        set_clauses = ", ".join(f"p.{k} = ${k}" for k in props)
        cypher = f"MATCH (p:Person {{id: $person_id}}) SET {set_clauses}"
        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                await session.run(cypher, person_id=person_id, **props)
            logger.debug("update_person %s props=%s %.1fms", person_id, list(props), (time.monotonic() - t0) * 1000)
        except Exception as exc:
            logger.error("update_person failed for %s: %s", person_id, exc)

    # ------------------------------------------------------------------

    @staticmethod
    def _utcnow() -> "datetime":
        """Return timezone-aware UTC now (isolated for testability)."""
        from datetime import datetime as _dt, timezone as _tz
        return _dt.now(_tz.utc)
