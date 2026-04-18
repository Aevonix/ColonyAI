"""Novelty Scorer — rate how surprising or useful an insight is.

Scores connections and insights based on:
- Historical frequency (has this been observed before?)
- Domain distance (cross-domain insights score higher)
- Confidence delta (unexpected findings score higher)

Higher novelty scores indicate insights the user is less likely to
already know, making them more valuable to surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class NoveltyScore:
    """Novelty assessment for an insight or connection.

    Attributes:
        score: Overall novelty score between 0 and 1
        reasons: Human-readable explanations for the score
        historical_frequency: Number of prior observations of similar connections
        domain_distance: How far apart the involved domains are (0-1)
        confidence_delta: How much the confidence deviates from expectation
    """

    score: float
    reasons: List[str]
    historical_frequency: int
    domain_distance: float
    confidence_delta: float


@runtime_checkable
class ScoringGraphClient(Protocol):
    """Protocol for graph access needed by novelty scoring."""

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]: ...

    async def run_query(
        self,
        cypher: str,
        params: dict,
    ) -> List[Dict[str, Any]]: ...


@runtime_checkable
class Connectable(Protocol):
    """Protocol for objects that have source/target domain attributes."""

    @property
    def source_domain(self) -> str: ...

    @property
    def target_domain(self) -> str: ...

    @property
    def confidence(self) -> float: ...

    @property
    def description(self) -> str: ...


# Semantic distance matrix between domains.
# Higher values mean domains are more distant (cross-domain insights
# spanning distant domains are scored as more novel).
DOMAIN_DISTANCES: Dict[tuple[str, str], float] = {
    ("work", "relationships"): 0.6,
    ("health", "work"): 0.8,
    ("health", "finance"): 0.9,
    ("work", "finance"): 0.4,
}


class NoveltyScorer:
    """Score how novel or useful a connection or insight is.

    Combines historical frequency, domain distance, and confidence
    deviation into a single novelty score. Lower frequency + higher
    distance + higher confidence delta = more novel.

    Args:
        graph_client: Graph database client for historical lookups
    """

    def __init__(self, graph_client: ScoringGraphClient) -> None:
        self.graph = graph_client

    async def score_connection(self, connection: Connectable) -> NoveltyScore:
        """Score novelty of a discovered connection.

        Computes a weighted composite of three factors:
        - Frequency factor (40%): inverse of historical frequency
        - Domain distance (30%): semantic distance between domains
        - Confidence delta (30%): deviation from expected confidence

        Args:
            connection: A connection or insight with domain attributes

        Returns:
            NoveltyScore with composite score and explanations.
        """
        freq = await self._get_historical_frequency(connection)
        distance = await self._compute_domain_distance(
            connection.source_domain,
            connection.target_domain,
        )
        delta = await self._calculate_confidence_delta(connection)

        # Composite score: lower frequency + higher distance + higher delta = more novel
        frequency_factor = 1.0 / (1.0 + freq)
        score = frequency_factor * 0.4 + distance * 0.3 + delta * 0.3

        reasons: List[str] = []
        if freq == 0:
            reasons.append("Never observed before")
        if distance > 0.5:
            reasons.append("Cross-domain connection")
        if delta > 0.3:
            reasons.append("Unexpected confidence level")

        return NoveltyScore(
            score=min(1.0, score),
            reasons=reasons,
            historical_frequency=freq,
            domain_distance=distance,
            confidence_delta=delta,
        )

    async def _get_historical_frequency(self, connection: Connectable) -> int:
        """Count how many similar connections exist in the graph."""
        source_domain = connection.source_domain
        target_domain = connection.target_domain

        # Try run_query first (direct Cypher)
        if hasattr(self.graph, 'run_query'):
            try:
                cypher = """
MATCH (m:Memory {type: "connection"})
WHERE (m.metadata.source_domain = $source_domain
       AND m.metadata.target_domain = $target_domain)
   OR (m.metadata.source_domain = $target_domain
       AND m.metadata.target_domain = $source_domain)
