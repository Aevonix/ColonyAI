"""Cognitive Performance Index (CPI) computation."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime


@dataclass
class CPIComponent:
    """Score for a single CPI dimension."""
    name: str
    score: float  # 0-100
    trend: str = "stable"  # "improving", "declining", "stable"
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class CognitivePerformanceIndex:
    """6-component cognitive performance index."""
    retrieval: CPIComponent
    prediction: CPIComponent
    goal_progress: CPIComponent
    tool_efficiency: CPIComponent
    initiative: CPIComponent
    response_quality: CPIComponent
    overall: float
    computed_at: datetime


class PerformanceIndexComputer:
    """Compute 6-component Cognitive Performance Index."""

    COMPONENT_WEIGHTS = {
        "retrieval": 0.20,
        "prediction": 0.15,
        "goal_progress": 0.20,
        "tool_efficiency": 0.15,
        "initiative": 0.15,
        "response_quality": 0.15,
    }

    # Thresholds for trend detection
    IMPROVEMENT_THRESHOLD = 5.0  # 5-point improvement
    DECLINE_THRESHOLD = -5.0

    def __init__(self, graph: "ColonyGraph"):
        self.graph = graph
        self._prior_scores: dict[str, list[float]] = {}  # component → recent scores

    async def compute(self, metrics_collector: Optional["MetricsCollector"] = None) -> CognitivePerformanceIndex:
        """Compute CPI from recent metrics."""
        components = {}

        # 1. Retrieval: memory quality, relevance, latency
        retrieval_metrics = await self._get_metrics(metrics_collector, "retrieval", days=7)
        components["retrieval"] = self._compute_retrieval(retrieval_metrics)

        # 2. Prediction: accuracy, confidence calibration
        pred_metrics = await self._get_metrics(metrics_collector, "prediction", days=7)
        components["prediction"] = self._compute_prediction(pred_metrics)

        # 3. Goal Progress: task completion velocity
        goal_metrics = await self._get_metrics(metrics_collector, "goal_progress", days=7)
        components["goal_progress"] = self._compute_goal_progress(goal_metrics)

        # 4. Tool Efficiency: success rate, latency
        tool_metrics = await self._get_metrics(metrics_collector, "tool_efficiency", days=7)
        components["tool_efficiency"] = self._compute_tool_efficiency(tool_metrics)

        # 5. Initiative: proactive suggestion hit rate
        init_metrics = await self._get_metrics(metrics_collector, "initiative", days=7)
        components["initiative"] = self._compute_initiative(init_metrics)

        # 6. Response Quality: relevance, conciseness, actionability
        quality_metrics = await self._get_metrics(metrics_collector, "response_quality", days=7)
        components["response_quality"] = self._compute_response_quality(quality_metrics)

        # Compute overall weighted score
        overall = sum(
            components[name].score * weight
            for name, weight in self.COMPONENT_WEIGHTS.items()
        )

        return CognitivePerformanceIndex(
            retrieval=components["retrieval"],
            prediction=components["prediction"],
            goal_progress=components["goal_progress"],
            tool_efficiency=components["tool_efficiency"],
            initiative=components["initiative"],
            response_quality=components["response_quality"],
            overall=overall,
            computed_at=datetime.now(),
        )

    async def _get_metrics(
        self,
        collector: Optional["MetricsCollector"],
        domain: str,
        days: int = 7
    ) -> Dict[str, float]:
        """Fetch metrics for a domain."""
        if collector:
            try:
                return await collector.get_metrics(domain, days=days)
            except Exception:
                pass

        # Fallback: query graph directly
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    MATCH (m:CognitiveMetric)
                    WHERE m.domain = $domain
                    AND m.recorded_at >= datetime() - duration({days: $days})
                    RETURN m.metric_type AS type, m.value AS value
                """, domain=domain, days=days)

                metrics = {}
                async for record in result:
                    metrics[record["type"]] = record["value"]
                return metrics
        except Exception:
            return {}

    def _compute_retrieval(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute retrieval component score."""
        # Base score from metrics
        relevance = metrics.get("relevance_score", 70.0)
        latency = metrics.get("latency_ms", 500)

        # Latency penalty (cap at 2s)
        latency_score = max(0, 100 - (latency / 20))

        score = (relevance * 0.7 + latency_score * 0.3)
        trend = self._compute_trend("retrieval", score)

        return CPIComponent(
            name="retrieval",
            score=score,
            trend=trend,
            metrics=metrics,
        )

    def _compute_prediction(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute prediction component score."""
        correct = metrics.get("correct_count", 0)
        total = metrics.get("total_predictions", 1)

        accuracy = (correct / max(total, 1)) * 100
        trend = self._compute_trend("prediction", accuracy)

        return CPIComponent(
            name="prediction",
            score=accuracy,
            trend=trend,
            metrics={"accuracy": accuracy, "total": total},
        )

    def _compute_goal_progress(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute goal progress component score."""
        completed = metrics.get("completed_tasks", 0)
        total = metrics.get("total_tasks", 1)
        velocity = metrics.get("velocity", 1.0)  # tasks/day

        completion_rate = (completed / max(total, 1)) * 100
        velocity_score = min(velocity * 20, 100)  # 5 tasks/day = 100

        score = completion_rate * 0.6 + velocity_score * 0.4
        trend = self._compute_trend("goal_progress", score)

        return CPIComponent(
            name="goal_progress",
            score=score,
            trend=trend,
            metrics=metrics,
        )

    def _compute_tool_efficiency(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute tool efficiency component score."""
        success_rate = metrics.get("success_rate", 80.0)
        avg_latency = metrics.get("avg_latency_ms", 1000)

        latency_score = max(0, 100 - (avg_latency / 50))
        score = success_rate * 0.7 + latency_score * 0.3
        trend = self._compute_trend("tool_efficiency", score)

        return CPIComponent(
            name="tool_efficiency",
            score=score,
            trend=trend,
            metrics=metrics,
        )

    def _compute_initiative(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute initiative component score."""
        suggestions = metrics.get("suggestions_made", 0)
        accepted = metrics.get("suggestions_accepted", 0)

        if suggestions > 0:
            hit_rate = (accepted / suggestions) * 100
        else:
            hit_rate = 50.0  # Neutral if no data

        volume_score = min(suggestions * 10, 100)  # 10 suggestions = 100

        score = hit_rate * 0.7 + volume_score * 0.3
        trend = self._compute_trend("initiative", score)

        return CPIComponent(
            name="initiative",
            score=score,
            trend=trend,
            metrics=metrics,
        )

    def _compute_response_quality(self, metrics: Dict[str, float]) -> CPIComponent:
        """Compute response quality component score."""
        relevance = metrics.get("relevance_score", 80.0)
        conciseness = metrics.get("conciseness_score", 75.0)
        actionability = metrics.get("actionability_score", 70.0)

        score = relevance * 0.4 + conciseness * 0.3 + actionability * 0.3
        trend = self._compute_trend("response_quality", score)

        return CPIComponent(
            name="response_quality",
            score=score,
            trend=trend,
            metrics=metrics,
        )

    def _compute_trend(self, component: str, current_score: float) -> str:
        """Determine trend by comparing to the rolling average of prior scores.

        Uses an in-session cache of up to 5 recent scores per component.
        On first call the cache is empty, so "stable" is returned.

        Returns:
            "improving"  — current score is ≥ IMPROVEMENT_THRESHOLD above prior avg
            "declining"  — current score is ≤ DECLINE_THRESHOLD below prior avg
            "stable"     — within threshold range, or no prior data
        """
        prior = self._prior_scores.get(component, [])

        if not prior:
            # No prior data — record current score and report stable
            self._prior_scores[component] = [current_score]
            return "stable"

        prior_avg = sum(prior) / len(prior)
        delta = current_score - prior_avg

        if delta >= self.IMPROVEMENT_THRESHOLD:
            trend = "improving"
        elif delta <= self.DECLINE_THRESHOLD:
            trend = "declining"
        else:
            trend = "stable"

        # Keep a rolling window of up to 5 prior scores
        self._prior_scores[component] = ([current_score] + prior)[:5]
        return trend
