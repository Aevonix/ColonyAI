"""Strategy adjustment for cognitive gaps."""
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum

from colony_sidecar.self_model.benchmark import cognition_p4_mode

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

    def __init__(self, graph: "ColonyGraph", params: Any = None):
        self.graph = graph
        # AdaptiveParamStore: the read-back path for tuning adjustments.
        # Without it, threshold adjustments have nowhere consumers look and
        # are refused rather than written into the void.
        self._params = params
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

    async def _execute_action(self, action: dict) -> dict:
        """Compatibility hook: convert every legacy action into a proposal."""
        action_type = action.get("type")
        params = action.get("params", {})
        return {
            "success": False,
            "action": action_type,
            "params": params,
            "proposal_required": True,
            "writer": "ExperimentEngine",
        }

    async def _adjust_threshold(self, threshold: float) -> dict:
        """Raise/lower the recall relevance floor via the AdaptiveParamStore.

        This replaces a legacy write to a graph Config node that no consumer
        ever read back. ColonyGraph.recall reads recall.min_relevance at
        query time, so the adjustment takes effect immediately; the store
        clamps to [0, 0.5] and journals the change (domain meta_learning).
        """
        return self._set_param(
            "recall.min_relevance", threshold, action="adjust_threshold",
            reason="semantic_mismatch gap: raise recall relevance floor")

    async def _adjust_consolidation_threshold(self, threshold: float) -> dict:
        """Adjust the MemoryConsolidator merge threshold (read per run)."""
        return self._set_param(
            "consolidation.similarity_threshold", threshold,
            action="adjust_consolidation_threshold",
            reason="low_memory_quality gap: tune duplicate-merge threshold")

    def _set_param(self, name: str, value: float, *, action: str,
                   reason: str) -> dict:
        return {
            "success": False,
            "action": action,
            "param": name,
            "requested": float(value),
            "hypothesis": reason,
            "proposal_required": True,
            "writer": "ExperimentEngine",
        }

    async def _decay_signals(self, factor: float) -> dict:
        """RETIRED — this action never decayed signals.

        Despite its name it called graph.decay_memories(half_life_days=7/factor),
        silently compressing the half-life of EVERY memory whenever a
        'stale_data' gap fired — a second, hidden writer racing the autonomy
        loop's memory_decay phase. Memory decay has exactly one writer (the
        loop phase, tuned via COLONY_DECAY_HALF_LIFE_DAYS); this action now
        refuses and never touches the graph.
        """
        return {
            "success": False,
            "action": "decay_signals",
            "error": ("retired: decayed memories, not signals; memory decay "
                      "is owned solely by the autonomy loop's memory_decay "
                      "phase"),
        }

    async def _recalibrate_baselines(self) -> dict:
        """Retired writer: calibration changes must be controlled experiments."""
        return {
            "success": False,
            "action": "recalibrate_baselines",
            "proposal_required": True,
            "writer": "ExperimentEngine",
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
