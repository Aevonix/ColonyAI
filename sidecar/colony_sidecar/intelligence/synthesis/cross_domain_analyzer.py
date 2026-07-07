"""Cross-Domain Analyzer — synthesize insights across domains.

Analyzes relationships between:
- Health data (sleep, stress → availability predictions)
- Work patterns (deadlines → communication shifts)
- Social patterns (relationship dynamics → scheduling impact)
- Financial patterns (spending → stress correlation)

Produces actionable DomainInsight objects that can be validated,
scored for novelty, and delivered through appropriate channels.
"""

from __future__ import annotations

import logging
import math
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class DomainInsight:
    """Insight derived from cross-domain analysis.

    Captures a pattern or prediction that spans multiple domains,
    along with evidence, confidence, and an optional recommended action.

    Attributes:
        id: Unique insight identifier
        domains: List of domains this insight spans (e.g. ["health", "work"])
        insight_type: Category ("availability_prediction", "stress_pattern", etc.)
        description: Human-readable description of the insight
        confidence: Confidence score between 0 and 1
        actionable: Whether this insight has a concrete recommended action
        recommended_action: Suggested action if actionable
        supporting_evidence: Memory IDs, signal IDs, or descriptions backing this insight
    """

    id: str
    domains: List[str]
    insight_type: str
    description: str
    confidence: float
    actionable: bool
    recommended_action: Optional[str] = None
    supporting_evidence: List[str] = field(default_factory=list)


@runtime_checkable
class AnalyzerGraphClient(Protocol):
    """Protocol for graph access needed by cross-domain analysis."""

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]: ...


@runtime_checkable
class AnalyzerEventBus(Protocol):
    """Protocol for event emission from the analyzer."""

    async def emit_async(self, event: Any) -> None: ...


