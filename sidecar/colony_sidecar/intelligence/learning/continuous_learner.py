"""ContinuousLearner — fuse live signals into intelligence component weights.

Unlike MetaLearner (which operates on historical windows), this component
processes signals in near-real-time and updates component weights without
waiting for a training window to close.

Signal sources:
- User corrections (explicit: "that's wrong", "don't do that again")
- Implicit feedback (task re-done immediately → low quality)
- Engagement signals (briefing sections read vs skipped)
- Goal success/failure outcomes

Weight update rule: exponential moving average with per-signal-type learning
rates. Weights are clamped to [0.1, 2.0] so no component is permanently
silenced or runs unrestrained.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------

@dataclass
class BriefingEngagement:
    """Engagement data from a delivered briefing."""

    briefing_id: str
    section: str          # e.g. "goals", "relationships", "insights"
    read: bool            # True = read, False = skipped
    dwell_seconds: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GoalOutcome:
    """Outcome of a goal managed by Colony."""

    goal_id: str
    success: bool
    component: str        # Which component's output influenced this goal
    confidence_at_time: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Learning rates by signal type
# ---------------------------------------------------------------------------

_LEARNING_RATES: Dict[str, float] = {
    # correction_type → lr (how much a single correction moves the weight)
    "factual": 0.10,
    "tone": 0.05,
    "action": 0.08,
    "preference": 0.06,
    # engagement
    "engagement_read": 0.02,
    "engagement_skip": 0.03,
    # outcomes
    "outcome_success": 0.04,
    "outcome_failure": 0.06,
}

_DEFAULT_WEIGHT = 1.0
_WEIGHT_MIN = 0.1
_WEIGHT_MAX = 2.0


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ContinuousLearner:
    """Fuse live signals into intelligence component weights.

    Weights are maintained in-memory and optionally persisted to a graph
    if a ``graph_client`` is provided.

    Args:
        graph_client: Optional ColonyGraph instance for weight persistence.
        initial_weights: Optional seed weights (component_name → float).
    """

    def __init__(
        self,
        graph_client: Optional[object] = None,
        initial_weights: Optional[Dict[str, float]] = None,
    ) -> None:
        self._graph = graph_client
        # Component weights: maps component name to a multiplier [0.1, 2.0]
        self._weights: Dict[str, float] = dict(initial_weights or {})
        self._correction_count = 0
        self._engagement_count = 0
        self._outcome_count = 0

    # ------------------------------------------------------------------
    # Signal ingestion
    # ------------------------------------------------------------------

    async def ingest_correction(self, correction: "UserCorrection") -> None:  # noqa: F821
        """Update component weights based on an explicit user correction.

        The component inferred from correction_type has its weight reduced
        (we were wrong); complementary components may be boosted slightly.
        """
        lr = _LEARNING_RATES.get(correction.correction_type, 0.05)
        component = self._correction_type_to_component(correction.correction_type)

        # Penalise the component that produced the wrong output
        self._adjust_weight(component, -lr)

        # Slight boost to components that weren't responsible
        if correction.correction_type == "factual":
            self._adjust_weight("retrieval", -lr * 0.5)
        elif correction.correction_type == "action":
            self._adjust_weight("task_planner", -lr * 0.5)

        self._correction_count += 1
        logger.debug(
            "ingest_correction: type=%s component=%s lr=%.3f new_weight=%.3f",
            correction.correction_type,
            component,
            lr,
            self._weights.get(component, _DEFAULT_WEIGHT),
        )

        await self._maybe_persist_weights()

    async def ingest_engagement(self, engagement: BriefingEngagement) -> None:
        """Update weights from briefing section engagement signal."""
        signal_type = "engagement_read" if engagement.read else "engagement_skip"
        lr = _LEARNING_RATES[signal_type]
        component = self._section_to_component(engagement.section)

        # Read → boost, skip → penalise
        delta = lr if engagement.read else -lr
        self._adjust_weight(component, delta)

        self._engagement_count += 1
        logger.debug(
            "ingest_engagement: section=%s read=%s component=%s delta=%.3f",
            engagement.section,
            engagement.read,
            component,
            delta,
        )

        await self._maybe_persist_weights()

    async def ingest_outcome(self, outcome: GoalOutcome) -> None:
        """Update weights from goal success/failure."""
        lr = _LEARNING_RATES["outcome_success" if outcome.success else "outcome_failure"]
        # Scale by confidence: high-confidence failures are penalised more
        effective_lr = lr * outcome.confidence_at_time
        delta = effective_lr if outcome.success else -effective_lr
        self._adjust_weight(outcome.component, delta)

        self._outcome_count += 1
        logger.debug(
            "ingest_outcome: goal=%s success=%s component=%s delta=%.3f",
            outcome.goal_id,
            outcome.success,
            outcome.component,
            delta,
        )

        await self._maybe_persist_weights()

    # ------------------------------------------------------------------
    # Weight access
    # ------------------------------------------------------------------

    async def get_component_weights(self) -> Dict[str, float]:
        """Return a snapshot of current component weights."""
        return dict(self._weights)

    def get_weight(self, component: str) -> float:
        """Return current weight for *component* (1.0 if not yet set)."""
        return self._weights.get(component, _DEFAULT_WEIGHT)

    def set_weight(self, component: str, value: float) -> None:
        """Manually set a component weight (e.g., MetaLearner override)."""
        self._weights[component] = max(_WEIGHT_MIN, min(_WEIGHT_MAX, value))

    @property
    def stats(self) -> Dict[str, int]:
        """Return ingestion counters."""
        return {
            "corrections": self._correction_count,
            "engagements": self._engagement_count,
            "outcomes": self._outcome_count,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _adjust_weight(self, component: str, delta: float) -> None:
        """Apply *delta* to *component* weight, clamping to [min, max]."""
        current = self._weights.get(component, _DEFAULT_WEIGHT)
        updated = max(_WEIGHT_MIN, min(_WEIGHT_MAX, current + delta))
        self._weights[component] = updated

    async def _maybe_persist_weights(self) -> None:
        """Persist current weights to the graph if a client is available."""
        if self._graph is None:
            return
        try:
            weights_json = str(self._weights)  # simple repr; real impl uses JSON
            if hasattr(self._graph, "execute"):
                await self._graph.execute(
                    "MERGE (w:LearnerWeights {id: 'continuous_learner'}) "
                    "SET w.weights = $weights, w.updated_at = datetime()",
                    weights=weights_json,
                )
        except Exception as exc:
            logger.debug("Weight persistence skipped: %s", exc)

    @staticmethod
    def _correction_type_to_component(correction_type: str) -> str:
        mapping = {
            "factual": "knowledge",
            "tone": "response_quality",
            "action": "task_planner",
            "preference": "preference_learner",
        }
        return mapping.get(correction_type, "general")

    @staticmethod
    def _section_to_component(section: str) -> str:
        mapping = {
            "goals": "goal_engine",
            "relationships": "contact_style_adapter",
            "insights": "research_orchestrator",
            "anomalies": "anomaly_detector",
            "predictions": "mind_predictor",
        }
        return mapping.get(section, "briefing")
