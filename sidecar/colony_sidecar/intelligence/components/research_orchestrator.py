"""Research Orchestrator — coordinate research across multiple sources.

Capabilities:
    - Source prioritization
    - Result synthesis
    - Citation tracking
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


class SourceType(str, Enum):
    """Types of research sources."""

    WEB = "web"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    MEMORY = "memory"
    API = "api"


@dataclass
class ResearchSource:
    """A research source.

    Attributes:
        type: Category of source
        name: Human-readable source name
        priority: Trust/quality weight (0-1, higher = more trusted)
        rate_limit: Optional requests-per-minute cap
    """

    type: SourceType
    name: str
    priority: float = 0.5
    rate_limit: Optional[int] = None


@dataclass
class ResearchResult:
    """A result from a single research source.

    Attributes:
        source: Name of the source that produced this result
        content: The actual content/finding
        confidence: How confident the source is (0-1)
        timestamp: When the result was obtained
        citations: Reference URLs or identifiers
    """

    source: str
    content: str
    confidence: float
    timestamp: datetime = field(default_factory=datetime.now)
    citations: List[str] = field(default_factory=list)


@dataclass
class ResearchReport:
    """Aggregated research report.

    Attributes:
        query: The original research query
        results: Individual results from each source
        synthesized_summary: Combined summary across all results
        confidence: Overall confidence score (average of results)
    """

    query: str
    results: List[ResearchResult] = field(default_factory=list)
    synthesized_summary: Optional[str] = None
    confidence: float = 0.0


class ResearchOrchestrator:
    """Orchestrate multi-source research.

    Manages a registry of research sources, queries them in priority
    order, synthesizes findings, and tracks citations.

    Source routing:
    - MEMORY: queries graph memory via ``graph.recall()``
    - KNOWLEDGE_GRAPH: returns structured knowledge graph context
    - WEB / API: returns ``None`` (external integrations not yet wired)

    Args:
        graph_client: Colony graph client for knowledge graph queries
        event_bus: Colony event bus for research lifecycle events
    """

    def __init__(self, graph_client: Any, event_bus: Any) -> None:
        self.graph = graph_client
        self.events = event_bus
        self._sources: List[ResearchSource] = []

    def register_source(self, source: ResearchSource) -> None:
        """Register a research source.

        Args:
            source: Source to add to the registry
        """
        self._sources.append(source)
        logger.debug("Registered research source: %s (%s)", source.name, source.type.value)

    def unregister_source(self, name: str) -> None:
        """Remove a research source by name.

        Args:
            name: Name of the source to remove
        """
        self._sources = [s for s in self._sources if s.name != name]

    async def research(self, query: str, max_sources: int = 3) -> ResearchReport:
        """Conduct research across registered sources.

        Queries sources in priority order (up to max_sources),
        then synthesizes results into a unified report.

        Args:
            query: The research question
            max_sources: Maximum number of sources to query

        Returns:
            Aggregated research report with synthesized findings
        """
        sorted_sources = sorted(
            self._sources,
            key=lambda s: s.priority,
            reverse=True,
        )[:max_sources]

        results: List[ResearchResult] = []
        for source in sorted_sources:
            result = await self._query_source(source, query)
            if result:
                results.append(result)

        summary = await self._synthesize(results, query)

        confidence = (
            sum(r.confidence for r in results) / len(results) if results else 0.0
        )

        report = ResearchReport(
            query=query,
            results=results,
            synthesized_summary=summary,
            confidence=confidence,
        )

        logger.debug(
            "Research complete for '%s': %d results, confidence=%.2f",
            query[:50],
            len(results),
            confidence,
        )
        return report

    async def _query_source(
        self,
        source: ResearchSource,
        query: str,
    ) -> Optional[ResearchResult]:
        """Query a single source, routing by source type.

        Returns:
            A ResearchResult if the source returned data, else None.
        """
        try:
            if source.type == SourceType.MEMORY:
                return await self._query_memory_source(source, query)
            elif source.type == SourceType.KNOWLEDGE_GRAPH:
                return await self._query_graph_source(source, query)
            elif source.type in (SourceType.API, SourceType.WEB):
                return None  # External integrations not yet wired
        except Exception as exc:
            logger.warning("Source %s query failed: %s", source.name, exc)
        return None

    async def _query_memory_source(
        self,
        source: ResearchSource,
        query: str,
    ) -> Optional[ResearchResult]:
        """Query graph memory for relevant information."""
        try:
            memories = await self.graph.recall(query, limit=5)
            if not memories:
                return None
            # Combine up to 3 most relevant memories into one result
            content = "\n".join(
                m.get("content", "") if isinstance(m, dict) else str(m)
                for m in memories[:3]
                if m
            )
            if not content.strip():
                return None
            return ResearchResult(
                source=source.name,
                content=content,
                confidence=min(0.9, source.priority * 0.9),
            )
        except Exception as exc:
            logger.debug("Memory source query failed: %s", exc)
            return None

    async def _query_graph_source(
        self,
        source: ResearchSource,
        query: str,
    ) -> Optional[ResearchResult]:
        """Query the knowledge graph using entity and type recall.

        Runs RECALL_BY_ENTITY (treating the query as an entity name) and
        RECALL_BY_TYPE (treating the query as a memory type) against Neo4j,
        then combines up to 3 results into a single ResearchResult.
        """
        from colony_sidecar.intelligence.graph.queries import RECALL_BY_ENTITY, RECALL_BY_TYPE

        try:
            memories: List[Any] = []
            async with self.graph.driver.session(database=self.graph.database) as session:
                entity_result = await session.run(
                    RECALL_BY_ENTITY,
                    entity_name=query,
                    min_strength=0.1,
                    limit=5,
                )
                memories.extend([record["memory"] async for record in entity_result])

                type_result = await session.run(
                    RECALL_BY_TYPE,
                    memory_type=query,
                    min_strength=0.1,
                    limit=5,
                )
                memories.extend([record["memory"] async for record in type_result])

            if not memories:
                return None

            content = "\n".join(
                m.get("content", "") if isinstance(m, dict) else str(m)
                for m in memories[:3]
                if m
            )
            if not content.strip():
                return None

            return ResearchResult(
                source=source.name,
                content=content,
                confidence=min(0.85, source.priority * 0.85),
            )
        except Exception as exc:
            logger.debug("Graph source query failed: %s", exc)
            return None

    async def _synthesize(
        self,
        results: List[ResearchResult],
        query: str,
    ) -> Optional[str]:
        """Synthesize results into a coherent summary.

        Combines the top results (by confidence) into a single narrative,
        prefixed with a summary header.
        """
        if not results:
            return None

        sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)
        parts = [f"Research summary for '{query[:60]}' — {len(results)} source(s):"]
        for r in sorted_results[:3]:
            if r.content:
                excerpt = r.content[:200].replace("\n", " ")
                parts.append(f"[{r.source}] {excerpt}")

        return "\n".join(parts)
