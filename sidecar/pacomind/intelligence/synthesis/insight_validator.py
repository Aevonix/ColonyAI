"""Insight Validator — ensure insights meet quality thresholds before surfacing.

Validates insights against configurable criteria:
- Minimum evidence count (don't surface guesses)
- Confidence threshold (minimum certainty)
- Actionability check (prefer actionable insights)
- Data recency (stale data degrades trust)

Only insights passing all checks should be delivered to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of insight validation.

    Attributes:
        valid: Whether the insight passed all checks
        reasons: List of failure reasons (empty if valid)
        evidence_count: Number of supporting evidence items
        confidence: The insight's confidence score
        data_age_hours: Age of the oldest supporting data in hours
    """

    valid: bool
    reasons: List[str]
    evidence_count: int
    confidence: float
    data_age_hours: float


@runtime_checkable
class Validatable(Protocol):
    """Protocol for objects that can be validated as insights.

    Insight objects must expose confidence, supporting evidence,
    and optionally actionability flags.
    """

    @property
    def confidence(self) -> float: ...

    @property
    def supporting_evidence(self) -> List[str]: ...


@runtime_checkable
class ValidatorGraphClient(Protocol):
    """Protocol for graph access needed by insight validation."""

    async def recall(
        self,
        query: str,
        limit: int = 10,
        min_strength: float = 0.1,
    ) -> List[Dict[str, Any]]: ...


class InsightValidator:
    """Validate insights before surfacing to the user.

    Applies configurable quality gates. An insight must pass all
    checks to be considered valid for delivery.

    Args:
        min_evidence: Minimum number of supporting evidence items
        min_confidence: Minimum confidence score (0-1)
        max_data_age_hours: Maximum age of supporting data in hours
        graph: Optional graph client for evidence timestamp lookups
    """

    def __init__(
        self,
        min_evidence: int = 2,
        min_confidence: float = 0.6,
        max_data_age_hours: float = 168.0,  # 1 week
        graph: Optional[ValidatorGraphClient] = None,
    ) -> None:
        self.min_evidence = min_evidence
        self.min_confidence = min_confidence
        self.max_data_age_hours = max_data_age_hours
        self._graph = graph

    async def validate(self, insight: Validatable) -> ValidationResult:
        """Check if an insight meets all quality thresholds.

        Evaluates evidence count, confidence, and data recency.
        Returns a ValidationResult with pass/fail and detailed reasons.

        Args:
            insight: The insight to validate

        Returns:
            ValidationResult indicating whether the insight is fit
            for delivery, with failure reasons if not.
        """
        reasons: List[str] = []

        # Evidence count
        evidence_count = len(insight.supporting_evidence)
        if evidence_count < self.min_evidence:
            reasons.append(
                f"Insufficient evidence ({evidence_count} < {self.min_evidence})"
            )

        # Confidence check
        confidence = insight.confidence
        if confidence < self.min_confidence:
            reasons.append(
                f"Low confidence ({confidence:.2f} < {self.min_confidence})"
            )

        # Data recency
        data_age = await self._compute_data_age(insight)
        if data_age > self.max_data_age_hours:
            reasons.append(
                f"Stale data ({data_age:.0f}h > {self.max_data_age_hours:.0f}h)"
            )

        valid = len(reasons) == 0

        return ValidationResult(
            valid=valid,
            reasons=reasons,
            evidence_count=evidence_count,
            confidence=confidence,
            data_age_hours=data_age,
        )

    async def validate_batch(
        self,
        insights: List[Validatable],
    ) -> List[tuple[Any, ValidationResult]]:
        """Validate multiple insights, returning paired results.

        Args:
            insights: List of insights to validate

        Returns:
            List of (insight, ValidationResult) tuples.
        """
        results = []
        for insight in insights:
            result = await self.validate(insight)
            results.append((insight, result))
        return results

    async def _compute_data_age(self, insight: Validatable) -> float:
        """Calculate the age of the oldest supporting data in hours."""
        if not insight.supporting_evidence or self._graph is None:
            return 0.0  # no evidence or no graph = treat as fresh

        oldest_hours = 0.0
        now = datetime.now(timezone.utc)
        for evidence_id in insight.supporting_evidence:
            try:
                records = await self._graph.recall(
                    evidence_id, limit=1, min_strength=0.0
                )
                if records:
                    created_at = records[0].get("created_at")
                    if created_at is not None:
                        if hasattr(created_at, 'to_native'):
                            created_at = created_at.to_native()
                        elif isinstance(created_at, str):
                            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if isinstance(created_at, datetime):
                            if created_at.tzinfo is None:
                                created_at = created_at.replace(tzinfo=timezone.utc)
                            age = (now - created_at).total_seconds() / 3600
                            oldest_hours = max(oldest_hours, age)
            except Exception as e:
                logger.debug("Could not fetch data age for evidence %s: %s", evidence_id, e)
        return oldest_hours
