"""MemoryDistiller — promote frequently-recalled episodic memories into semantic facts.

Gap A from the intelligence-systems-improvement spec.  Runs as a weekly
background task in the autonomy loop (after consolidation and pruning).

Algorithm:
1. Identify episodic memories with high recall frequency and sufficient strength
2. Cluster by shared entity mentions (union-find on entity overlap)
3. For qualifying clusters (3+ members), create a semantic summary memory
4. Mark source memories as distilled (they continue to decay naturally)
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class DistillationResult:
    """Outcome of one MemoryDistiller.run() call."""

    clusters_found: int = 0
    memories_promoted: int = 0
    source_memories_marked: int = 0
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0


class MemoryDistiller:
    """Promote frequently-recalled episodic memories into durable semantic facts.

    Args:
        graph_client: ColonyGraph instance (or compatible mock with execute())
        metrics: Optional ColonyMetricsCollector for instrumentation
        min_recalls: Minimum recall count for a memory to be a distillation candidate
        min_strength: Minimum strength for a candidate
        min_age_days: Minimum age in days (only distill established memories)
        min_cluster_size: Minimum cluster size to trigger distillation
        candidate_limit: Max candidates per run
    """

    def __init__(
        self,
        graph_client: Any,
        metrics: Optional[Any] = None,
        min_recalls: int = 3,
        min_strength: float = 0.5,
        min_age_days: int = 7,
        min_cluster_size: int = 3,
        candidate_limit: int = 200,
    ) -> None:
        self.graph = graph_client
        self._metrics = metrics
        self._min_recalls = min_recalls
        self._min_strength = min_strength
        self._min_age_days = min_age_days
        self._min_cluster_size = min_cluster_size
        self._candidate_limit = candidate_limit

    async def run(self) -> DistillationResult:
        """Execute one distillation pass. Never raises — errors are captured."""
        import time
        start = time.monotonic()
        result = DistillationResult()

        try:
            candidates = await self._fetch_candidates()
        except Exception as exc:
            result.errors.append(f"fetch_candidates: {exc}")
            result.duration_ms = (time.monotonic() - start) * 1000
            return result

        if not candidates:
            result.duration_ms = (time.monotonic() - start) * 1000
            return result

        # Cluster by shared entities
        clusters = self._cluster_by_entities(candidates)
        result.clusters_found = len(clusters)

        # Distill each qualifying cluster
        for cluster in clusters:
            try:
                promoted = await self._distill_cluster(cluster)
                if promoted:
                    result.memories_promoted += 1
                    result.source_memories_marked += len(cluster)
            except Exception as exc:
                result.errors.append(f"distill_cluster: {exc}")

        result.duration_ms = (time.monotonic() - start) * 1000

        if self._metrics is not None:
            try:
                self._metrics.record_distillation_run(
                    clusters=result.clusters_found,
                    promoted=result.memories_promoted,
                )
            except Exception:
                pass

        logger.info(
            "MemoryDistiller: clusters=%d promoted=%d marked=%d errors=%d (%.1fms)",
            result.clusters_found,
            result.memories_promoted,
            result.source_memories_marked,
            len(result.errors),
            result.duration_ms,
        )
        return result

    async def _fetch_candidates(self) -> List[Dict[str, Any]]:
        """Fetch episodic memories eligible for distillation."""
        query = (
            "MATCH (m:Memory) "
            "WHERE m.type = 'episodic' "
            "  AND m.recalls >= $min_recalls "
            "  AND m.strength >= $min_strength "
            "  AND m.created_at < datetime() - duration({days: $min_age_days}) "
            "  AND coalesce(m.distilled, false) = false "
            "OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity) "
            "RETURN m.id AS id, m.content AS content, m.strength AS strength, "
            "       m.recalls AS recalls, collect(e.name) AS entities "
            "ORDER BY m.recalls DESC "
            "LIMIT $limit"
        )
        rows = await self._execute(
            query,
            min_recalls=self._min_recalls,
            min_strength=self._min_strength,
            min_age_days=self._min_age_days,
            limit=self._candidate_limit,
        )
        return rows if rows else []

    def _cluster_by_entities(
        self, candidates: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """Group memories that share 2+ entity mentions using union-find."""
        # Build entity → memory indices mapping
        entity_to_indices: Dict[str, List[int]] = defaultdict(list)
        for idx, mem in enumerate(candidates):
            for entity in (mem.get("entities") or []):
                entity_to_indices[entity].append(idx)

        # Union-find
        parent: List[int] = list(range(len(candidates)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Union memories that share entities
        for indices in entity_to_indices.values():
            for i in range(1, len(indices)):
                union(indices[0], indices[i])

        # Group by root and filter for shared-entity overlap >= 2
        groups: Dict[int, List[int]] = defaultdict(list)
        for idx in range(len(candidates)):
            groups[find(idx)].append(idx)

        clusters: List[List[Dict[str, Any]]] = []
        for member_indices in groups.values():
            if len(member_indices) < self._min_cluster_size:
                continue
            cluster = [candidates[i] for i in member_indices]
            # Verify the cluster actually shares entities (not just single-entity links)
            all_entities: List[Set[str]] = [
                set(m.get("entities") or []) for m in cluster
            ]
            if all_entities:
                shared = all_entities[0]
                for eset in all_entities[1:]:
                    shared = shared & eset
                # At least some pair must share 2+ entities
                if not shared:
                    # Check pairwise: any pair with 2+ shared?
                    has_overlap = False
                    for i in range(len(all_entities)):
                        for j in range(i + 1, len(all_entities)):
                            if len(all_entities[i] & all_entities[j]) >= 2:
                                has_overlap = True
                                break
                        if has_overlap:
                            break
                    if not has_overlap:
                        continue
            clusters.append(cluster)

        return clusters

    async def _distill_cluster(self, cluster: List[Dict[str, Any]]) -> bool:
        """Create a semantic memory from a cluster of episodic memories.

        Returns True if a new semantic memory was created.
        """
        # Sort by strength descending — strongest memory is the base
        cluster.sort(key=lambda m: float(m.get("strength") or 0), reverse=True)

        # Collect all unique entities across cluster
        all_entities: Set[str] = set()
        for mem in cluster:
            for e in (mem.get("entities") or []):
                all_entities.add(e)

        # Build summary from strongest memory + additional entity context
        base_content = cluster[0].get("content", "")
        base_entities = set(cluster[0].get("entities") or [])
        extra_entities = all_entities - base_entities

        if extra_entities:
            summary = f"[Distilled] {base_content} (also: {', '.join(sorted(extra_entities))})"
        else:
            summary = f"[Distilled] {base_content}"

        source_ids = [m["id"] for m in cluster if m.get("id")]

        # Create semantic memory node
        create_query = (
            "CREATE (m:Memory {"
            "  id: randomUUID(),"
            "  content: $content,"
            "  type: 'semantic',"
            "  importance: 1.0,"
            "  strength: 1.0,"
            "  recalls: 0,"
            "  created_at: datetime(),"
            "  accessed_at: datetime(),"
            "  provenance: $source_ids,"
            "  distilled: true"
            "}) "
            "WITH m "
            "FOREACH (entity_name IN $entities | "
            "  MERGE (e:Entity {name: entity_name}) "
            "  CREATE (m)-[:MENTIONS]->(e) "
            ") "
            "RETURN m.id AS id"
        )
        rows = await self._execute(
            create_query,
            content=summary,
            source_ids=source_ids,
            entities=list(all_entities),
        )
        if not rows:
            return False

        semantic_id = rows[0].get("id", "")
        logger.debug(
            "Created semantic memory %s from %d episodic sources",
            semantic_id, len(source_ids),
        )

        # Mark source memories as distilled
        if source_ids:
            await self._execute(
                "MATCH (m:Memory) WHERE m.id IN $ids "
                "SET m.distilled = true",
                ids=source_ids,
            )

        return True

    async def _execute(self, query: str, **params: Any) -> List[Dict[str, Any]]:
        """Execute a Cypher query via the graph client."""
        if hasattr(self.graph, "execute"):
            result = await self.graph.execute(query, **params)
            return result if result is not None else []

        async with self.graph.driver.session(database=self.graph.database) as session:
            result = await session.run(query, **params)
            return [dict(record) async for record in result]
