"""Connection Discoverer — find non-obvious connections across domains.

Scans memories, signals, and relationships to find patterns that connect
seemingly unrelated information (e.g., "Mom always calls after doctor
appointments", "stress spikes correlate with late-night work sessions").

Uses graph queries + signal analysis to discover temporal, causal,
entity-based, and behavioral patterns across domain boundaries.
"""

from __future__ import annotations

import logging
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class ConnectionType(str, Enum):
    """Categories of cross-domain connections.

    TEMPORAL: A happens before/after B (time correlation)
    CAUSAL: A causes B (inferred from repeated co-occurrence)
    TOPIC: Same topic surfaces across different contexts
    ENTITY: Same person/company appears in different domains
    BEHAVIORAL: Repeating behavior pattern across contexts
    """

    TEMPORAL = "temporal"
    CAUSAL = "causal"
    TOPIC = "topic"
    ENTITY = "entity"
    BEHAVIORAL = "behavioral"


@dataclass
class Connection:
    """A discovered connection between entities or concepts.

    Represents a cross-domain relationship with evidence tracking
    and confidence scoring.

    Attributes:
        id: Unique connection identifier
        connection_type: Category of connection
        source_domain: Origin domain ("health", "work", "relationships")
        target_domain: Destination domain
        entities: Related person IDs, memory IDs, etc.
        description: Human-readable description of the connection
        confidence: Confidence score between 0 and 1
        evidence: List of evidence references (memory IDs, signal IDs)
        first_observed: When connection was first detected
        last_observed: Most recent observation
        observation_count: How many times this connection has been seen
    """

    id: str
    connection_type: ConnectionType
    source_domain: str
    target_domain: str
    entities: List[str]
    description: str
    confidence: float
    evidence: List[str] = field(default_factory=list)
    first_observed: Optional[datetime] = None
    last_observed: Optional[datetime] = None
    observation_count: int = 1


@runtime_checkable
class GraphClient(Protocol):
    """Protocol for graph database access.

    Any object providing these methods can serve as the graph backend
    for connection discovery.
    """

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]: ...

    async def traverse_memory_connections(
        self,
        memory_id: str,
        max_depth: int = 3,
        min_strength: float = 0.3,
        limit: int = 20,
    ) -> List[Dict[str, Any]]: ...

    async def run_query(
        self,
        cypher: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]: ...


