"""Cognition package - MetaLearner and cognitive performance tracking."""
from .metalearner import MetaLearner, MetaLearnerConfig, CycleResult
from .performance_index import CognitivePerformanceIndex, CPIComponent, PerformanceIndexComputer
from .gap_detector import GapType, GapSeverity, Gap, GapDetector
from .strategy_adjuster import AdjustmentStatus, Adjustment, StrategyAdjuster
from .metrics_collector import MetricsCollector, MetricObservation

__all__ = [
    "MetaLearner",
    "MetaLearnerConfig",
    "CycleResult",
    "CognitivePerformanceIndex",
    "CPIComponent",
    "PerformanceIndexComputer",
    "GapType",
    "GapSeverity",
    "Gap",
    "GapDetector",
    "AdjustmentStatus",
    "Adjustment",
    "StrategyAdjuster",
    "MetricsCollector",
    "MetricObservation",
]
