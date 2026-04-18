"""Gap detection for cognitive performance."""
from dataclasses import dataclass
from typing import Dict, Any
from enum import Enum
from datetime import datetime

from .types import GapSeverity  # noqa: F401 — re-exported for backward compat


class GapType(str, Enum):
    """Types of cognitive performance gaps."""
    LOW_MEMORY_QUALITY = "low_memory_quality"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    INSUFFICIENT_DATA = "insufficient_data"
    STALE_DATA = "stale_data"
    MISSING_PREFERENCE = "missing_preference"
    LOW_PREDICTION_ACCURACY = "low_prediction_accuracy"
    TOOL_INEFFICIENCY = "tool_inefficiency"
    INITIATIVE_MISMATCH = "initiative_mismatch"


@dataclass
class Gap:
    """Detected performance gap."""
    gap_type: GapType
    severity: GapSeverity
    description: str
    component: str  # Which CPI component affected
    evidence: Dict[str, Any]
    diagnosed_at: datetime = None

    def __post_init__(self):
        if self.diagnosed_at is None:
            self.diagnosed_at = datetime.now()


class GapDetector:
    """Detect performance gaps from CPI components."""

    # Thresholds for gap detection
    THRESHOLDS = {
        "retrieval": 70,
        "prediction": 60,
        "tool_efficiency": 75,
        "initiative": 50,
        "goal_progress": 50,
        "response_quality": 65,
    }

    # Component to gap type mapping
    GAP_MAPPING = {
        "retrieval": {
            "low": GapType.LOW_MEMORY_QUALITY,
            "mismatch": GapType.SEMANTIC_MISMATCH,
            "stale": GapType.STALE_DATA,
        },
        "prediction": {
            "low": GapType.LOW_PREDICTION_ACCURACY,
            "insufficient": GapType.INSUFFICIENT_DATA,
        },
        "tool_efficiency": {
            "low": GapType.TOOL_INEFFICIENCY,
        },
        "initiative": {
            "low": GapType.INITIATIVE_MISMATCH,
        },
        "goal_progress": {
            "insufficient": GapType.INSUFFICIENT_DATA,
        },
        "response_quality": {
            "missing": GapType.MISSING_PREFERENCE,
        },
    }

    def detect(self, cpi: "CognitivePerformanceIndex") -> list:
        """Detect gaps from CPI snapshot."""
        gaps = []

        # Check each component against threshold
        for component_name in ["retrieval", "prediction", "tool_efficiency", "initiative", "goal_progress", "response_quality"]:
            component = getattr(cpi, component_name, None)
            if not component:
                continue

            threshold = self.THRESHOLDS.get(component_name, 60)

            if component.score < threshold:
                severity = self._determine_severity(component.score, threshold)
                gap_type = self._map_gap_type(component_name, component)

                gaps.append(Gap(
                    gap_type=gap_type,
                    severity=severity,
                    description=f"{component_name.replace('_', ' ').title()} performance below threshold ({component.score:.0f} < {threshold})",
                    component=component_name,
                    evidence={
                        "score": component.score,
                        "threshold": threshold,
                        "trend": component.trend,
                        "metrics": component.metrics,
                    },
                ))

            # Check for declining trends
            if component.trend == "declining":
                gaps.append(Gap(
                    gap_type=GapType.SEMANTIC_MISMATCH,
                    severity=GapSeverity.INFO,
                    description=f"{component_name.replace('_', ' ').title()} performance declining",
                    component=component_name,
                    evidence={"trend": "declining", "score": component.score},
                ))

        return gaps

    def _determine_severity(self, score: float, threshold: float) -> GapSeverity:
        """Determine gap severity from score distance to threshold."""
        gap = threshold - score

        if gap > 30:
            return GapSeverity.CRITICAL
        elif gap > 15:
            return GapSeverity.WARNING
        else:
            return GapSeverity.INFO

    def _map_gap_type(self, component: str, component_data: "CPIComponent") -> GapType:
        """Map component to specific gap type."""
        mapping = self.GAP_MAPPING.get(component, {})

        # Check metrics for specific signals
        metrics = component_data.metrics

        if "relevance_score" in metrics and metrics["relevance_score"] < 60:
            return mapping.get("mismatch", GapType.LOW_MEMORY_QUALITY)

        if component_data.trend == "declining":
            return mapping.get("mismatch", GapType.SEMANTIC_MISMATCH)

        if "total" in metrics and metrics.get("total", 0) < 5:
            return mapping.get("insufficient", GapType.INSUFFICIENT_DATA)

        return mapping.get("low", GapType.LOW_MEMORY_QUALITY)

    def prioritize(self, gaps: list) -> list:
        """Sort gaps by severity for action prioritization."""
        severity_order = {GapSeverity.CRITICAL: 0, GapSeverity.WARNING: 1, GapSeverity.INFO: 2}
        return sorted(gaps, key=lambda g: severity_order.get(g.severity, 99))
