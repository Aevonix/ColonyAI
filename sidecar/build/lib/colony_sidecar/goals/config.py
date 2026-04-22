"""Configuration for the Colony Goal Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GoalEngineConfig:
    """Configuration for the Colony Goal Engine.

    Attributes:
        enabled:                Whether the goal engine is active.
        auto_accept_threshold:  Confidence threshold for auto-accepting inferred goals (0.0–1.0).
        max_replans:            Maximum replan attempts per goal before escalating to user.
        max_subtasks:           Maximum subtasks per goal DAG.
        max_depth:              Maximum DAG depth.
        max_blocked_hours:      Hours before a blocked goal is escalated to the user.
        inference_enabled:      Whether to run goal inference on conversation messages.
        inference_lm_threshold: Confidence below which LLM inference pass is triggered.
        proposal_min_interval:  Minimum seconds between goal proposals to avoid noise.
        retention_days:         Days to retain completed/abandoned goals.
        telemetry_enabled:      Whether to emit goal telemetry to the MetaLearner.
        db_path:                Path to SQLite database (defaults to in-memory for tests).
    """
    enabled: bool = True
    auto_accept_threshold: float = 0.85
    max_replans: int = 5
    max_subtasks: int = 50
    max_depth: int = 5
    max_blocked_hours: float = 24.0
    inference_enabled: bool = True
    inference_lm_threshold: float = 0.70
    proposal_min_interval: float = 300.0   # 5 minutes
    retention_days: int = 90
    telemetry_enabled: bool = True
    db_path: Optional[str] = None          # None → in-memory SQLite
