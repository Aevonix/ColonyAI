"""Colony Graph Memory System — Neo4j async client.

Replaces Hermes MEMORY.md with a persistent graph database that models
relationships, events, and behavioral patterns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver
except ImportError:
    pass
from pydantic import SecretStr

if TYPE_CHECKING:
    from colony_sidecar.vector.store import VectorStore

logger = logging.getLogger(__name__)


def _recency_factor(days_old: float) -> float:
    """Recency weight for retrieval ranking (v0.21.0).

    Configurable exponential half-life with a floor, so recent memories surface
    over stale ones without fully suppressing older context. Defaults:
    half-life 90d, floor 0.5 (a year-old memory keeps ~0.53 weight; fresh = 1.0).
    Set COLONY_RECENCY_HALF_LIFE_DAYS<=0 to disable. The previous behaviour was a
    near-flat ~10%/year discount that barely affected ranking.
    """
    import os
    try:
        half_life = float(os.environ.get("COLONY_RECENCY_HALF_LIFE_DAYS", "90"))
    except (ValueError, TypeError):
        half_life = 90.0
    if half_life <= 0:
        return 1.0
    try:
        floor = float(os.environ.get("COLONY_RECENCY_FLOOR", "0.5"))
    except (ValueError, TypeError):
        floor = 0.5
    floor = min(max(floor, 0.0), 1.0)
    return floor + (1.0 - floor) * (0.5 ** (max(days_old, 0.0) / half_life))


@dataclass
class GraphConfig:
    """Connection settings for the Neo4j graph database."""

    uri: str = "bolt://localhost:7687"
    database: str = "colony"
    auth: Optional[tuple[str, SecretStr]] = None  # (user, password) — password masked in logs
    max_pool_size: int = 50
    connection_timeout_secs: float = 10.0
    max_retry_secs: float = 30.0


class MemorySourceType(str, Enum):
    CONVERSATION = "conversation"
    FILE = "file"
    TOOL_OUTPUT = "tool_output"
    USER_ASSERTION = "user_assertion"
    INFERENCE = "inference"


class EpistemicState(str, Enum):
    INFERRED = "inferred"
    OBSERVED = "observed"
    CORROBORATED = "corroborated"
    VERIFIED = "verified"
    STALE = "stale"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


SOURCE_RELIABILITY: Dict[str, float] = {
    MemorySourceType.USER_ASSERTION: 1.0,
    MemorySourceType.FILE: 0.9,
    MemorySourceType.TOOL_OUTPUT: 0.85,
    MemorySourceType.CONVERSATION: 0.7,
    MemorySourceType.INFERENCE: 0.5,
}

MAX_IMPORTANCE: Dict[str, float] = {
    MemorySourceType.USER_ASSERTION: 1.0,
    MemorySourceType.FILE: 0.95,
    MemorySourceType.TOOL_OUTPUT: 0.9,
    MemorySourceType.CONVERSATION: 0.8,
    MemorySourceType.INFERENCE: 0.7,
}


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
            # Suppress benign DBMS notifications (unknown-label/property warnings
            # for nodes not yet created) — pure log noise, not errors.
            notifications_min_severity="OFF",
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

    async def ensure_colony_self(self) -> None:
        """Ensure Colony's self-representation exists in the graph (v0.11.0).

        Creates an Agent node for Colony with DEPENDS_ON edges to
        known Subsystem nodes. Idempotent — safe to call multiple times.
        """
        try:
            async with self.driver.session(database=self.database) as session:
                # Create Agent node
                await session.run("""
                    MERGE (a:Agent {id: 'colony-sidecar'})
                    SET a.name = 'Colony',
                        a.version = '0.11.1',
                        a.status = 'active',
                        a.created_at = coalesce(a.created_at, datetime())
                """)

                # Create Subsystem nodes for known components
                subsystems = [
                    ("embed_pipeline", "Embedding Pipeline"),
                    ("delivery_bridge", "Delivery Bridge"),
                    ("event_bus", "Event Bus"),
                    ("graph_client", "Graph Client"),
                    ("initiative_engine", "Initiative Engine"),
                    ("mind_model", "Mind Model"),
                ]
                for sub_id, sub_name in subsystems:
                    await session.run("""
                        MERGE (s:Subsystem {id: $id})
                        SET s.name = $name,
                            s.status = coalesce(s.status, 'active'),
                            s.created_at = coalesce(s.created_at, datetime())
                    """, id=sub_id, name=sub_name)

                    # Create DEPENDS_ON edge from Agent to Subsystem
                    await session.run("""
                        MATCH (a:Agent {id: 'colony-sidecar'}), (s:Subsystem {id: $id})
                        MERGE (a)-[r:DEPENDS_ON]->(s)
                        SET r.created_at = coalesce(r.created_at, datetime())
                    """, id=sub_id)

                logger.info("Colony self-representation verified in graph")
        except Exception as e:
            logger.warning("Failed to ensure Colony self-representation: %s", e)

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
        """Produce an embedding vector for *text* (document side).

        Raises:
            RuntimeError: If no embedding function has been registered.
        """
        if self._embed_fn is None:
            raise RuntimeError(
                "No embedding function registered. Call set_embed_fn() first."
            )
        return await self._embed_fn(text)

    async def _embed_query(self, query: str) -> List[float]:
        """Embed a *query* for retrieval (v0.21.1).

        Instruct-tuned embedders (Qwen3-Embedding, E5, BGE, …) are ASYMMETRIC:
        the query gets an instruction prefix while documents do not. Without it,
        retrieval quality collapses (every result lands at ~0.9 cosine distance
        and the right memory never surfaces). Configurable via
        COLONY_EMBED_QUERY_INSTRUCTION (set to empty for symmetric models).
        """
        import os
        default_instr = ("Instruct: Given a search query, retrieve relevant "
                         "memories that answer it\nQuery: ")
        instr = os.environ.get("COLONY_EMBED_QUERY_INSTRUCTION", default_instr)
        return await self._embed((instr + query) if instr else query)

    @staticmethod
    def compute_effective_confidence(
        base_confidence: float,
        source_reliability: float,
        corroboration_count: int,
        contradiction_count: int,
        recalls: int,
        last_verified_at: Optional[Any],
        created_at: Any,
        epistemic_state: str,
        now: Any,
    ) -> float:
        """Compute effective confidence from multiple signals.

        Args:
            base_confidence: Initial confidence (0-1)
            source_reliability: Reliability of the source (0-1)
            corroboration_count: Number of corroborating memories
            contradiction_count: Number of contradicting memories
            recalls: Number of times recalled
            last_verified_at: Last verification timestamp or None
            created_at: Creation timestamp
            epistemic_state: Current epistemic state string
            now: Current timestamp

        Returns:
            Effective confidence in [0, 1].
        """
        from datetime import datetime as _dt, timezone as _tz

        if now is None:
            now = _dt.now(_tz.utc)
        if hasattr(now, "to_native"):
            now = now.to_native()
        if isinstance(now, str):
            now = _dt.fromisoformat(now.replace("Z", "+00:00"))
        if hasattr(created_at, "to_native"):
            created_at = created_at.to_native()
        if isinstance(created_at, str):
            created_at = _dt.fromisoformat(created_at.replace("Z", "+00:00"))

        # Source weight
        confidence = base_confidence * source_reliability

        # Corroboration / contradiction adjustment
        net_support = corroboration_count - contradiction_count
        confidence *= min(1.0, 1.0 + net_support * 0.1)

        # Recall reinforcement (diminishing returns)
        confidence *= min(1.3, 1.0 + recalls * 0.03)

        # Recency weighting (v0.21.0, configurable half-life + floor — see
        # _recency_factor). Recent memories surface; VERIFIED memories are
        # additionally floored at 0.9 by the epistemic-state clamp below.
        days_old = max(0, (now - created_at).days)
        recency_factor = _recency_factor(days_old)
        confidence *= recency_factor

        # Verification boost
        if last_verified_at:
            if hasattr(last_verified_at, "to_native"):
                last_verified_at = last_verified_at.to_native()
            if isinstance(last_verified_at, str):
                last_verified_at = _dt.fromisoformat(last_verified_at.replace("Z", "+00:00"))
            if (now - last_verified_at).days < 7:
                confidence *= 1.2

        # State clamp
        if epistemic_state == EpistemicState.VERIFIED.value:
            confidence = max(confidence, 0.9)
        elif epistemic_state in (EpistemicState.STALE.value, EpistemicState.SUPERSEDED.value):
            confidence *= 0.3
        elif epistemic_state == EpistemicState.DEPRECATED.value:
            confidence *= 0.1

        return min(1.0, max(0.0, confidence))

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
        source_type: str = "inference",
        source_uri: Optional[str] = None,
        source_version: Optional[str] = None,
        content_hash: Optional[str] = None,
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
            source_type: Origin of this memory (conversation, file, tool_output, user_assertion, inference)
            source_uri: Optional URI referencing the source
            source_version: Optional version string for the source
            content_hash: Optional SHA-256 hash of the content

        Returns:
            The UUID of the newly created Memory node.
        """
        metadata = metadata or {}
        # Guard (v0.21.1): never store empty/whitespace memories — they were
        # accumulating as duplicate junk nodes.
        if not content or not content.strip():
            return ""
        max_importance = MAX_IMPORTANCE.get(source_type, 0.7)
        if importance > max_importance:
            logger.warning(
                "Importance %.2f for source_type '%s' exceeds max %.2f; clamping.",
                importance, source_type, max_importance,
            )
            importance = max_importance
        importance = max(0.0, min(1.0, importance))

        # Resolve person_id from explicit arg or metadata fallback
        person_id = person_id or metadata.get("person_id")
        # Never mint a :Person node for a non-contact sentinel. Ids like 'default'/'unknown'/empty are
        # NOT real people; attributing memories to them created a catch-all junk Person that polluted
        # per-person recall (a host whose resolver fell back to "default" dumped most of its memory
        # onto one pseudo-contact). Such memories are stored UNATTRIBUTED (no :ABOUT edge) instead.
        # Real contact ids still create/link a Person normally (that is the graph's discovery design).
        if isinstance(person_id, str):
            person_id = person_id.strip() or None
            if person_id and person_id.lower() in {"default", "unknown", "none", "null", "anonymous"}:
                person_id = None
        elif person_id is not None:
            person_id = None
        # Resolve session_id from explicit arg or metadata fallback
        session_id = session_id or (metadata.get("session_id") if metadata else None)

        source_type = (source_type or MemorySourceType.INFERENCE.value).lower()
        source_uri = source_uri or None
        source_version = source_version or None
        # Always derive a content hash so identical memories can be deduped
        # (v0.21.1 — previously null, so every write created a duplicate node).
        content_hash = content_hash or hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_reliability = SOURCE_RELIABILITY.get(source_type, 0.5)
        protected = source_type == MemorySourceType.USER_ASSERTION
        base_confidence = importance
        epistemic_state = EpistemicState.INFERRED
        created_at = self._utcnow()

        effective_confidence = self.compute_effective_confidence(
            base_confidence=base_confidence,
            source_reliability=source_reliability,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=created_at,
            epistemic_state=epistemic_state.value,
            now=created_at,
        )

        # Dedup (v0.21.1): if an identical memory already exists (same content
        # hash), reinforce it instead of creating a duplicate — and skip the
        # embed cost entirely. This collapses the runaway duplication (e.g. a
        # recurring cron prompt that had been stored 70+ times).
        if content_hash:
            async with self.driver.session(database=self.database) as session:
                dq = await session.run(
                    """
                    MATCH (m:Memory {content_hash: $content_hash})
                    WHERE m.superseded_by IS NULL
                    SET m.accessed_at = datetime(),
                        m.corroboration_count = coalesce(m.corroboration_count, 0) + 1,
                        m.strength = CASE WHEN coalesce(m.strength, 0.0) < 1.0
                                          THEN coalesce(m.strength, 0.0) + 0.05 ELSE 1.0 END
                    RETURN m.id AS id
                    ORDER BY m.created_at ASC
                    LIMIT 1
                    """,
                    content_hash=content_hash,
                )
                existing = await dq.single()
                if existing is not None:
                    logger.debug("store_memory dedup: reinforced %s", existing["id"])
                    return existing["id"]

        # Compute embedding. If an embedder is configured but we can't get a
        # usable vector (outage that survived retries), FAIL the write rather than
        # create an unsearchable memory (silent loss). The vector lives in the
        # LanceDB store; the Neo4j m.embedding property is secondary/best-effort.
        embedding: Optional[List[float]] = None
        if self._embed_fn is not None:
            embedding = await self._embed(content)
            if not embedding:
                raise RuntimeError(
                    "embedding unavailable — refusing to store unsearchable memory")

        # Preserve dict for vector store before stringifying for Neo4j
        metadata_dict = metadata
        metadata_str = str(metadata_dict) if metadata_dict else "{}"

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
                    session_id: $session_id,
                    source_type: $source_type,
                    source_uri: $source_uri,
                    source_version: $source_version,
                    content_hash: $content_hash,
                    base_confidence: $base_confidence,
                    source_reliability: $source_reliability,
                    corroboration_count: 0,
                    contradiction_count: 0,
                    effective_confidence: $effective_confidence,
                    epistemic_state: $epistemic_state,
                    protected: $protected,
                    last_verified_at: null,
                    superseded_by: null,
                    provenance: []
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
                WITH m
                FOREACH (_ IN CASE WHEN $source_uri IS NOT NULL AND $source_type = "file" THEN [1] ELSE [] END |
                    MERGE (fa:FileAnchor {uri: $source_uri})
                    ON CREATE SET fa.first_seen = datetime()
                    CREATE (m)-[:DERIVED_FROM {derivation_type: "file_read"}]->(fa)
                )
                RETURN m.id AS id
                """,
                content=content,
                memory_type=memory_type,
                importance=importance,
                entities=entities,
                embedding=embedding,
                metadata=metadata_str,
                person_id=person_id,
                session_id=session_id,
                source_type=source_type,
                source_uri=source_uri,
                source_version=source_version,
                content_hash=content_hash,
                base_confidence=base_confidence,
                source_reliability=source_reliability,
                effective_confidence=effective_confidence,
                epistemic_state=epistemic_state,
                protected=protected,
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
                        "person_id": metadata_dict.get("person_id") if metadata_dict else None,
                        "tags": metadata_dict.get("tags", []) if metadata_dict else [],
                        "created_at": metadata_dict.get("created_at") if metadata_dict else None,
                        "session_id": session_id,
                        "source_type": source_type,
                        "source_uri": source_uri,
                        "source_version": source_version,
                        "content_hash": content_hash,
                        "effective_confidence": effective_confidence,
                        "epistemic_state": epistemic_state.value,
                        "protected": protected,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to write memory to vector store: %s", exc)

        return memory_id

    async def record_turn(
        self,
        session_id: str,
        contact_id: Optional[str],
        topics: List[str],
        entities: List[str],
        tools_used: List[str],
        summary: Optional[str],
    ) -> Optional[str]:
        """Store a conversation turn as an episodic memory.

        Creates a :Memory node of type ``episodic`` linked to the
        conversation session and contact.  Entities and topics are
        merged as :Entity nodes.  Tools used are stored in metadata.

        Args:
            session_id: Hermes session identifier
            contact_id: Optional contact / person identifier
            topics: Extracted topics from the turn
            entities: Named entities mentioned
            tools_used: Tool names invoked during the turn
            summary: Human-readable summary of the exchange

        Returns:
            The UUID of the created Memory node, or None if storage fails.
        """
        if not summary:
            return None
        # Salience gate: don't memorialize internal-plumbing turns (context-compaction references and
        # host-specific system-prompt wrappers / self-checks). Generic markers are built in; a
        # deployment adds its own via COLONY_MEMORY_SKIP_MARKERS ('|'-separated, case-insensitive).
        # This is what keeps the memory graph facts-and-events, not a verbatim transcript log.
        _sl = summary.lower()
        _skip = ("[context compaction", "[post-compaction", "reference only]", "[context summary]")
        _env = os.environ.get("COLONY_MEMORY_SKIP_MARKERS", "")
        if any(m in _sl for m in _skip) or any(m.strip().lower() in _sl for m in _env.split("|") if m.strip()):
            logger.debug("record_turn: skipped low-salience / internal-marker turn")
            return None

        # Real salience score (attribution redesign Phase 2), replacing the old hardcoded
        # importance=0.85 that overrode the computed value. Signal from what the turn
        # actually carries: named entities (facts about people/things), tool use (an
        # action happened), and substance (length). A throwaway "ok thanks" scores low
        # and decays fast; a fact-dense exchange scores high and persists.
        _ent_n = len(entities or [])
        _score = 0.35
        _score += min(0.30, 0.10 * _ent_n)        # up to +0.30 for entities
        if tools_used:
            _score += 0.15                         # an action was taken
        if len(summary) > 240:
            _score += 0.10                         # substantive exchange
        if "?" in summary:
            _score += 0.05                         # a question = intent/curiosity worth recalling
        importance = round(min(_score, 0.95), 3)

        # Optional distillation (shadow by default): store the salient content rather
        # than the verbatim "User:/Agent:" wrapper. Off => log what it WOULD store so
        # we can validate before flipping live. On => strip the wrapper prefix.
        content = summary
        _distill = os.environ.get("COLONY_DISTILL_TURNS", "0") not in ("0", "false", "no")
        if _distill:
            _lines = [ln.split(":", 1)[1].strip() if ":" in ln else ln
                      for ln in summary.splitlines()]
            content = " — ".join(x for x in _lines if x) or summary
        else:
            logger.debug("distill(shadow): would store salient content for session %s (imp=%.2f)",
                         session_id, importance)

        metadata: Dict[str, Any] = {
            "turn": True,
            "topics": topics,
            "tools_used": tools_used,
            "salience": importance,
        }

        try:
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return await self.store_memory(
                content=content,
                memory_type="episodic",
                entities=entities or [],
                metadata=metadata,
                importance=importance,
                person_id=contact_id,
                source_type=MemorySourceType.CONVERSATION.value,
                source_uri=f"session:{session_id}",
                content_hash=content_hash,
            )
        except Exception as exc:
            logger.warning("record_turn failed: %s", exc)
            return None

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
        min_confidence: float = 0.1,
    ) -> List[Dict[str, Any]]:
        """Retrieve memories by semantic similarity with strength decay.

        Uses LanceDB for ANN search, then hydrates full Memory nodes from
        Neo4j with entity mentions.  Falls back to graph-only keyword
        recall if no vector store or embedding function is configured.

        Returns:
            A list of memory dicts, each annotated with ``entities`` and
            sorted by relevance descending.
        """
        # Vector search path: embed query (with instruction) → LanceDB ANN → Neo4j hydration
        if self._vector_store is not None and self._embed_fn is not None:
            try:
                embedding = await self._embed_query(query)
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
                            # Filter terminal epistemic states and low confidence
                            epistemic_state = mem.get("epistemic_state", "inferred")
                            if epistemic_state in ("stale", "superseded", "deprecated", "archived"):
                                continue
                            effective_confidence = float(mem.get("effective_confidence", mem.get("strength", 1.0)))
                            if effective_confidence < min_confidence:
                                continue
                            mem["relevance"] = vector_score * effective_confidence
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
                  AND NOT m.epistemic_state IN ["stale", "superseded", "deprecated", "archived"]
                OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
                WITH m, collect(e.name) AS entity_names
                RETURN m {.*, entities: entity_names} AS memory,
                       m.effective_confidence AS relevance
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
                effective_confidence = float(mem.get("effective_confidence", mem.get("strength", 1.0)))
                if effective_confidence < min_confidence:
                    continue
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
        """Apply Ebbinghaus forgetting curve to all non-identity, non-protected memories.

        Formula: strength = importance * e^(-lambda * days) * (1 + recalls * 0.2)

        Where lambda = ln(2) / half_life_days.
        - Identity memories are skipped (never decay).
        - Protected memories are skipped.
        - Procedural memories use lambda / 2 (half rate).
        - Result is capped at 1.0.

        Args:
            half_life_days: Number of days for strength to halve (default 7).
        """
        lambda_normal = math.log(2) / max(half_life_days, 0.001)
        lambda_procedural = lambda_normal / 2

        async with self.driver.session(database=self.database) as session:
            # First pass: update strength
            await session.run(
                """
                MATCH (m:Memory)
                WHERE m.type <> 'identity' AND coalesce(m.protected, false) = false
                WITH m,
                     toFloat(duration.inDays(coalesce(m.accessed_at, m.created_at, datetime()), datetime()).days) AS days_since,
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

        # Second pass: update effective_confidence in batches
        await self._update_effective_confidence_batch()

    async def prune_weak_memories(
        self,
        threshold: float = 0.05,
    ) -> int:
        """Delete memories whose strength has decayed below *threshold*.

        Only targets memories in ``inferred``, ``observed``, or ``stale``
        epistemic states. Skips protected memories, ``corroborated``,
        ``verified``, and fully terminal states (``superseded``,
        ``deprecated``, ``archived``).

        Returns:
            The number of pruned Memory nodes.
        """
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory)
                WHERE m.strength < $threshold
                  AND coalesce(m.protected, false) = false
                  AND m.epistemic_state IN ["inferred", "observed", "stale"]
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

    async def _update_effective_confidence_batch(self, batch_size: int = 1000) -> None:
        """Update effective_confidence for all memories in batches."""
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        offset = 0
        while True:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    """
                    MATCH (m:Memory)
                    RETURN m {
                        .id, .base_confidence, .source_reliability,
                        .corroboration_count, .contradiction_count,
                        .recalls, .last_verified_at, .created_at,
                        .epistemic_state
                    } AS mem
                    SKIP $offset LIMIT $limit
                    """,
                    offset=offset,
                    limit=batch_size,
                )
                rows = [dict(r["mem"]) async for r in result]
                if not rows:
                    break
                for row in rows:
                    new_confidence = self.compute_effective_confidence(
                        base_confidence=row.get("base_confidence") or 1.0,
                        source_reliability=row.get("source_reliability") or 0.5,
                        corroboration_count=row.get("corroboration_count") or 0,
                        contradiction_count=row.get("contradiction_count") or 0,
                        recalls=row.get("recalls") or 0,
                        last_verified_at=row.get("last_verified_at"),
                        created_at=row.get("created_at") or now,
                        epistemic_state=row.get("epistemic_state") or "inferred",
                        now=now,
                    )
                    await session.run(
                        """
                        MATCH (m:Memory {id: $memory_id})
                        SET m.effective_confidence = $effective_confidence
                        """,
                        memory_id=row["id"],
                        effective_confidence=new_confidence,
                    )
                offset += batch_size

    async def verify_memory(self, memory_id: str) -> None:
        """Mark a memory as manually verified.

        Sets last_verified_at, transitions epistemic_state to ``verified``
        if currently in an active state, and floors effective_confidence
        at 0.9.
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                SET m.last_verified_at = datetime(),
                    m.epistemic_state = CASE
                        WHEN m.epistemic_state IN ["inferred", "observed", "corroborated"]
                        THEN "verified"
                        ELSE m.epistemic_state
                    END,
                    m.effective_confidence = CASE
                        WHEN m.effective_confidence < 0.9 THEN 0.9
                        ELSE m.effective_confidence
                    END
                """,
                memory_id=memory_id,
            )

    async def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single memory by ID.

        Returns:
            Memory dict or None if not found.
        """
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
                WITH m, collect(e.name) AS entity_names
                RETURN m {.*, entities: entity_names} AS memory
                """,
                memory_id=memory_id,
            )
            record = await result.single()
            return dict(record["memory"]) if record else None

    async def transition_epistemic_state(
        self,
        memory_id: str,
        new_state: str,
        superseded_by: Optional[str] = None,
    ) -> None:
        """Transition a memory to a new epistemic state.

        Args:
            memory_id: UUID of the memory to transition.
            new_state: Target EpistemicState value.
            superseded_by: Optional UUID of the memory that superseded this one.
        """
        async with self.driver.session(database=self.database) as session:
            await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                SET m.epistemic_state = $new_state
                WITH m
                FOREACH (_ IN CASE WHEN $superseded_by IS NOT NULL THEN [1] ELSE [] END |
                    SET m.superseded_by = $superseded_by
                )
                """,
                memory_id=memory_id,
                new_state=new_state,
                superseded_by=superseded_by,
            )

    async def archive_memories(self, max_age_days: int = 30) -> int:
        """Archive memories that have been in a terminal state for too long.

        Relabels :Memory to :ArchivedMemory, copies key relationships,
        removes from vector store, and deletes the original.

        Returns:
            Number of memories archived.
        """
        archived = 0
        async with self.driver.session(database=self.database) as session:
            result = await session.run(
                """
                MATCH (m:Memory)
                WHERE m.epistemic_state IN ["superseded", "deprecated", "stale"]
                  AND duration.inDays(m.accessed_at, datetime()).days >= $max_age_days
                RETURN m.id AS id
                """,
                max_age_days=max_age_days,
            )
            memory_ids = [r["id"] async for r in result]

        for memory_id in memory_ids:
            try:
                async with self.driver.session(database=self.database) as session:
                    # Copy to ArchivedMemory with key relationships
                    await session.run(
                        """
                        MATCH (m:Memory {id: $memory_id})
                        CREATE (a:ArchivedMemory)
                        SET a = properties(m)
                        WITH m, a
                        OPTIONAL MATCH (m)-[r:MENTIONS]->(e:Entity)
                        FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END |
                            CREATE (a)-[:MENTIONS {created_at: datetime()}]->(e)
                        )
                        WITH m, a
                        OPTIONAL MATCH (m)-[r:ABOUT]->(p:Person)
                        FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
                            CREATE (a)-[:ABOUT {created_at: datetime()}]->(p)
                        )
                        WITH m, a
                        OPTIONAL MATCH (m)-[r:SUPERSEDES]->(old:Memory)
                        FOREACH (_ IN CASE WHEN old IS NOT NULL THEN [1] ELSE [] END |
                            CREATE (a)-[:SUPERSEDES {superseded_at: r.superseded_at}]->(old)
                        )
                        WITH m, a
                        OPTIONAL MATCH (m)-[r:DERIVED_FROM]->(fa:FileAnchor)
                        FOREACH (_ IN CASE WHEN fa IS NOT NULL THEN [1] ELSE [] END |
                            CREATE (a)-[:DERIVED_FROM {derivation_type: r.derivation_type}]->(fa)
                        )
                        DETACH DELETE m
                        """,
                        memory_id=memory_id,
                    )
                # Remove from vector store
                if self._vector_store is not None:
                    try:
                        from colony_sidecar.vector.collections import Collection
                        await self._vector_store.delete(
                            collection=Collection.MEMORIES,
                            id=memory_id,
                        )
                    except Exception as exc:
                        logger.debug("Failed to remove archived memory from vector store: %s", exc)
                archived += 1
            except Exception as exc:
                logger.warning("Failed to archive memory %s: %s", memory_id, exc)

        return archived

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
        "MATCH (m1:Memory)-[:ABOUT]->(p:Person {id: $person_id})\n"
        "MATCH (m2:Memory)-[:ABOUT]->(p)\n"
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
        "WHERE ($person_id IS NULL OR (m)-[:ABOUT]->(:Person {id: $person_id}))\n"
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

    async def delete_person(self, person_id: str) -> bool:
        """Permanently delete a Person node and all attached relationships."""
        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (p:Person {id: $person_id}) DETACH DELETE p RETURN count(p) AS deleted",
                    person_id=person_id,
                )
                record = await result.single()
                deleted = record["deleted"] if record else 0
                logger.debug(
                    "delete_person %s deleted=%d %.1fms",
                    person_id, deleted, (time.monotonic() - t0) * 1000,
                )
                return deleted > 0
        except Exception as exc:
            logger.error("delete_person failed for %s: %s", person_id, exc)
            return False

    async def get_people_with_substance(
        self,
        min_signals: int = 2,
        min_memories: int = 2,
    ) -> List[Dict[str, Any]]:
        """Return Person nodes that have enough substance to become contacts.

        A person has substance if they have:
        - a name AND (a phone or email)  
        - OR a name AND (>= min_signals OR >= min_memories)
        """
        t0 = time.monotonic()
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    """
                    MATCH (p:Person)
                    WHERE p.name IS NOT NULL
                    OPTIONAL MATCH (p)-[:EXHIBITED]->(s:Signal)
                    OPTIONAL MATCH (m:Memory)-[:ABOUT]->(p)
                    WITH p, count(s) AS sigs, count(m) AS mems
                    WHERE p.phone IS NOT NULL OR p.email IS NOT NULL
                       OR sigs >= $min_signals OR mems >= $min_memories
                    RETURN p.id AS id,
                           p.name AS name,
                           p.phone AS phone,
                           p.email AS email,
                           coalesce(p.score, 0.0) AS score,
                           coalesce(p.tier, 'regular') AS tier,
                           sigs,
                           mems
                    """,
                    min_signals=min_signals,
                    min_memories=min_memories,
                )
                people = [dict(r) async for r in result]
                logger.debug(
                    "get_people_with_substance count=%d %.1fms",
                    len(people), (time.monotonic() - t0) * 1000,
                )
                return people
        except Exception as exc:
            logger.error("get_people_with_substance failed: %s", exc)
            return []

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
        store = None,  # Optional SQLiteContactStore for reverse sync
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
            # Sync to SQLite if linked contact exists
            if store is not None:
                try:
                    contact = await store.find_by_person_node_id(person_id)
                    if contact:
                        # scorer works in 0-100; the contact field is 0-1
                        _norm = new_score / 100.0 if new_score > 1.0 else new_score
                        await store.update_relationship_score(contact.contact_id, _norm)
                except Exception as exc:
                    logger.debug("Score sync to SQLite failed for %s: %s", person_id, exc)
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

    # Property names permitted on Person nodes. Values are always passed as
    # parameters; this allowlist guards the one remaining interpolation point
    # (the property name itself) against accidental misuse from a caller that
    # forwards an attacker-controlled dict.
    _PERSON_PROPS_ALLOWED = frozenset({
        "name", "tier", "score", "lastInteraction", "created_at",
        "baseline_msg_count",
        "baseline_length_mean",
        "baseline_length_m2",
        "baseline_length_std",
        "baseline_hour_histogram",
        "baseline_updated_at",
    })

    async def update_person(self, person_id: str, **props: Any) -> None:
        """Update arbitrary properties on a Person node.

        Only called from trusted internal code (BaselineStore).
        All values are passed as Neo4j parameters; property names are
        validated against ``_PERSON_PROPS_ALLOWED``.
        """
        if not props:
            return
        unknown = set(props) - self._PERSON_PROPS_ALLOWED
        if unknown:
            raise ValueError(
                f"update_person rejected unknown properties: {sorted(unknown)}"
            )
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
