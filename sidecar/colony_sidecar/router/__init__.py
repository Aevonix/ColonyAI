"""Colony LLM Cost Router — intelligent model tier selection.

Route LLM requests to the cheapest capable model based on prompt complexity.
Expected cost savings: 30–40% vs always using the largest model.

Usage::

    from colony_sidecar.router import LLMRouter, ModelTier

    router = LLMRouter()
    response = await router.complete(messages, context={"task": "summarise"})
"""

from colony_sidecar.router.tiers import ModelTier, TierConfig, DEFAULT_TIERS
from colony_sidecar.router.complexity_scorer import ComplexityScorer, ComplexitySignals
from colony_sidecar.router.router import LLMRouter, LLMResponse
from colony_sidecar.router.self_learning import RouterSelfLearner
from colony_sidecar.router.fallback import FallbackHandler

__all__ = [
    "LLMRouter",
    "LLMResponse",
    "ModelTier",
    "TierConfig",
    "DEFAULT_TIERS",
    "ComplexityScorer",
    "ComplexitySignals",
    "RouterSelfLearner",
    "FallbackHandler",
]
