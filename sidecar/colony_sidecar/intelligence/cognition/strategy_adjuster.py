"""Strategy adjustment for cognitive gaps."""
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum

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
    """Generate and apply strategy adjustments for performance gaps."""

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
        self._applied_adjustments: list = []

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
        """Apply an adjustment and track result."""
        results = []

        for action in adjustment.actions:
            result = await self._execute_action(action)
            results.append(result)

        adjustment.result = {
            "actions_taken": len(results),
            "successful": sum(1 for r in results if r.get("success", False)),
            "details": results,
        }

        if adjustment.result["successful"] > 0:
            adjustment.status = AdjustmentStatus.APPLIED
            adjustment.applied_at = datetime.now()
            self._applied_adjustments.append(adjustment)
            if adjustment.adjustment_type == "low_memory_quality":
                logger.info(
                    "low_memory_quality adjustment applied: memory consolidation should run "
                    "to improve retrieval quality"
                )
            elif adjustment.adjustment_type == "initiative_mismatch":
                logger.info(
                    "initiative_mismatch adjustment applied: suggestion frequency/threshold "
                    "adjusted to better match user needs"
                )
            return True
        else:
            adjustment.status = AdjustmentStatus.FAILED
            return False

    async def _execute_action(self, action: dict) -> dict:
        """Execute a single adjustment action."""
        action_type = action.get("type")
        params = action.get("params", {})

        # Map action types to implementations
        try:
            if action_type == "reindex_memories":
                # Vector index is now managed by LanceDB — reindex is a no-op
                logger.info("reindex_memories is a no-op: vector index managed by LanceDB")
                return {"success": True, "action": "reindex_memories", "note": "managed by LanceDB"}
            elif action_type == "adjust_similarity_threshold":
                return await self._adjust_threshold(**params)
            elif action_type == "adjust_consolidation_threshold":
                return await self._adjust_consolidation_threshold(**params)
            elif action_type == "decay_old_signals":
                return await self._decay_signals(**params)
            elif action_type == "recalibrate_baselines":
                return await self._recalibrate_baselines(**params)
            else:
                return {"success": False, "action": action_type, "note": "unknown action type"}
        except (TypeError, RuntimeError, AttributeError, NotImplementedError) as e:
            return {"success": False, "action": action_type, "error": str(e)}

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
        if self._params is None:
            logger.warning("No AdaptiveParamStore wired; %s not applied", action)
            return {"success": False, "action": action,
                    "error": "adaptive param store not wired"}
        try:
            applied = self._params.set(name, float(value), reason=reason,
                                       source="strategy_adjuster")
            if applied is None:
                return {"success": False, "action": action,
                        "error": f"param {name} not registered"}
            return {"success": True, "action": action, "param": name,
                    "requested": float(value), "applied": applied}
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Failed %s: %s", action, e)
            return {"success": False, "action": action, "error": str(e)}

    async def _decay_signals(self, factor: float) -> dict:
        """Apply decay to old signals."""
        try:
            await self.graph.decay_memories(half_life_days=7.0 / factor)
            return {"success": True, "action": "decay_signals", "factor": factor}
        except (OSError, RuntimeError, AttributeError) as e:
            return {"success": False, "error": str(e)}

    async def _recalibrate_baselines(self) -> dict:
        """Recalculate baseline signal values for all persons over the last 30 days."""
        if not hasattr(self.graph, 'run_query'):
            logger.warning("Graph client does not support run_query; baselines not recalibrated")
            return {"success": False, "action": "recalibrate_baselines", "error": "run_query not available"}
        try:
            records = await self.graph.run_query(
                """MATCH (p:Person)-[:EXHIBITED]->(s:Signal)
                   WHERE s.timestamp >= datetime() - duration({days: 30})
                   WITH p.id AS pid, s.signal_type AS stype, avg(s.normalized_value) AS baseline_val
                   MERGE (b:Baseline {person_id: pid, signal_type: stype})
                   SET b.value = baseline_val, b.updated_at = datetime()
                   RETURN count(b) AS updated_baselines""",
                {}
            )
            count = records[0].get("updated_baselines", 0) if records else 0
            logger.info("Recalibrated %d baselines", count)
            return {"success": True, "action": "recalibrate_baselines", "updated_baselines": int(count)}
        except (OSError, RuntimeError) as e:
            logger.error("Failed to recalibrate baselines: %s", e)
            return {"success": False, "action": "recalibrate_baselines", "error": str(e)}

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
