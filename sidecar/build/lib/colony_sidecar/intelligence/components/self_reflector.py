"""Self Reflector — evaluate own performance and identify improvements.

Analyzes:
    - Response quality patterns
    - Error patterns
    - Improvement opportunities
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)

# Score thresholds for issue/improvement classification
_CRITICAL_THRESHOLD = 0.5
_WARN_THRESHOLD = 0.7
_GOOD_THRESHOLD = 0.85


@dataclass
class Reflection:
    """A self-reflection on performance.

    Attributes:
        id: Unique reflection identifier
        area: What was reflected on ("response_quality", "tool_usage", "memory_recall")
        score: Quality score (0-1)
        issues: Problems identified during reflection
        improvements: Suggested improvements
        timestamp: When the reflection occurred
    """

    id: str
    area: str
    score: float
    issues: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


class SelfReflector:
    """Evaluate and improve own performance.

    Performs self-reflection across different areas (response quality,
    memory recall, tool usage) and tracks improvement opportunities.
    Attempts to query ``metrics_collector`` for real performance data;
    falls back to sensible defaults when metrics are unavailable.

    Args:
        metrics_collector: Colony metrics collector for gathering performance data
        event_bus: Colony event bus for emitting reflection events
    """

    def __init__(self, metrics_collector: Any, event_bus: Any) -> None:
        self.metrics = metrics_collector
        self.events = event_bus
        self._reflections: List[Reflection] = []

    async def reflect(self, area: Optional[str] = None) -> Reflection:
        """Perform self-reflection on an area.

        Args:
            area: Specific area to reflect on, or None for default (response_quality)

        Returns:
            Reflection with score, issues, and suggested improvements
        """
        if area == "response_quality" or area is None:
            reflection = await self._reflect_on_responses()
        elif area == "memory_recall":
            reflection = await self._reflect_on_memory()
        elif area == "tool_usage":
            reflection = await self._reflect_on_tool_usage()
        else:
            reflection = Reflection(
                id=f"reflect-{area}-{datetime.now().isoformat()}",
                area=area,
                score=0.5,
                issues=[f"No reflection handler for area: {area}"],
                improvements=["Add dedicated reflection handler for this area"],
            )

        self._reflections.append(reflection)
        logger.debug("Completed reflection on %s: score=%.2f", reflection.area, reflection.score)
        return reflection

    async def get_recent_reflections(self, limit: int = 10) -> List[Reflection]:
        """Get the most recent reflections.

        Args:
            limit: Maximum number of reflections to return

        Returns:
            Most recent reflections, newest first
        """
        return list(reversed(self._reflections[-limit:]))

    async def get_area_trend(self, area: str) -> Dict[str, Any]:
        """Compute trend statistics for a specific reflection area.

        Args:
            area: The reflection area to analyse

        Returns:
            Dict with 'count', 'avg_score', 'min_score', 'max_score', 'trend'
            (positive = improving, negative = declining).
        """
        area_reflections = [r for r in self._reflections if r.area == area]
        if not area_reflections:
            return {"count": 0, "avg_score": None, "min_score": None, "max_score": None, "trend": 0.0}

        scores = [r.score for r in area_reflections]
        trend = 0.0
        if len(scores) >= 2:
            trend = scores[-1] - scores[0]

        return {
            "count": len(scores),
            "avg_score": sum(scores) / len(scores),
            "min_score": min(scores),
            "max_score": max(scores),
            "trend": trend,
        }

    async def _reflect_on_responses(self) -> Reflection:
        """Reflect on response quality using available metrics."""
        score = 0.75
        issues: List[str] = []
        improvements: List[str] = []

        try:
            if hasattr(self.metrics, "get_recent_stats"):
                stats = await self.metrics.get_recent_stats()
                if isinstance(stats, dict):
                    score = float(stats.get("response_quality", 0.75))
        except Exception:
            pass

        score = max(0.0, min(1.0, score))

        if score < _CRITICAL_THRESHOLD:
            issues.extend([
                "Response quality significantly below threshold",
                "High error rate detected in recent responses",
            ])
            improvements.extend([
                "Review response generation strategy immediately",
                "Add output validation layer",
            ])
        elif score < _WARN_THRESHOLD:
            issues.append("Response quality below acceptable level")
            improvements.append("Add brevity scoring to response evaluation")
        elif score < _GOOD_THRESHOLD:
            issues.append("Could be more concise in some responses")
            improvements.append("Implement response length optimization")
        else:
            improvements.append("Maintain current quality standards")

        return Reflection(
            id=f"reflect-responses-{datetime.now().isoformat()}",
            area="response_quality",
            score=score,
            issues=issues,
            improvements=improvements,
        )

    async def _reflect_on_memory(self) -> Reflection:
        """Reflect on memory recall accuracy."""
        score = 0.8
        issues: List[str] = []
        improvements: List[str] = []

        try:
            if hasattr(self.metrics, "get_memory_stats"):
                stats = await self.metrics.get_memory_stats()
                if isinstance(stats, dict):
                    score = float(stats.get("recall_accuracy", 0.8))
        except Exception:
            pass

        score = max(0.0, min(1.0, score))

        if score < _CRITICAL_THRESHOLD:
            issues.extend([
                "Memory recall accuracy critically low",
                "Significant number of false positives in recall",
            ])
            improvements.extend([
                "Rebuild memory index",
                "Increase memory index refresh frequency",
            ])
        elif score < _WARN_THRESHOLD:
            issues.append("Some stale memories surfaced in recent recalls")
            improvements.append("Increase recency weighting in recall scoring")
        elif score < _GOOD_THRESHOLD:
            issues.append("Occasional irrelevant memories retrieved")
            improvements.append("Tune vector similarity threshold for recall")
        else:
            improvements.append("Consider expanding memory retention window")

        return Reflection(
            id=f"reflect-memory-{datetime.now().isoformat()}",
            area="memory_recall",
            score=score,
            issues=issues,
            improvements=improvements,
        )

    async def _reflect_on_tool_usage(self) -> Reflection:
        """Reflect on tool usage effectiveness."""
        score = 0.7
        issues: List[str] = []
        improvements: List[str] = []

        try:
            if hasattr(self.metrics, "get_tool_stats"):
                stats = await self.metrics.get_tool_stats()
                if isinstance(stats, dict):
                    score = float(stats.get("tool_success_rate", 0.7))
        except Exception:
            pass

        score = max(0.0, min(1.0, score))

        if score < _CRITICAL_THRESHOLD:
            issues.extend([
                "Significant tool failures detected",
                "Wrong tools selected for task types",
            ])
            improvements.extend([
                "Review tool selection criteria",
                "Consult ToolLearner preferences before every tool call",
            ])
        elif score < _WARN_THRESHOLD:
            issues.append("Some tools used for wrong task types")
            improvements.append("Cross-reference tool learner preferences before selection")
        elif score < _GOOD_THRESHOLD:
            issues.append("Occasional suboptimal tool choices")
            improvements.append("Expand tool preference learning dataset")
        else:
            improvements.append("Tool usage patterns are effective; continue monitoring")

        return Reflection(
            id=f"reflect-tools-{datetime.now().isoformat()}",
            area="tool_usage",
            score=score,
            issues=issues,
            improvements=improvements,
        )
