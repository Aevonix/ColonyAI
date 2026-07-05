"""CognitionPipeline — auto-wiring container for the cognition pipeline.

Instantiates and connects MetricsCollector, PerformanceIndexComputer,
GapDetector, StrategyAdjuster, and MetaLearner into a single callable
pipeline.  Called once at Colony startup; ``run_tick()`` is driven by the
autonomy loop on every event-processing cycle.

Usage::

    pipeline = CognitionPipeline(graph=graph_client)
    # In the autonomy loop tick:
    await pipeline.run_tick()
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from .gap_detector import GapDetector, GapSeverity
from .metalearner import MetaLearner, MetaLearnerConfig, CycleResult
from .metrics_collector import MetricsCollector
from .performance_index import PerformanceIndexComputer
from .strategy_adjuster import StrategyAdjuster

logger = logging.getLogger(__name__)


class CognitionPipeline:
    """Auto-wiring container for the five cognition modules.

    Connects MetricsCollector → PerformanceIndexComputer → GapDetector →
    StrategyAdjuster → MetaLearner and exposes a single ``run_tick()``
    coroutine that drives one full evaluate-detect-adjust cycle.

    Args:
        graph: Colony graph client (passed through to sub-components).
        event_bus: Optional event bus; if supplied, pipeline subscribes to
            ``goal.completed``, ``task.completed``, and ``anomaly.detected``
            to record metrics in real time.
        config: Optional MetaLearnerConfig to customise cycle behaviour.
    """

    def __init__(
        self,
        graph: Any,
        event_bus: Optional[Any] = None,
        config: Optional[MetaLearnerConfig] = None,
        params: Optional[Any] = None,
    ) -> None:
        self.graph = graph
        self.event_bus = event_bus

        # Instantiate sub-components
        self.metrics = MetricsCollector(graph)
        self.performance_index = PerformanceIndexComputer(graph)
        self.gap_detector = GapDetector()
        self.strategy_adjuster = StrategyAdjuster(graph, params=params)
        self.meta_learner = MetaLearner(graph, config=config or MetaLearnerConfig())

        # Wire sub-components into MetaLearner via dependency-injection setters
        self.meta_learner.set_metrics_collector(self.metrics)
        self.meta_learner.set_performance_index(self.performance_index)
        self.meta_learner.set_gap_detector(self.gap_detector)
        self.meta_learner.set_strategy_adjuster(self.strategy_adjuster)

        # Subscribe to event-bus events if a bus was provided
        if event_bus is not None:
            self._subscribe(event_bus)

        logger.info(
            "CognitionPipeline initialised. Components: %s",
            self.meta_learner.available_components,
        )

    # ── event-bus subscriptions ──────────────────────────────────────

    def _subscribe(self, event_bus: Any) -> None:
        """Register event handlers on *event_bus*.

        Supports any bus that exposes ``subscribe(event_name, callback)``.
        Errors during subscription are logged but not raised so that the
        pipeline degrades gracefully when the bus API differs.
        """
        handlers: List[tuple[str, Callable]] = [
            ("goal.completed", self._on_goal_completed),
            ("task.completed", self._on_task_completed),
            ("anomaly.detected", self._on_anomaly),
        ]
        for event_name, handler in handlers:
            try:
                event_bus.subscribe(event_name, handler)
                logger.debug("CognitionPipeline subscribed to %r", event_name)
            except Exception as exc:
                logger.warning(
                    "CognitionPipeline: could not subscribe to %r: %s", event_name, exc
                )

    def _on_goal_completed(self, event: Any) -> None:
        """Record a goal-completed metric (synchronous hook)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self.metrics.record("goal_completed", 1.0, domain="goal_progress")
                )
        except Exception as exc:
            logger.debug("CognitionPipeline._on_goal_completed error: %s", exc)

    def _on_task_completed(self, event: Any) -> None:
        """Record a task-completed metric (synchronous hook)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            task_type = getattr(event, "task_type", None) or (
                event.get("task_type") if isinstance(event, dict) else "unknown"
            )
            duration = getattr(event, "duration_minutes", None) or (
                event.get("duration_minutes") if isinstance(event, dict) else 0.0
            )
            if loop.is_running():
                loop.create_task(
                    self.metrics.record_task_completion(
                        str(task_type), float(duration or 0.0)
                    )
                )
        except Exception as exc:
            logger.debug("CognitionPipeline._on_task_completed error: %s", exc)

    def _on_anomaly(self, event: Any) -> None:
        """Record an anomaly signal (synchronous hook)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self.metrics.record("anomaly_detected", 1.0, domain="general")
                )
        except Exception as exc:
            logger.debug("CognitionPipeline._on_anomaly error: %s", exc)

    # ── main tick ────────────────────────────────────────────────────

    async def run_tick(self) -> CycleResult:
        """Execute one full cognition cycle.

        Called once per autonomy-loop tick.  Runs:
        1. Performance evaluation (compute CPI)
        2. Gap detection
        3. Strategy adjustment (up to ``config.max_adjustments_per_cycle``)

        Returns:
            ``CycleResult`` with CPI, detected gaps, and applied adjustments.
            Errors within sub-steps are captured in ``result.errors`` rather
            than propagated so the autonomy loop is never blocked.
        """
        result = await self.meta_learner.run_cycle()

        if result.errors:
            logger.warning(
                "CognitionPipeline.run_tick completed with %d error(s): %s",
                len(result.errors),
                "; ".join(result.errors[:3]),
            )
        else:
            overall = result.cpi.overall if result.cpi else 0.0
            gap_count = len(result.gaps)
            logger.debug(
                "CognitionPipeline.run_tick: CPI=%.1f, gaps=%d, adjustments=%d",
                overall,
                gap_count,
                len(result.adjustments),
            )

        return result

    # ── convenience accessors ────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        """True if all five sub-components are wired."""
        return self.meta_learner.is_fully_wired

    async def record_metric(
        self,
        metric_type: str,
        value: float,
        domain: str = "general",
    ) -> None:
        """Convenience proxy: record a metric directly into the pipeline."""
        await self.metrics.record(metric_type, value, domain=domain)
