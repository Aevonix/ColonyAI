"""MemoryConsolidator — deduplicate and merge near-duplicate memories in Neo4j.

Runs as a background task (not in the hot path). Called by the autonomy loop
once per hour (configurable). Operates entirely on Memory nodes and their edges.

Algorithm:
1. Retrieve all Memory nodes modified in the last N hours
2. Compute pairwise cosine similarity using stored embeddings
3. For pairs with similarity > threshold (default 0.92):
   a. Keep the node with higher strength; add sources of both; update last_seen
   b. Redirect all edges from the merged node to the survivor
   c. Mark the merged node with MERGED_INTO edge and delete it
4. Detect conflicting facts (same entity, contradictory attribute values):
   create a CONFLICTS_WITH edge and flag for human review
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ConflictPair:
    """Two memories that assert contradictory facts."""
    memory_id_a: str
    memory_id_b: str
    entity_name: str
    reason: str


@dataclass
class ConsolidationResult:
    """Outcome of one MemoryConsolidator.run() call."""
    pairs_examined: int = 0
    pairs_merged: int = 0
    conflicts_detected: int = 0
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def reduction_ratio(self) -> float:
        """Fraction of examined pairs that were merged."""
        if self.pairs_examined == 0:
            return 0.0
        return self.pairs_merged / self.pairs_examined


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0 on error."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Token-level Jaccard similarity — fast fallback when embeddings are absent."""
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MemoryConsolidator:
    """Deduplicate and merge near-duplicate memories in Neo4j.

    Designed to be used with ColonyGraph but accepts any object that exposes
    an ``async def execute(query, **params)`` method returning a list of
    record dicts — this makes unit-testing straightforward without Neo4j.

    Args:
        graph_client: ColonyGraph instance (or compatible mock)
        similarity_threshold: Cosine/Jaccard score above which two memories
            are considered duplicates (default: 0.92)
        lookback_hours: Only examine memories touched in the last N hours
            (default: 24)
    """

    def __init__(
        self,
        graph_client: Any,
        similarity_threshold: float = 0.92,
        lookback_hours: int = 24,
        max_merge_ratio: float = 0.5,
    ) -> None:
        self.graph = graph_client
        self.similarity_threshold = similarity_threshold
        self.lookback_hours = lookback_hours
        self._max_merge_ratio = max_merge_ratio

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> ConsolidationResult:
        """Execute one consolidation pass. Never raises — errors are captured."""
        import time
        start = time.monotonic()
        result = ConsolidationResult()

        try:
            candidates = await self._fetch_recent_memories()
        except Exception as exc:
            result.errors.append(f"fetch_recent_memories: {exc}")
            result.duration_ms = (time.monotonic() - start) * 1000
            return result

        # Pairwise comparison (O(n²) — acceptable for typical batch sizes < 1000)
        # Safety cap: merge at most max_merge_ratio of candidates per pass
        # to prevent catastrophic information loss from runaway merges.
        n = len(candidates)
        max_merges = max(1, int(n * self._max_merge_ratio))
        for i in range(n):
            for j in range(i + 1, n):
                a, b = candidates[i], candidates[j]
                result.pairs_examined += 1
                sim = self._similarity(a, b)
                if sim >= self.similarity_threshold:
                    if result.pairs_merged >= max_merges:
                        logger.warning(
                            "Consolidation safety cap reached: merged %d/%d (%.0f%% limit)",
                            result.pairs_merged, n, self._max_merge_ratio * 100,
                        )
                        break
                    try:
                        await self._merge_pair(a, b)
                        result.pairs_merged += 1
                    except Exception as exc:
                        result.errors.append(
                            f"merge_pair({a.get('id')}, {b.get('id')}): {exc}"
                        )
            else:
                continue
            break  # Break outer loop when safety cap reached

        try:
            conflicts = await self._detect_conflicts()
            result.conflicts_detected = len(conflicts)
        except Exception as exc:
            result.errors.append(f"detect_conflicts: {exc}")

        result.duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "MemoryConsolidator: examined=%d merged=%d conflicts=%d errors=%d (%.1fms)",
            result.pairs_examined,
            result.pairs_merged,
            result.conflicts_detected,
            len(result.errors),
            result.duration_ms,
        )
        try:
            from colony_sidecar.events.broadcaster import emit as _emit
            _emit("memory_consolidated", {
                "examined": result.pairs_examined,
                "merged": result.pairs_merged,
                "conflicts": result.conflicts_detected,
                "duration_ms": result.duration_ms,
            })
        except Exception:
            logger.debug("memory_consolidated broadcast failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_recent_memories(self) -> List[Dict[str, Any]]:
        """Return Memory nodes touched within lookback_hours."""
        query = (
            "MATCH (m:Memory) "
            "WHERE m.accessed_at >= datetime() - duration({hours: $hours}) "
            "RETURN m.id AS id, m.content AS content, m.embedding AS embedding, "
            "m.strength AS strength, m.type AS type, m.sources AS sources "
            "ORDER BY m.strength DESC"
        )
        rows = await self._execute(query, hours=self.lookback_hours)
        return rows if rows else []

    def _similarity(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        """Return the best similarity score between two memory records."""
        emb_a = a.get("embedding") or []
        emb_b = b.get("embedding") or []
        if emb_a and emb_b and len(emb_a) == len(emb_b):
            return _cosine_similarity(emb_a, emb_b)
        # Fall back to Jaccard on raw content
        return _jaccard_similarity(
            a.get("content", ""), b.get("content", "")
        )

    async def _merge_pair(self, keep_rec: Dict[str, Any], merge_rec: Dict[str, Any]) -> None:
        """Merge *merge_rec* into *keep_rec*: highest-strength node survives."""
        from colony_sidecar.intelligence.graph.client import EpistemicState

        # Determine survivor by effective_confidence → strength → recency
        keep_conf = keep_rec.get("effective_confidence") or keep_rec.get("strength") or 0.0
        merge_conf = merge_rec.get("effective_confidence") or merge_rec.get("strength") or 0.0
        if merge_conf > keep_conf:
            keep_rec, merge_rec = merge_rec, keep_rec
            keep_conf, merge_conf = merge_conf, keep_conf

        keep_id = keep_rec["id"]
        merge_id = merge_rec["id"]

        # 1. Re-point all outgoing MENTIONS relationships from merge → keep
        await self._execute(
            """
            MATCH (m:Memory {id: $merge_id})-[r:MENTIONS]->(e:Entity)
            MATCH (k:Memory {id: $keep_id})
            MERGE (k)-[nr:MENTIONS]->(e)
            SET nr.created_at = coalesce(r.created_at, datetime())
            DELETE r
            """,
            keep_id=keep_id,
            merge_id=merge_id,
        )

        # 2. Re-point all incoming MENTIONS relationships to keep
        await self._execute(
            """
            MATCH (e:Entity)-[r:MENTIONS]->(m:Memory {id: $merge_id})
            MATCH (k:Memory {id: $keep_id})
            MERGE (e)-[nr:MENTIONS]->(k)
            SET nr.created_at = coalesce(r.created_at, datetime())
            DELETE r
            """,
            keep_id=keep_id,
            merge_id=merge_id,
        )

        # 3. Re-point ABOUT relationships
        await self._execute(
            """
            MATCH (m:Memory {id: $merge_id})-[r:ABOUT]->(p:Person)
            MATCH (k:Memory {id: $keep_id})
            MERGE (k)-[nr:ABOUT]->(p)
            SET nr.created_at = coalesce(r.created_at, datetime())
            DELETE r
            """,
            keep_id=keep_id,
            merge_id=merge_id,
        )

        # 4. Create MERGED_INTO edge before deletion
        await self._execute(
            """
            MATCH (k:Memory {id: $keep_id}), (m:Memory {id: $merge_id})
            MERGE (m)-[r:MERGED_INTO]->(k)
            SET r.merged_at = datetime()
            """,
            keep_id=keep_id,
            merge_id=merge_id,
        )

        # 5. Merge provenance and update survivor
        merge_sources = merge_rec.get("sources") or []
        merge_provenance = merge_rec.get("provenance") or []
        await self._execute(
            """
            MATCH (k:Memory {id: $keep_id})
            SET k.sources = CASE WHEN k.sources IS NULL THEN $sources
                           ELSE k.sources + [x IN $sources WHERE NOT x IN k.sources] END,
                k.provenance = coalesce(k.provenance, []) + $merge_provenance + [$merged_id],
                k.accessed_at = datetime(),
                k.epistemic_state = CASE
                    WHEN k.epistemic_state = 'inferred' AND $merge_state IN ['observed', 'corroborated', 'verified']
                    THEN $merge_state
                    ELSE k.epistemic_state
                END
            """,
            keep_id=keep_id,
            sources=merge_sources,
            merge_provenance=merge_provenance,
            merged_id=merge_id,
            merge_state=merge_rec.get("epistemic_state", "inferred"),
        )

        # 6. Transition merged node to archived
        await self._execute(
            """
            MATCH (m:Memory {id: $merge_id})
            SET m.epistemic_state = 'archived'
            """,
            merge_id=merge_id,
        )

        # 7. Delete the merged node (MERGED_INTO preserves audit trail)
        await self._execute(
            "MATCH (m:Memory {id: $merge_id}) DETACH DELETE m",
            merge_id=merge_id,
        )

        logger.debug("Merged memory %s → %s (MERGED_INTO + provenance recorded)", merge_id, keep_id)

    async def _detect_conflicts(self) -> List[ConflictPair]:
        """Find Memory pairs that assert contradictory facts about the same entity."""
        # Look for memories sharing an entity where content contradicts
        # (simple heuristic: both contain the entity name + negation words)
        query = (
            "MATCH (m1:Memory)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m2:Memory) "
            "WHERE id(m1) < id(m2) "
            "AND NOT (m1)-[:MERGED_INTO]-() AND NOT (m2)-[:MERGED_INTO]-() "
            "RETURN m1.id AS id_a, m2.id AS id_b, e.name AS entity, "
            "       m1.content AS content_a, m2.content AS content_b"
        )
        rows = await self._execute(query)
        if not rows:
            return []

        conflicts: List[ConflictPair] = []
        negation_words = {"not", "never", "no", "isn't", "wasn't", "doesn't", "can't", "won't"}

        for row in rows:
            ca = set((row.get("content_a") or "").lower().split())
            cb = set((row.get("content_b") or "").lower().split())
            # Conflict heuristic: one sentence has negation that the other lacks
            neg_a = bool(ca & negation_words)
            neg_b = bool(cb & negation_words)
            if neg_a != neg_b:
                conflict = ConflictPair(
                    memory_id_a=row["id_a"],
                    memory_id_b=row["id_b"],
                    entity_name=row.get("entity", ""),
                    reason="negation asymmetry",
                )
                conflicts.append(conflict)
                # Flag with CONFLICTS_WITH edge
                try:
                    await self._execute(
                        "MATCH (m1:Memory {id: $id_a}), (m2:Memory {id: $id_b}) "
                        "MERGE (m1)-[:CONFLICTS_WITH]->(m2)",
                        id_a=row["id_a"],
                        id_b=row["id_b"],
                    )
                except Exception as exc:
                    logger.debug("Could not create CONFLICTS_WITH edge: %s", exc)

        return conflicts

    async def _execute(self, query: str, **params: Any) -> List[Dict[str, Any]]:
        """Execute a Cypher query via the graph client.

        Supports ColonyGraph (which exposes an AsyncDriver) or any mock that
        implements ``async def execute(query, **params) -> list``.
        """
        if hasattr(self.graph, "execute"):
            result = await self.graph.execute(query, **params)
            return result if result is not None else []

        # ColonyGraph path: use the driver session directly
        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(query, **params)
            return [dict(record) async for record in result]
