"""MetaLearner - orchestrates cognitive performance tracking and self-improvement."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, List, Optional, Dict, Any, TYPE_CHECKING
from datetime import datetime
import time

from .types import GapSeverity

if TYPE_CHECKING:
    from colony_sidecar.vector.embedder import EmbeddingPipeline
    from colony_sidecar.vector.store import VectorStore


@dataclass
class CycleResult:
    """Result of a complete cognition cycle."""
    cpi: Optional["CognitivePerformanceIndex"]
    gaps: List["Gap"]
    adjustments: List["Adjustment"]
    duration_ms: float
    errors: List[str]

    @property
    def has_critical_gaps(self) -> bool:
        return any(g.severity == GapSeverity.CRITICAL for g in self.gaps) if self.gaps else False


@dataclass
class MetaLearnerConfig:
    """Configuration for MetaLearner behavior."""
    metrics_lookback_days: int = 7
    min_cpi_threshold: float = 50.0
    max_adjustments_per_cycle: int = 3
    persist_to_graph: bool = True


logger = __import__('logging').getLogger(__name__)


class MetaLearner:
    """Orchestrates cognitive performance tracking and self-improvement."""

    def __init__(self, graph: "ColonyGraph", config: Optional[MetaLearnerConfig] = None):
        self.graph = graph
        self.config = config or MetaLearnerConfig()

        # Sub-components (loaded lazily)
        self._metrics_collector = None
        self._performance_index = None
        self._gap_detector = None
        self._strategy_adjuster = None

        # Optional FeedbackStore for consuming user corrections
        self._feedback_store: Optional[Any] = None

        # Vector store + embedder for cognition pattern matching (§6.2)
        self._vector_store: Optional["VectorStore"] = None
        self._embedder: Optional["EmbeddingPipeline"] = None

        # Hooks for external metric recording
        self._metric_hooks: List[Callable[..., None]] = []

        # Counter incremented on each persistence failure for observability
        self._persist_errors: int = 0

        # Dedup / throttle state
        self._last_persisted_cpi: Optional[float] = None  # overall score of last persisted CPI
        self._cycle_count: int = 0  # total run_cycle() calls; persist summary every 10th
        self._open_gap_types: set = set()  # gap_type values with an open Gap node in graph

    @property
    def is_fully_wired(self) -> bool:
        """Check if all sub-components are available."""
        return all([
            self._performance_index is not None,
            self._gap_detector is not None,
            self._strategy_adjuster is not None,
        ])

    @property
    def available_components(self) -> List[str]:
        """List which sub-components are available."""
        components = []
        if self._metrics_collector:
            components.append("metrics_collector")
        if self._performance_index:
            components.append("performance_index")
        if self._gap_detector:
            components.append("gap_detector")
        if self._strategy_adjuster:
            components.append("strategy_adjuster")
        return components

    # Component setters for dependency injection
    def set_metrics_collector(self, collector: "MetricsCollector") -> None:
        self._metrics_collector = collector

    def set_performance_index(self, computer: "PerformanceIndexComputer") -> None:
        self._performance_index = computer

    def set_gap_detector(self, detector: "GapDetector") -> None:
        self._gap_detector = detector

    def set_strategy_adjuster(self, adjuster: "StrategyAdjuster") -> None:
        self._strategy_adjuster = adjuster

    def set_feedback_store(self, store: Any) -> None:
        """Wire a FeedbackStore so MetaLearner consumes user corrections each cycle."""
        self._feedback_store = store

    def set_vector_store(self, store: "VectorStore", embedder: "EmbeddingPipeline") -> None:
        """Wire vector store + embedder for cognition pattern matching."""
        self._vector_store = store
        self._embedder = embedder

    def register_metric_hook(self, hook: Callable[..., None]) -> None:
        """Register a callback to receive metrics."""
        self._metric_hooks.append(hook)

    async def record_metric(self, metric_type: str, value: float, domain: str = "general") -> None:
        """Record a metric value."""
        if self._metrics_collector:
            await self._metrics_collector.record(metric_type, value, domain)

        # Also notify hooks
        for hook in self._metric_hooks:
            try:
                hook(metric_type, value, domain)
            except Exception as exc:
                logger.warning(
                    "MetaLearner: metric hook %r raised (hook will be skipped): %s",
                    getattr(hook, "__name__", hook),
                    exc,
                )

    async def evaluate(self) -> "CognitivePerformanceIndex":
        """Compute current Cognitive Performance Index."""
        if not self._performance_index:
            raise RuntimeError("PerformanceIndexComputer not wired")

        cpi = await self._performance_index.compute(self._metrics_collector)

        # Persist to graph
        if self.config.persist_to_graph:
            await self._persist_cpi(cpi)

        return cpi

    async def detect_gaps(self, cpi: Optional["CognitivePerformanceIndex"] = None) -> List["Gap"]:
        """Detect performance gaps from CPI."""
        if not self._gap_detector:
            raise RuntimeError("GapDetector not wired")

        if cpi is None:
            cpi = await self.evaluate()

        gaps = self._gap_detector.detect(cpi)

        # Sort by severity
        severity_order = {GapSeverity.CRITICAL: 0, GapSeverity.WARNING: 1, GapSeverity.INFO: 2}
        gaps.sort(key=lambda g: severity_order.get(g.severity, 99))

        # Persist gaps to graph
        if self.config.persist_to_graph:
            for gap in gaps:
                await self._persist_gap(gap)

        return gaps

    async def adjust_strategy(self, gap: "Gap") -> "Adjustment":
        """Generate and apply strategy adjustment for a gap."""
        if not self._strategy_adjuster:
            raise RuntimeError("StrategyAdjuster not wired")

        adjustment = await self._strategy_adjuster.generate(gap)

        # Apply the adjustment
        await self._strategy_adjuster.apply(adjustment)

        # Persist to graph — pass the source gap for relationship creation
        if self.config.persist_to_graph:
            await self._persist_adjustment(adjustment, gap)

        return adjustment

    async def run_cycle(self) -> CycleResult:
        """Execute full cognition cycle: evaluate → detect → adjust."""
        self._cycle_count += 1
        start_time = time.time()
        cpi = None
        gaps = []
        adjustments = []
        errors = []

        try:
            # 1. Evaluate current performance
            cpi = await self.evaluate()
        except Exception as e:
            msg = f"Evaluation failed: {e}"
            logger.warning("MetaLearner.run_cycle: %s", msg)
            errors.append(msg)

        try:
            # 2. Detect gaps
            if cpi:
                gaps = await self.detect_gaps(cpi)
        except Exception as e:
            msg = f"Gap detection failed: {e}"
            logger.warning("MetaLearner.run_cycle: %s", msg)
            errors.append(msg)

        try:
            # 2b. Process user corrections from FeedbackStore
            if self._feedback_store is not None:
                await self._process_feedback_corrections()
        except Exception as e:
            msg = f"Feedback processing failed: {e}"
            logger.warning("MetaLearner.run_cycle: %s", msg)
            errors.append(msg)

        try:
            # 3. Adjust strategy for each gap (up to max)
            if gaps and self._strategy_adjuster:
                for gap in gaps[:self.config.max_adjustments_per_cycle]:
                    try:
                        adjustment = await self.adjust_strategy(gap)
                        adjustments.append(adjustment)
                    except Exception as e:
                        msg = f"Adjustment failed for {gap.gap_type}: {e}"
                        logger.warning("MetaLearner.run_cycle: %s", msg)
                        errors.append(msg)
        except Exception as e:
            msg = f"Strategy adjustment phase failed: {e}"
            logger.warning("MetaLearner.run_cycle: %s", msg)
            errors.append(msg)

        # Persist cycle summary
        if self.config.persist_to_graph:
            await self._persist_cycle_summary(cpi, gaps, adjustments, errors)

        # Store cycle in vector store for pattern matching (§6.2)
        await self._store_cognition_vector(cpi, gaps, adjustments)

        duration_ms = (time.time() - start_time) * 1000
        return CycleResult(
            cpi=cpi,
            gaps=gaps,
            adjustments=adjustments,
            duration_ms=duration_ms,
            errors=errors,
        )

    async def _process_feedback_corrections(self) -> None:
        """Pull unapplied corrections from FeedbackStore and record as metrics."""
        if self._feedback_store is None:
            return
        corrections = self._feedback_store.get_unapplied(limit=50)
        if not corrections:
            return
        metric_map = {
            "factual": ("factual_error_rate", 1.0),
            "tone": ("tone_error_rate", 1.0),
            "action": ("action_error_rate", 1.0),
            "preference": ("preference_mismatch_rate", 1.0),
        }
        applied_ids = []
        for correction in corrections:
            metric_type, value = metric_map.get(
                correction.correction_type, ("correction_rate", 1.0)
            )
            await self.record_metric(metric_type, value, domain="feedback")
            applied_ids.append(correction.correction_id)
        self._feedback_store.mark_applied(applied_ids)

    async def _persist_cpi(self, cpi: "CognitivePerformanceIndex") -> None:
        """Persist CPI components as CognitiveMetric nodes (MERGE, only on >1.0 change)."""
        overall = getattr(cpi, "overall", 0.0)
        # Skip if change from last persisted value is ≤1.0 (avoids 53K identical nodes)
        if self._last_persisted_cpi is not None and abs(overall - self._last_persisted_cpi) <= 1.0:
            return
        self._last_persisted_cpi = overall
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                today = datetime.now().strftime("%Y-%m-%d")
                for component_name in ["retrieval", "prediction", "goal_progress", "tool_efficiency", "initiative", "response_quality"]:
                    component = getattr(cpi, component_name, None)
                    if component:
                        await session.run("""
                            MERGE (m:CognitiveMetric {metric_type: $component_name, day: $day})
                            SET m.value = $score,
                                m.domain = 'cpi',
                                m.recorded_at = datetime()
                        """, component_name=component_name, day=today, score=component.score)
        except Exception as exc:
            self._persist_errors += 1
            logger.warning("MetaLearner: failed to persist CPI to graph: %s", exc)

    async def _persist_gap(self, gap: "Gap") -> None:
        """Persist gap to graph (MERGE on gap_type+status='open', skip if already open)."""
        gap_type_val = gap.gap_type.value if hasattr(gap.gap_type, "value") else str(gap.gap_type)
        # Skip if we already have an open node for this gap type in this session
        if gap_type_val in self._open_gap_types:
            return
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                # Check for existing open gap of same type before creating
                result = await session.run(
                    "MATCH (g:Gap {gap_type: $gap_type, status: 'open'}) RETURN count(g) AS cnt",
                    gap_type=gap_type_val,
                )
                record = await result.single()
                if record and record["cnt"] > 0:
                    self._open_gap_types.add(gap_type_val)
                    return
                await session.run("""
                    MERGE (g:Gap {gap_type: $gap_type, status: 'open'})
                    ON CREATE SET
                        g.id = randomUUID(),
                        g.severity = $severity,
                        g.component = $component,
                        g.description = $description,
                        g.diagnosed_at = datetime()
                    ON MATCH SET
                        g.severity = $severity,
                        g.updated_at = datetime()
                """, gap_type=gap_type_val, severity=gap.severity.value,
                     component=gap.component, description=gap.description)
                self._open_gap_types.add(gap_type_val)
        except Exception as exc:
            self._persist_errors += 1
            logger.warning(
                "MetaLearner: failed to persist gap %r to graph: %s",
                gap_type_val, exc,
            )

    async def _persist_adjustment(self, adjustment: "Adjustment", gap: Optional["Gap"] = None) -> None:
        """Persist adjustment to graph and link to its source gap."""
        gap_type_val = None
        if gap is not None:
            gap_type_val = gap.gap_type.value if hasattr(gap.gap_type, "value") else str(gap.gap_type)
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                if gap_type_val:
                    # Only create adjustment + link if the gap has no resolver yet
                    await session.run("""
                        MATCH (g:Gap {gap_type: $gap_type, status: 'open'})
                        WHERE NOT exists((g)-[:RESOLVED_BY]->(:Adjustment))
                        CREATE (a:Adjustment {
                            id: randomUUID(),
                            adjustment_type: $adj_type,
                            hypothesis: $hypothesis,
                            status: 'applied',
                            applied_at: datetime()
                        })
                        CREATE (g)-[:RESOLVED_BY]->(a)
                    """, gap_type=gap_type_val, adj_type=adjustment.adjustment_type,
                         hypothesis=adjustment.hypothesis)
                else:
                    await session.run("""
                        CREATE (a:Adjustment {
                            id: randomUUID(),
                            adjustment_type: $adj_type,
                            hypothesis: $hypothesis,
                            status: 'applied',
                            applied_at: datetime()
                        })
                    """, adj_type=adjustment.adjustment_type, hypothesis=adjustment.hypothesis)
        except Exception as exc:
            self._persist_errors += 1
            logger.warning(
                "MetaLearner: failed to persist adjustment %r to graph: %s",
                adjustment.adjustment_type, exc,
            )

    async def _persist_cycle_summary(
        self,
        cpi: Optional["CognitivePerformanceIndex"],
        gaps: List["Gap"],
        adjustments: List["Adjustment"],
        errors: List[str]
    ) -> None:
        """Persist cycle summary every 10th cycle to reduce graph churn."""
        if self._cycle_count % 10 != 0:
            return
        try:
            overall_score = cpi.overall if cpi else 0.0
            gap_types = [
                g.gap_type.value if hasattr(g.gap_type, "value") else str(g.gap_type)
                for g in gaps
            ]
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    CREATE (c:CognitionCycle {
                        id: randomUUID(),
                        cycle_number: $cycle_num,
                        overall_cpi: $overall,
                        gap_count: $gap_count,
                        adjustment_count: $adj_count,
                        error_count: $error_count,
                        completed_at: datetime()
                    })
                    RETURN c.id AS cycle_id
                """, cycle_num=self._cycle_count, overall=overall_score,
                     gap_count=len(gaps), adj_count=len(adjustments),
                     error_count=len(errors))
                record = await result.single()
                cycle_id = record["cycle_id"] if record else None

                # Link to detected gaps
                if cycle_id and gap_types:
                    for gap_type_val in gap_types:
                        await session.run("""
                            MATCH (c:CognitionCycle {id: $cycle_id})
                            MATCH (g:Gap {gap_type: $gap_type})
                            MERGE (c)-[:DETECTED]->(g)
                        """, cycle_id=cycle_id, gap_type=gap_type_val)
        except Exception as exc:
            self._persist_errors += 1
            logger.warning("MetaLearner: failed to persist cycle summary to graph: %s", exc)

    async def _cleanup_old_metrics(self) -> None:
        """Delete CognitiveMetric nodes older than 7 days (TTL cleanup)."""
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    MATCH (m:CognitiveMetric)
                    WHERE m.recorded_at < datetime() - duration({days: 7})
                    WITH m LIMIT 1000
                    DETACH DELETE m
                    RETURN count(m) AS deleted
                """)
                record = await result.single()
                deleted = record["deleted"] if record else 0
                if deleted:
                    logger.info("MetaLearner TTL: deleted %d old CognitiveMetric nodes", deleted)
        except Exception as exc:
            logger.debug("MetaLearner TTL cleanup error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Cognition pattern matching via vector store (§6.2)
    # ------------------------------------------------------------------

    def _build_cycle_summary(
        self,
        cpi: Optional["CognitivePerformanceIndex"],
        gaps: List["Gap"],
        adjustments: List["Adjustment"],
    ) -> str:
        """Generate a text summary of a cognition cycle for embedding."""
        parts = []
        if cpi:
            parts.append(f"CPI overall={getattr(cpi, 'overall', 0.0):.1f}")
        if gaps:
            gap_strs = [
                f"{g.gap_type.value if hasattr(g.gap_type, 'value') else g.gap_type}({g.severity.value})"
                for g in gaps
            ]
            parts.append(f"Gaps: {', '.join(gap_strs)}")
        if adjustments:
            adj_strs = [a.adjustment_type for a in adjustments]
            parts.append(f"Adjustments: {', '.join(adj_strs)}")
        return "; ".join(parts) if parts else "Empty cognition cycle"

    async def _store_cognition_vector(
        self,
        cpi: Optional["CognitivePerformanceIndex"],
        gaps: List["Gap"],
        adjustments: List["Adjustment"],
    ) -> None:
        """Embed and store a cognition cycle summary in the COGNITION collection."""
        if self._vector_store is None or self._embedder is None:
            return
        try:
            from colony_sidecar.vector.collections import Collection
            import uuid

            summary = self._build_cycle_summary(cpi, gaps, adjustments)
            vector = await self._embedder.embed(summary)
            cycle_id = str(uuid.uuid4())

            await self._vector_store.add(
                collection=Collection.COGNITION,
                id=cycle_id,
                text=summary,
                vector=vector,
                metadata={
                    "cycle_id": cycle_id,
                    "cpi_score": getattr(cpi, "overall", 0.0) if cpi else 0.0,
                    "gaps": [
                        g.gap_type.value if hasattr(g.gap_type, "value") else str(g.gap_type)
                        for g in gaps
                    ],
                    "adjustments": [a.adjustment_type for a in adjustments],
                    "cycle_at": time.time(),
                },
            )
        except Exception as exc:
            logger.warning("MetaLearner: failed to store cognition vector: %s", exc)

    async def find_similar_cycles(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Query the COGNITION collection for similar past cycles.

        Called at the start of a new cycle to provide pattern context to
        StrategyAdjuster.  Returns empty list if vector store is not
        configured.
        """
        if self._vector_store is None or self._embedder is None:
            return []
        try:
            from colony_sidecar.vector.collections import Collection

            # Use the most recent cycle summary as the query
            count = await self._vector_store.count(Collection.COGNITION)
            if count == 0:
                return []

            # Build a query from current state (lightweight — just embed a status string)
            query_text = "recent cognition cycle performance gaps and adjustments"
            query_vector = await self._embedder.embed(query_text)
            results = await self._vector_store.search(
                collection=Collection.COGNITION,
                query_vector=query_vector,
                limit=limit,
            )
            return [
                {"text": r.text, "score": r.score, "metadata": r.metadata}
                for r in results
            ]
        except Exception as exc:
            logger.warning("MetaLearner: failed to query similar cycles: %s", exc)
            return []
