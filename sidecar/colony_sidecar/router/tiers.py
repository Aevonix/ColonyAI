"""Model tier definitions for the LLM cost router."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModelTier(Enum):
    HEURISTIC = "heuristic"  # No LLM call — rule-based response
    SMALL = "small"          # Fast/cheap: haiku-class, gpt-4o-mini
    MEDIUM = "medium"        # Balanced: sonnet-class, gpt-4o
    LARGE = "large"          # Best quality: opus-class, o3


@dataclass
class TierConfig:
    tier: ModelTier
    model_id: str              # LiteLLM model string
    max_tokens: int
    cost_per_1k_input: float   # USD
    cost_per_1k_output: float
    latency_p50_ms: int        # approximate


# Default tier configurations — overridable via config.yaml
DEFAULT_TIERS: dict[ModelTier, TierConfig] = {
    ModelTier.SMALL: TierConfig(
        tier=ModelTier.SMALL,
        model_id="anthropic/claude-haiku-4-5-20251001",
        max_tokens=4096,
        cost_per_1k_input=0.0008,
        cost_per_1k_output=0.004,
        latency_p50_ms=800,
    ),
    ModelTier.MEDIUM: TierConfig(
        tier=ModelTier.MEDIUM,
        model_id="anthropic/claude-sonnet-4-6",
        max_tokens=8192,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        latency_p50_ms=2000,
    ),
    ModelTier.LARGE: TierConfig(
        tier=ModelTier.LARGE,
        model_id="anthropic/claude-opus-4-6",
        max_tokens=32768,
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        latency_p50_ms=5000,
    ),
}