RETURN count(m) AS frequency
"""
                records = await self.graph.run_query(cypher, {
                    "source_domain": source_domain,
                    "target_domain": target_domain,
                })
                if records:
                    return int(records[0].get("frequency", 0))
            except Exception as e:
                logger.debug("_get_historical_frequency query failed: %s", e)

        # Fallback: use recall with domain pair query
        try:
            query = f"{source_domain} {target_domain} connection pattern"
            results = await self.graph.recall(query, limit=50, min_strength=0.1)
            return len(results)
        except Exception as e:
            logger.debug("_get_historical_frequency recall fallback failed: %s", e)

        return 0

    async def _compute_domain_distance(self, source: str, target: str) -> float:
        """Compute domain distance from graph confidence data.

        Queries the graph for mean confidence of historical connections between
        the domain pair. Distance is derived as ``1 - mean_confidence``: high-
        confidence connections indicate domains are closely related (low
        distance); low-confidence connections indicate distant domains (high
        distance). Falls back to ``_calculate_domain_distance`` when the graph
        returns no data.

        Args:
            source: Source domain name.
            target: Target domain name.

        Returns:
            Distance between 0 (same / closely related) and 1 (maximally distant).
        """
        if source == target:
            return 0.0

        if hasattr(self.graph, "run_query"):
            try:
                cypher = """
MATCH (m:Memory {type: "connection"})
WHERE ((m.metadata.source_domain = $source_domain
        AND m.metadata.target_domain = $target_domain)
    OR (m.metadata.source_domain = $target_domain
        AND m.metadata.target_domain = $source_domain))
  AND m.metadata.confidence IS NOT NULL
RETURN avg(toFloat(m.metadata.confidence)) AS mean_confidence
"""
                records = await self.graph.run_query(
                    cypher,
                    {"source_domain": source, "target_domain": target},
                )
                if records and records[0].get("mean_confidence") is not None:
                    mean_conf = float(records[0]["mean_confidence"])
                    return max(0.0, min(1.0, 1.0 - mean_conf))
            except Exception as e:
                logger.debug("_compute_domain_distance query failed: %s", e)

        return self._calculate_domain_distance(source, target)

    @staticmethod
    def _calculate_domain_distance(source: str, target: str) -> float:
        """Calculate semantic distance between two domains.

        Uses a precomputed distance matrix. Unknown pairs default to 0.5.
        Same-domain connections have distance 0.

        Args:
            source: Source domain name
            target: Target domain name

        Returns:
            Distance between 0 (same domain) and 1 (maximally distant).
        """
        if source == target:
            return 0.0
        return DOMAIN_DISTANCES.get(
            (source, target),
            DOMAIN_DISTANCES.get((target, source), 0.5),
        )

    async def _calculate_confidence_delta(self, connection: Connectable) -> float:
        """Measure how unexpected the connection's confidence level is."""
        source_domain = connection.source_domain
        target_domain = connection.target_domain

        # Try run_query first
        if hasattr(self.graph, 'run_query'):
            try:
                cypher = """
MATCH (m:Memory {type: "connection"})
WHERE ((m.metadata.source_domain = $source_domain
        AND m.metadata.target_domain = $target_domain)
    OR (m.metadata.source_domain = $target_domain
        AND m.metadata.target_domain = $source_domain))
  AND m.metadata.confidence IS NOT NULL
RETURN avg(toFloat(m.metadata.confidence)) AS mean_confidence
"""
                records = await self.graph.run_query(cypher, {
                    "source_domain": source_domain,
                    "target_domain": target_domain,
                })
                if records and records[0].get("mean_confidence") is not None:
                    mean_conf = float(records[0]["mean_confidence"])
                    return abs(connection.confidence - mean_conf)
            except Exception as e:
                logger.debug("_calculate_confidence_delta query failed: %s", e)

        return 0.0