class CrossDomainAnalyzer:
    """Analyze patterns spanning multiple domains.

    Runs targeted analyzers for domain pairs (health+work,
    relationships+health, etc.) and produces scored insights.

    Args:
        graph_client: Graph database client for querying cross-domain data
        event_bus: Event bus for emitting insight discovery events
    """

    def __init__(
        self,
        graph_client: AnalyzerGraphClient,
        event_bus: AnalyzerEventBus,
    ) -> None:
        self.graph = graph_client
        self.events = event_bus

    async def analyze(
        self,
        domains: Optional[List[str]] = None,
    ) -> List[DomainInsight]:
        """Run cross-domain analysis across specified domains.

        If no domains are specified, all available analyzers run.
        Results are sorted by confidence descending.

        Args:
            domains: Optional list of domains to include. None means all.

        Returns:
            List of DomainInsight objects, highest confidence first.
        """
        insights: List[DomainInsight] = []

        # Health + Work: stress → availability
        if not domains or "health" in domains or "work" in domains:
            health_work = await self._analyze_health_work()
            insights.extend(health_work)

        # Relationships + Health: social stress patterns
        if not domains or "relationships" in domains or "health" in domains:
            relationships_health = await self._analyze_relationships_health()
            insights.extend(relationships_health)

        # Work + Relationships: deadline → social impact
        if not domains or "work" in domains or "relationships" in domains:
            work_relationships = await self._analyze_work_relationships()
            insights.extend(work_relationships)

        return sorted(insights, key=lambda i: i.confidence, reverse=True)

    @staticmethod
    def _pearson_correlation(xs: list, ys: list) -> float:
        """Compute Pearson correlation coefficient between two lists."""
        n = len(xs)
        if n < 2:
            return 0.0
        try:
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
            std_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
            std_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
            if std_x == 0.0 or std_y == 0.0:
                return 0.0
            return cov / (std_x * std_y)
        except (ArithmeticError, ValueError, ZeroDivisionError) as exc:
            logger.debug("Pearson correlation failed (treating as 0.0): %s", exc)
            return 0.0

    def _extract_date(self, mem: dict) -> Optional[datetime]:
        """Extract datetime from a memory dict."""
        created_at = mem.get("created_at")
        if created_at is None:
            return None
        def _aware(dt):
            # Mixed naive/aware datetimes make the later subtraction raise
            # TypeError (swallowed upstream -> insights silently vanish).
            if isinstance(dt, datetime) and dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        if isinstance(created_at, datetime):
            return _aware(created_at)
        # Neo4j DateTime object
        if hasattr(created_at, 'to_native'):
            return _aware(created_at.to_native())
        # ISO string
        try:
            if isinstance(created_at, str):
                return _aware(datetime.fromisoformat(created_at.replace("Z", "+00:00")))
        except (ValueError, AttributeError) as exc:
            logger.debug("_extract_date: unparseable created_at %r: %s", created_at, exc)
        return None

    def _correlate_domain_memories(
        self,
        mems_a: list,
        mems_b: list,
        window_days: float = 3.0,
    ) -> tuple:
        """Find temporally co-occurring pairs between two memory lists.

        Returns (pairs, correlation) where pairs is list of (mem_a, mem_b) tuples
        and correlation is the Pearson correlation of their strength values.
        """
        pairs = []
        for ma in mems_a:
            date_a = self._extract_date(ma)
            if date_a is None:
                continue
            for mb in mems_b:
                date_b = self._extract_date(mb)
                if date_b is None:
                    continue
                diff_days = abs((date_a - date_b).total_seconds()) / 86400
                if diff_days <= window_days:
                    pairs.append((ma, mb))
        if len(pairs) < 3:
            return pairs, 0.0
        strengths_a = [p[0].get("strength", 0.5) or 0.5 for p in pairs]
        strengths_b = [p[1].get("strength", 0.5) or 0.5 for p in pairs]
        corr = self._pearson_correlation(strengths_a, strengths_b)
        return pairs, corr

    async def _analyze_health_work(self) -> List[DomainInsight]:
        """Analyze health -> work availability patterns."""
        try:
            health_mems = await self.graph.recall(
                "sleep score stress energy fatigue exercise",
                limit=50, min_strength=0.2,
            )
            work_mems = await self.graph.recall(
                "deadline meeting cancelled productivity output",
                limit=50, min_strength=0.2,
            )
            pairs, correlation = self._correlate_domain_memories(health_mems, work_mems, window_days=3.0)
            if len(pairs) < 3 or abs(correlation) < 0.4:
                return []
            evidence = list({p[0].get("id", "") for p in pairs[:5]} | {p[1].get("id", "") for p in pairs[:5]})
            evidence = [e for e in evidence if e][:5]
            return [DomainInsight(
                id=str(uuid.uuid4()),
                domains=["health", "work"],
                insight_type="availability_prediction",
                description="Low sleep scores correlate with reduced work output in the following days",
                confidence=min(1.0, abs(correlation)),
                actionable=True,
                recommended_action="Block focus time after poor sleep nights",
                supporting_evidence=evidence,
            )]
        except Exception as e:
            logger.debug("_analyze_health_work failed: %s", e)
            return []

    async def _analyze_relationships_health(self) -> List[DomainInsight]:
        """Analyze relationship dynamics -> health impact."""
        try:
            relationship_mems = await self.graph.recall(
                "conflict argument tension difficult person stressful interaction",
                limit=50, min_strength=0.2,
            )
            health_mems = await self.graph.recall(
                "stress anxiety sleep disruption fatigue mood",
                limit=50, min_strength=0.2,
            )
            pairs, correlation = self._correlate_domain_memories(relationship_mems, health_mems, window_days=2.0)
            if len(pairs) < 3 or abs(correlation) < 0.4:
                return []
            evidence = list({p[0].get("id", "") for p in pairs[:5]} | {p[1].get("id", "") for p in pairs[:5]})
            evidence = [e for e in evidence if e][:5]
            return [DomainInsight(
                id=str(uuid.uuid4()),
                domains=["relationships", "health"],
                insight_type="stress_pattern",
                description="Difficult relationship interactions correlate with health signal spikes within 48 hours",
                confidence=min(1.0, abs(correlation)),
                actionable=True,
                recommended_action="Schedule recovery time after high-conflict interactions",
                supporting_evidence=evidence,
            )]
        except Exception as e:
            logger.debug("_analyze_relationships_health failed: %s", e)
            return []

    async def _analyze_work_relationships(self) -> List[DomainInsight]:
        """Analyze work patterns -> relationship impact."""
        try:
            work_stress_mems = await self.graph.recall(
                "deadline crunch sprint overloaded travel busy",
                limit=50, min_strength=0.2,
            )
            social_mems = await self.graph.recall(
                "cancelled plans late reply short message ignored social",
                limit=50, min_strength=0.2,
            )
            pairs, correlation = self._correlate_domain_memories(work_stress_mems, social_mems, window_days=3.0)
            if len(pairs) < 3 or abs(correlation) < 0.4:
                return []
            evidence = list({p[0].get("id", "") for p in pairs[:5]} | {p[1].get("id", "") for p in pairs[:5]})
            evidence = [e for e in evidence if e][:5]
            return [DomainInsight(
                id=str(uuid.uuid4()),
                domains=["work", "relationships"],
                insight_type="communication_shift",
                description="Heavy work stress periods correlate with reduced social responsiveness",
                confidence=min(1.0, abs(correlation)),
                actionable=True,
                recommended_action="Proactively communicate reduced availability during high-load periods",
                supporting_evidence=evidence,
            )]
        except Exception as e:
            logger.debug("_analyze_work_relationships failed: %s", e)
            return []