class ConnectionDiscoverer:
    """Discover cross-domain connections from graph data.

    Analyzes memories, signals, and entity relationships to surface
    patterns spanning multiple domains. Supports temporal correlation,
    entity co-occurrence, and behavioral pattern detection.

    Args:
        graph_client: Graph database client implementing GraphClient protocol
        signal_threshold: Minimum signal strength to consider (0-1)
    """

    def __init__(
        self,
        graph_client: GraphClient,
        signal_threshold: float = 0.6,
    ) -> None:
        self.graph = graph_client
        self.signal_threshold = signal_threshold

    async def discover_connections(
        self,
        person_id: Optional[str] = None,
        domain: Optional[str] = None,
        min_confidence: float = 0.5,
    ) -> List[Connection]:
        """Find connections, optionally filtered by person or domain.

        Runs temporal, entity, and behavioral pattern detectors in
        sequence and returns results sorted by confidence descending.

        Args:
            person_id: Restrict search to this person's connections
            domain: Restrict search to connections involving this domain
            min_confidence: Minimum confidence threshold for results

        Returns:
            Connections sorted by confidence, highest first.
        """
        connections: List[Connection] = []

        # Temporal patterns (e.g., "calls mom on Sundays")
        temporal = await self._find_temporal_patterns(person_id)
        connections.extend(temporal)

        # Entity co-occurrence (same person in different contexts)
        entity = await self._find_entity_patterns(person_id)
        connections.extend(entity)

        # Behavioral patterns (repeating actions)
        behavioral = await self._find_behavioral_patterns(person_id)
        connections.extend(behavioral)

        # Filter by confidence and optional domain
        filtered = [c for c in connections if c.confidence >= min_confidence]
        if domain:
            filtered = [
                c
                for c in filtered
                if c.source_domain == domain or c.target_domain == domain
            ]

        return sorted(filtered, key=lambda c: c.confidence, reverse=True)

    async def _find_temporal_patterns(
        self,
        person_id: Optional[str],
    ) -> List[Connection]:
        """Find time-based connections between events.

        Queries graph for Memory node pairs that co-occur within a 4-hour
        window and have been seen together at least 2 times in the past
        30 days.  Confidence scales with daily co-occurrence rate.
        """
        if person_id is not None:
            cypher = textwrap.dedent("""
            MATCH (m1:Memory)-[:ABOUT]->(p:Person {id: $person_id})
            MATCH (m2:Memory)-[:ABOUT]->(p)
            WHERE m1.id < m2.id
              AND abs(duration.between(m1.created_at, m2.created_at).hours) <= $window_hours
              AND m1.created_at >= datetime() - duration({days: $lookback_days})
            WITH m1, m2, count(*) AS co_occurrences
            WHERE co_occurrences >= $min_count
            RETURN m1.id AS source_id, m2.id AS target_id,
                   m1.type AS source_type, m2.type AS target_type,
                   m1.metadata AS source_meta, m2.metadata AS target_meta,
                   co_occurrences,
                   toFloat(co_occurrences) / $lookback_days AS daily_rate
            ORDER BY daily_rate DESC
            LIMIT 20
            """).strip()
            params: Dict[str, Any] = {
                "person_id": person_id,
                "window_hours": 4,
                "lookback_days": 30,
                "min_count": 2,
            }
        else:
            cypher = textwrap.dedent("""
            MATCH (m1:Memory), (m2:Memory)
            WHERE m1.id < m2.id
              AND abs(duration.between(m1.created_at, m2.created_at).hours) <= $window_hours
              AND m1.created_at >= datetime() - duration({days: $lookback_days})
            WITH m1, m2, count(*) AS co_occurrences
            WHERE co_occurrences >= $min_count
            RETURN m1.id AS source_id, m2.id AS target_id,
                   m1.type AS source_type, m2.type AS target_type,
                   m1.metadata AS source_meta, m2.metadata AS target_meta,
                   co_occurrences,
                   toFloat(co_occurrences) / $lookback_days AS daily_rate
            ORDER BY daily_rate DESC
            LIMIT 20
            """).strip()
            params = {
                "window_hours": 4,
                "lookback_days": 30,
                "min_count": 2,
            }

        connections: List[Connection] = []
        try:
            rows = await self.graph.run_query(cypher, params)
        except Exception:
            logger.exception("_find_temporal_patterns query failed")
            return connections

        for row in rows:
            source_type = row.get("source_type") or "unknown"
            target_type = row.get("target_type") or "unknown"
            daily_rate = float(row.get("daily_rate") or 0.0)
            confidence = min(1.0, daily_rate * 7)
            connections.append(
                Connection(
                    id=self._make_id(),
                    connection_type=ConnectionType.TEMPORAL,
                    source_domain=source_type,
                    target_domain=target_type,
                    entities=[row["source_id"], row["target_id"]],
                    description=(
                        f"Memory type '{source_type}' co-occurs with"
                        f" '{target_type}' within 4 hours"
                        f" ({int(row['co_occurrences'])}x in 30 days)"
                    ),
                    confidence=confidence,
                    evidence=[row["source_id"], row["target_id"]],
                )
            )
        return connections

    async def _find_entity_patterns(
        self,
        person_id: Optional[str],
    ) -> List[Connection]:
        """Find entity co-occurrence across domains.

        Detects when the same person, company, or concept appears in
        multiple domain contexts (e.g., "Jeff" in both health and work).
        Requires an entity to appear in at least 2 distinct domains.
        """
        cypher = textwrap.dedent("""
        MATCH (e:Entity)<-[:MENTIONS]-(m:Memory)
        WHERE ($person_id IS NULL OR (m)-[:ABOUT]->(:Person {id: $person_id}))
        WITH e, collect(DISTINCT m.id) AS mems
        WITH e, mems, size(mems) AS mem_count
        WHERE mem_count >= 2
        RETURN e.name AS entity_name,
               mem_count AS occurrence_count,
               mems[0..5] AS evidence_sample
        ORDER BY mem_count DESC
        LIMIT 20
        """).strip()
        params = {
            "person_id": person_id,
            "min_domain_count": 2,
        }

        connections: List[Connection] = []
        try:
            rows = await self.graph.run_query(cypher, params)
        except Exception:
            logger.exception("_find_entity_patterns query failed")
            return connections

        for row in rows:
            entity_name = row.get("entity_name") or "unknown"
            domains: List[str] = list(row.get("domains") or [])
            evidence_sample = row.get("evidence_sample") or []
            # Flatten nested lists from collect(collect(...))
            evidence: List[str] = []
            for item in evidence_sample:
                if isinstance(item, list):
                    evidence.extend(str(x) for x in item)
                else:
                    evidence.append(str(item))
            source_domain = domains[0] if domains else "unknown"
            target_domain = domains[1] if len(domains) > 1 else "multiple"
            confidence = min(1.0, (len(domains) - 1) * 0.3 + 0.4)
            connections.append(
                Connection(
                    id=self._make_id(),
                    connection_type=ConnectionType.ENTITY,
                    source_domain=source_domain,
                    target_domain=target_domain,
                    entities=[entity_name],
                    description=f"{entity_name} appears across {', '.join(domains)}",
                    confidence=confidence,
                    evidence=evidence[:5],
                )
            )
        return connections

    async def _find_behavioral_patterns(
        self,
        person_id: Optional[str],
    ) -> List[Connection]:
        """Find repeating behavior patterns across contexts.

        Analyzes Signal sequences where signal type A is consistently
        followed by signal type B within 24 hours, for a given person.
        Requires person_id; returns empty list when person_id is None.
        """
        connections: List[Connection] = []
        if person_id is None:
            return connections

        cypher = textwrap.dedent("""
        MATCH (p:Person {id: $person_id})-[:EXHIBITED]->(s1:Signal)
        MATCH (p)-[:EXHIBITED]->(s2:Signal)
        WHERE s1.signal_type <> s2.signal_type
          AND s2.timestamp > s1.timestamp
          AND duration.between(s1.timestamp, s2.timestamp).hours <= $window_hours
        WITH s1.signal_type AS type_a, s2.signal_type AS type_b,
             count(*) AS occurrences,
             avg(s2.normalized_value - s1.normalized_value) AS avg_delta,
             collect(s1.id)[0..5] AS evidence
        WHERE occurrences >= $min_occurrences
        RETURN type_a, type_b, occurrences, avg_delta, evidence
        ORDER BY occurrences DESC
        LIMIT 15
        """).strip()
        params = {
            "person_id": person_id,
            "window_hours": 24,
            "min_occurrences": 3,
        }

        try:
            rows = await self.graph.run_query(cypher, params)
        except Exception:
            logger.exception("_find_behavioral_patterns query failed")
            return connections

        for row in rows:
            type_a = row.get("type_a") or "unknown"
            type_b = row.get("type_b") or "unknown"
            occurrences = int(row.get("occurrences") or 0)
            confidence = min(1.0, occurrences / 10.0)
            connections.append(
                Connection(
                    id=self._make_id(),
                    connection_type=ConnectionType.BEHAVIORAL,
                    source_domain="signals",
                    target_domain="signals",
                    entities=[],
                    description=f"Signal {type_a} consistently precedes {type_b}",
                    confidence=confidence,
                    evidence=list(row.get("evidence") or []),
                )
            )
        return connections

    @staticmethod
    def _make_id() -> str:
        """Generate a unique connection ID."""
        return str(uuid.uuid4())
