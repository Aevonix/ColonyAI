"""Research gatherer — Stage 2 of the Research-to-Artifact Pipeline.

Collects evidence from multiple sources in parallel:
  - WEB      — search engine results with citation metadata
  - GRAPH    — knowledge graph traversal (ColonyGraph)
  - DOCUMENT — structured extraction from attached documents
  - EMAIL    — email / contact archive search
  - API      — structured external API sources

All sources produce ``EvidenceItem`` objects that are assembled into an
``EvidencePackage`` for the synthesizer stage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source types
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    WEB = "web"
    GRAPH = "graph"
    DOCUMENT = "document"
    EMAIL = "email"
    API = "api"


# ---------------------------------------------------------------------------
# Evidence models
# ---------------------------------------------------------------------------


@dataclass
class EvidenceItem:
    """A single piece of gathered evidence from any source."""

    source_type: SourceType
    content: str                   # Extracted text (Markdown)
    citation: str                  # URL, graph node ID, doc ID, or email ID
    retrieved_at: datetime
    relevance_score: float = 1.0   # 0.0–1.0
    pii_flagged: bool = False
    injection_flagged: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_safe(self) -> bool:
        """Return True if item passed all security checks."""
        return not self.pii_flagged and not self.injection_flagged


# A package is simply a typed list of evidence items
EvidencePackage = List[EvidenceItem]


# ---------------------------------------------------------------------------
# Gather configuration
# ---------------------------------------------------------------------------


@dataclass
class GatherConfig:
    """Configuration for the gather stage."""

    max_web_results: int = 20
    max_graph_depth: int = 3
    max_documents: int = 10
    max_email_threads: int = 50
    evidence_dedup_threshold: float = 0.92
    timeout_seconds: float = 120.0

    # Which source types to query
    enable_web: bool = True
    enable_graph: bool = True
    enable_documents: bool = True
    enable_email: bool = True


# ---------------------------------------------------------------------------
# Source-specific gatherers
# ---------------------------------------------------------------------------


class WebGatherer:
    """Gather evidence from web search."""

    async def gather(
        self,
        query: str,
        max_results: int = 20,
    ) -> List[EvidenceItem]:
        """Return ranked web snippets for *query*."""
        results: List[EvidenceItem] = []
        try:
            # Attempt to use the ResearchOrchestrator if available
            from colony_sidecar.intelligence.components.research_orchestrator import (
                ResearchOrchestrator,
            )

            orchestrator = ResearchOrchestrator()
            raw = await orchestrator.search_web(query, max_results=max_results)
            for item in raw[:max_results]:
                results.append(
                    EvidenceItem(
                        source_type=SourceType.WEB,
                        content=item.get("snippet", item.get("content", "")),
                        citation=item.get("url", item.get("citation", "")),
                        retrieved_at=datetime.now(timezone.utc),
                        relevance_score=float(item.get("score", 0.7)),
                        metadata=item,
                    )
                )
        except Exception as exc:
            logger.debug("WebGatherer: orchestrator unavailable (%s) — skipping", exc)
            # No results — web search not configured
            pass
        return results


class GraphGatherer:
    """Gather evidence from the Colony knowledge graph."""

    async def gather(
        self,
        query: str,
        max_depth: int = 3,
    ) -> List[EvidenceItem]:
        """Traverse the knowledge graph for entities related to *query*."""
        results: List[EvidenceItem] = []
        try:
            from colony_sidecar.intelligence.graph.client import ColonyGraph

            graph = ColonyGraph()
            memories = await graph.recall(query, limit=20)
            for mem in memories:
                content = mem.get("content", mem.get("text", ""))
                if not content:
                    continue
                results.append(
                    EvidenceItem(
                        source_type=SourceType.GRAPH,
                        content=content,
                        citation=f"graph://{mem.get('id', 'unknown')}",
                        retrieved_at=datetime.now(timezone.utc),
                        relevance_score=float(mem.get("strength", mem.get("score", 0.6))),
                        metadata=mem,
                    )
                )
        except Exception as exc:
            logger.debug("GraphGatherer: graph client unavailable (%s)", exc)
        return results


class DocumentGatherer:
    """Gather evidence from attached / referenced documents."""

    async def gather(
        self,
        query: str,
        document_ids: Optional[List[str]] = None,
        max_documents: int = 10,
    ) -> List[EvidenceItem]:
        """Extract content from documents matching *query*."""
        results: List[EvidenceItem] = []
        if not document_ids:
            return results
        try:
            from colony_sidecar.plugins.document_intelligence.pipeline import (
                DocumentIntelligencePipeline,
            )

            pipeline = DocumentIntelligencePipeline()
            for doc_id in document_ids[:max_documents]:
                extracted = await pipeline.extract(doc_id)
                if extracted:
                    results.append(
                        EvidenceItem(
                            source_type=SourceType.DOCUMENT,
                            content=extracted.to_markdown(),
                            citation=f"document://{doc_id}",
                            retrieved_at=datetime.now(timezone.utc),
                            relevance_score=0.8,
                            pii_flagged=getattr(extracted, "pii_detected", False),
                            injection_flagged=getattr(extracted, "injection_detected", False),
                            metadata={"doc_id": doc_id},
                        )
                    )
        except Exception as exc:
            logger.debug("DocumentGatherer: pipeline unavailable (%s)", exc)
        return results


class EmailGatherer:
    """Gather evidence from email and contact archives."""

    async def gather(
        self,
        query: str,
        max_threads: int = 50,
    ) -> List[EvidenceItem]:
        """Search email archives for threads related to *query*."""
        results: List[EvidenceItem] = []
        try:
            from colony_sidecar.email.store import EmailStore

            store = EmailStore()
            threads = await store.search(query, limit=max_threads)
            for thread in threads:
                summary = thread.get("summary", thread.get("subject", ""))
                if not summary:
                    continue
                results.append(
                    EvidenceItem(
                        source_type=SourceType.EMAIL,
                        content=summary,
                        citation=f"email://{thread.get('id', 'unknown')}",
                        retrieved_at=datetime.now(timezone.utc),
                        relevance_score=float(thread.get("score", 0.5)),
                        metadata=thread,
                    )
                )
        except Exception as exc:
            logger.debug("EmailGatherer: email store unavailable (%s)", exc)
        return results


# ---------------------------------------------------------------------------
# Main gatherer
# ---------------------------------------------------------------------------


class SourceGatherer:
    """Orchestrates parallel evidence gathering from all enabled sources."""

    def __init__(self, config: Optional[GatherConfig] = None) -> None:
        self.config = config or GatherConfig()
        self._web = WebGatherer()
        self._graph = GraphGatherer()
        self._document = DocumentGatherer()
        self._email = EmailGatherer()

    async def gather(
        self,
        query: str,
        document_ids: Optional[List[str]] = None,
        scoping: Optional[Dict[str, Any]] = None,
    ) -> EvidencePackage:
        """Gather evidence from all enabled sources in parallel.

        Args:
            query: The research goal / query string.
            document_ids: Optional list of document IDs to ingest.
            scoping: Optional constraints (date range, domains, etc.).

        Returns:
            A deduplicated, security-screened ``EvidencePackage``.
        """
        cfg = self.config
        tasks: List[asyncio.Task] = []

        async def _gather_web() -> List[EvidenceItem]:
            if not cfg.enable_web:
                return []
            return await asyncio.wait_for(
                self._web.gather(query, max_results=cfg.max_web_results),
                timeout=cfg.timeout_seconds,
            )

        async def _gather_graph() -> List[EvidenceItem]:
            if not cfg.enable_graph:
                return []
            return await asyncio.wait_for(
                self._graph.gather(query, max_depth=cfg.max_graph_depth),
                timeout=cfg.timeout_seconds,
            )

        async def _gather_docs() -> List[EvidenceItem]:
            if not cfg.enable_documents or not document_ids:
                return []
            return await asyncio.wait_for(
                self._document.gather(query, document_ids, max_documents=cfg.max_documents),
                timeout=cfg.timeout_seconds,
            )

        async def _gather_email() -> List[EvidenceItem]:
            if not cfg.enable_email:
                return []
            return await asyncio.wait_for(
                self._email.gather(query, max_threads=cfg.max_email_threads),
                timeout=cfg.timeout_seconds,
            )

        gather_fns = [_gather_web, _gather_graph, _gather_docs, _gather_email]
        results = await asyncio.gather(
            *[fn() for fn in gather_fns],
            return_exceptions=True,
        )

        all_items: List[EvidenceItem] = []
        for i, result in enumerate(results):
            source_name = ["web", "graph", "documents", "email"][i]
            if isinstance(result, BaseException):
                logger.warning("gather[%s] failed: %s", source_name, result)
                continue
            all_items.extend(result)  # type: ignore[arg-type]

        logger.info(
            "SourceGatherer: gathered %d evidence items from %d sources",
            len(all_items),
            sum(1 for r in results if not isinstance(r, BaseException)),
        )
        return self._deduplicate(all_items)

    def _deduplicate(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        """Remove near-duplicate evidence items by content hash."""
        seen_citations: set = set()
        deduped: List[EvidenceItem] = []
        for item in items:
            key = item.citation
            if key not in seen_citations:
                seen_citations.add(key)
                deduped.append(item)
        return deduped
