"""PacoMind LLM Cost Router — intelligent model tier selection.

Route LLM requests to the cheapest capable model based on prompt complexity.
Expected cost savings: 30–40% vs always using the largest model.

Usage::

    from pacomind.router import LLMRouter, ModelTier

    router = LLMRouter()
    response = await router.complete(messages, context={"task": "summarise"})
"""

from pacomind.router.tiers import ModelTier, TierConfig, DEFAULT_TIERS
from pacomind.router.complexity_scorer import ComplexityScorer, ComplexitySignals
from pacomind.router.router import LLMRouter, LLMResponse
from pacomind.router.self_learning import RouterSelfLearner
from pacomind.router.fallback import FallbackHandler

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
