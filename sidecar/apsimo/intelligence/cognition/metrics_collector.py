"""Metrics collection for cognitive performance tracking."""
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import statistics


@dataclass
class MetricObservation:
    """Single metric observation."""
    metric_type: str
    value: float
    domain: str
    recorded_at: datetime
    context: Optional[dict] = None


class MetricsCollector:
    """Collect and aggregate metrics for CPI computation."""

    def __init__(self, graph: "ColonyGraph"):
        self.graph = graph
        self._buffer: List[MetricObservation] = []

    async def record(
        self,
        metric_type: str,
        value: float,
        domain: str = "general",
        context: Optional[dict] = None
    ) -> None:
        """Record a metric observation."""
        observation = MetricObservation(
            metric_type=metric_type,
            value=value,
            domain=domain,
            recorded_at=datetime.now(),
            context=context,
        )

        # Add to buffer
        self._buffer.append(observation)

        # Persist to graph
        await self._persist(observation)

    async def get_metrics(
        self,
        domain: str,
        days: int = 7
    ) -> Dict[str, float]:
        """Get aggregated metrics for a domain."""
        cutoff = datetime.now() - timedelta(days=days)

        # Filter buffer
        relevant = [
            obs for obs in self._buffer
            if obs.domain == domain and obs.recorded_at >= cutoff
        ]

        # Also fetch from graph
        try:
            graph_metrics = await self._fetch_from_graph(domain, days)
        except (OSError, RuntimeError):
            graph_metrics = []

        # Combine
        all_observations = relevant + graph_metrics

        # Aggregate by type
        by_type: Dict[str, List[float]] = {}
        for obs in all_observations:
            if obs.metric_type not in by_type:
                by_type[obs.metric_type] = []
            by_type[obs.metric_type].append(obs.value)

        # Compute aggregates
        result = {}
        for metric_type, values in by_type.items():
            if values:
                result[metric_type] = statistics.mean(values)
                result[f"{metric_type}_count"] = len(values)
                if len(values) > 1:
                    result[f"{metric_type}_std"] = statistics.stdev(values)

        return result

    async def record_prediction(self, correct: bool, confidence: float) -> None:
        """Record prediction outcome."""
        await self.record("prediction_outcome", 1.0 if correct else 0.0, "prediction")
        await self.record("prediction_confidence", confidence, "prediction")

    async def record_tool_usage(
        self,
        tool_name: str,
        success: bool,
        latency_ms: float
    ) -> None:
        """Record tool usage metrics."""
        await self.record(f"tool_{tool_name}_success", 1.0 if success else 0.0, "tool_efficiency")
        await self.record(f"tool_{tool_name}_latency", latency_ms, "tool_efficiency")

    async def record_memory_retrieval(
        self,
        relevance_score: float,
        latency_ms: float
    ) -> None:
        """Record memory retrieval metrics."""
        await self.record("relevance_score", relevance_score, "retrieval")
        await self.record("retrieval_latency", latency_ms, "retrieval")

    async def record_task_completion(
        self,
        task_type: str,
        duration_minutes: float
    ) -> None:
        """Record task completion metrics."""
        await self.record(f"task_{task_type}_duration", duration_minutes, "goal_progress")
        await self.record(f"task_{task_type}_completed", 1.0, "goal_progress")

    async def record_suggestion(
        self,
        suggestion_type: str,
        accepted: bool
    ) -> None:
        """Record proactive suggestion outcome."""
        await self.record(f"suggestion_{suggestion_type}", 1.0 if accepted else 0.0, "initiative")

    async def record_response_quality(
        self,
        relevance: float,
        conciseness: float,
        actionability: float
    ) -> None:
        """Record response quality metrics."""
        await self.record("relevance_score", relevance, "response_quality")
        await self.record("conciseness_score", conciseness, "response_quality")
        await self.record("actionability_score", actionability, "response_quality")

    async def _persist(self, observation: MetricObservation) -> None:
        """Persist observation to Neo4j."""
        try:
            person_id = (observation.context or {}).get("person_id")
            async with self.graph.driver.session(database=self.graph.database) as session:
                if person_id:
                    await session.run("""
                        CREATE (m:CognitiveMetric {
                            id: randomUUID(),
                            metric_type: $metric_type,
                            value: $value,
                            domain: $domain,
                            recorded_at: datetime($timestamp),
                            context: $context
                        })
                        WITH m
                        MERGE (p:Person {id: $person_id})
                        CREATE (m)-[:OBSERVED_FOR]->(p)
                    """,
                        metric_type=observation.metric_type,
                        value=observation.value,
                        domain=observation.domain,
                        timestamp=observation.recorded_at.isoformat(),
                        context=observation.context or {},
                        person_id=person_id,
                    )
                else:
                    await session.run("""
                        CREATE (m:CognitiveMetric {
                            id: randomUUID(),
                            metric_type: $metric_type,
                            value: $value,
                            domain: $domain,
                            recorded_at: datetime($timestamp),
                            context: $context
                        })
                    """,
                        metric_type=observation.metric_type,
                        value=observation.value,
                        domain=observation.domain,
                        timestamp=observation.recorded_at.isoformat(),
                        context=observation.context or {},
                    )
        except (OSError, RuntimeError):
            pass  # Don't fail if persistence fails

    async def _fetch_from_graph(
        self,
        domain: str,
        days: int
    ) -> List[MetricObservation]:
        """Fetch metrics from Neo4j."""
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    MATCH (m:CognitiveMetric)
                    WHERE m.domain = $domain
                    AND m.recorded_at >= datetime() - duration({days: $days})
                    RETURN m.metric_type AS type, m.value AS value, m.recorded_at AS ts
                """, domain=domain, days=days)

                observations = []
                async for record in result:
                    observations.append(MetricObservation(
                        metric_type=record["type"],
                        value=record["value"],
                        domain=domain,
                        recorded_at=record["ts"],
                    ))
                return observations
        except (OSError, RuntimeError):
            return []

    def flush_buffer(self) -> int:
        """Clear in-memory buffer, return count of flushed items."""
        count = len(self._buffer)
        self._buffer.clear()
        return count
