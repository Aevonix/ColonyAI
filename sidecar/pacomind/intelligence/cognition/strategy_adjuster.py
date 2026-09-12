"""Strategy adjustment for cognitive gaps."""
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum

from pacomind.self_model.benchmark import cognition_p4_mode

logger = logging.getLogger(__name__)


class AdjustmentStatus(str, Enum):
    """Status of an adjustment."""
    PROPOSED = "proposed"
    APPLIED = "applied"
    FAILED = "failed"
    REVERTED = "reverted"


@dataclass
class Adjustment:
    """Strategy adjustment for a gap."""
    adjustment_type: str
    hypothesis: str
    target_gap: "Gap"
    actions: list  # List of action dicts
    expected_impact: float  # Expected CPI improvement
    status: AdjustmentStatus = AdjustmentStatus.PROPOSED
    applied_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None


class StrategyAdjuster:
    """Generate experiment proposals from legacy CPI gaps.

    This component is retained as a detector adapter. It deliberately never
    writes an adaptive parameter or graph policy; ExperimentEngine is the one
    controlled writer and requires exposure/outcome evidence.
    """

    # Gap type to adjustment strategies mapping
    STRATEGIES = {
        "low_memory_quality": {
            "hypothesis": "Memory retrieval quality is low due to weak embeddings",
            "actions": [
                {"type": "reindex_memories", "params": {}},
                # Tighten the consolidator so near-miss pairs stop merging
                # (weak embeddings inflate similarity; merging makes recall
                # quality worse). Bounded by the param store [0.85, 0.98].
                {"type": "adjust_consolidation_threshold", "params": {"threshold": 0.95}},
            ],
            "expected_impact": 10.0,
        },
        "semantic_mismatch": {
            "hypothesis": "Queries not matching stored memories semantically",
            "actions": [
                # Raise the recall relevance floor so low-score noise stops
                # outranking real matches. Bounded by the param store [0, 0.5].
                {"type": "adjust_similarity_threshold", "params": {"threshold": 0.35}},
                {"type": "expand_query_terms", "params": {}},
            ],
            "expected_impact": 8.0,
        },
        "insufficient_data": {
            "hypothesis": "Not enough data to make accurate predictions",
            "actions": [
                {"type": "increase_observation_window", "params": {"days": 14}},
                {"type": "prompt_user_for_data", "params": {}},
            ],
            "expected_impact": 5.0,
        },
        "stale_data": {
            "hypothesis": "Data is too old to be relevant",
            "actions": [
                {"type": "refresh_data_source", "params": {}},
                {"type": "decay_old_signals", "params": {"factor": 0.5}},
            ],
            "expected_impact": 7.0,
        },
        "missing_preference": {
            "hypothesis": "User preference not captured",
            "actions": [
                {"type": "ask_preference", "params": {}},
                {"type": "infer_from_behavior", "params": {}},
            ],
            "expected_impact": 6.0,
        },
        "low_prediction_accuracy": {
            "hypothesis": "Prediction model not calibrated to user patterns",
            "actions": [
                {"type": "recalibrate_baselines", "params": {}},
                {"type": "adjust_confidence_threshold", "params": {"threshold": 0.6}},
            ],
            "expected_impact": 12.0,
        },
        "tool_inefficiency": {
            "hypothesis": "Tools being used suboptimally",
            "actions": [
                {"type": "audit_tool_usage", "params": {}},
                {"type": "optimize_tool_selection", "params": {}},
            ],
            "expected_impact": 8.0,
        },
        "initiative_mismatch": {
            "hypothesis": "Proactive suggestions not matching user needs",
            "actions": [
                {"type": "adjust_suggestion_frequency", "params": {"factor": 0.7}},
                {"type": "refine_suggestion_criteria", "params": {}},
            ],
            "expected_impact": 6.0,
        },
    }

    def __init__(self, graph: "PacoMindGraph", params: Any = None):
        self.graph = graph
        # params remains a constructor compatibility argument. Only the
        # ExperimentEngine owns parameter writes; this detector proposes.
        self._experiment_proposer: Any = None
        self._applied_adjustments: list = []

    def set_experiment_proposer(self, proposer: Any) -> None:
        """Wire ExperimentEngine after boot without creating another writer."""

        self._experiment_proposer = proposer

    async def generate(self, gap: "Gap") -> Adjustment:
        """Generate adjustment strategy for a gap."""
        gap_type_str = gap.gap_type.value if hasattr(gap.gap_type, "value") else str(gap.gap_type)
        strategy = self.STRATEGIES.get(gap_type_str, self._default_strategy())

        adjustment = Adjustment(
            adjustment_type=gap_type_str,
            hypothesis=strategy["hypothesis"],
            target_gap=gap,
            actions=strategy["actions"],
            expected_impact=strategy["expected_impact"],
        )

        return adjustment

    async def apply(self, adjustment: Adjustment) -> bool:
        """Record typed proposals without executing legacy adjustment actions."""
        results = []
        for action in adjustment.actions:
            result = {
                "success": False,
                "action": action.get("type"),
                "params": dict(action.get("params") or {}),
                "proposal_required": True,
                "reason": "legacy CPI detectors may propose; ExperimentEngine writes",
            }
            spec = self._experiment_spec(adjustment, action)
            if spec is not None:
                result["experiment_proposal"] = spec
                if (self._experiment_proposer is not None
                        and cognition_p4_mode() in {"shadow", "live"}):
                    try:
                        saved = self._experiment_proposer.propose(**spec)
                        result["proposal_id"] = saved.get("id")
                    except ValueError as exc:
                        # An already-open proposal is the expected dedupe path.
                        result["proposal_error"] = str(exc)
            results.append(result)
        adjustment.result = {
            "actions_taken": len(results),
            "successful": 0,
            "proposals": len(results),
            "details": results,
        }
        adjustment.status = AdjustmentStatus.PROPOSED
        logger.info(
            "legacy cognition gap %s emitted %d controlled-learning proposal(s)",
            adjustment.adjustment_type, len(results))
        return False

    @staticmethod
    def _experiment_spec(adjustment: Adjustment,
                         action: dict) -> Optional[Dict[str, Any]]:
        """Map the two real legacy knobs into typed, non-started proposals."""

        action_type = action.get("type")
        raw = dict(action.get("params") or {})
        if action_type == "adjust_similarity_threshold":
            ref = "recall.min_relevance"
        elif action_type == "adjust_consolidation_threshold":
            ref = "consolidation.similarity_threshold"
        else:
            return None
        if "threshold" not in raw:
            return None
        return {
            "hypothesis": adjustment.hypothesis,
            "ref": ref,
            "variant": float(raw["threshold"]),
            "metric": "recall.fact_coverage",
            "metric_version": "v2",
            "assignment_mode": "cohort",
            "max_regression": 0.05,
            "window_days": 7,
            "source": f"legacy-cpi-gap:{adjustment.adjustment_type}",
        }


    def _default_strategy(self) -> dict:
        """Default strategy for unknown gap types."""
        return {
            "hypothesis": "Performance gap detected, root cause unclear",
            "actions": [
                {"type": "log_gap", "params": {}},
                {"type": "monitor", "params": {"duration_hours": 24}},
            ],
            "expected_impact": 3.0,
        }

    def get_recent_adjustments(self, hours: int = 24) -> list:
        """Get recently applied adjustments."""
        cutoff = datetime.now()
        return [
            adj for adj in self._applied_adjustments
            if adj.applied_at and (cutoff - adj.applied_at).total_seconds() / 3600 < hours
        ]
