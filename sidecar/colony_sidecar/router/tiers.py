"""Model tier definitions for the LLM cost router.

Tiers are configured by the host (OpenClaw, Hermes, etc.) at startup
via ``POST /v1/host/configure``. Colony does not manage its own LLM
credentials — it inherits them from whichever host mounts it.

Provider presets define sensible model assignments per tier for known
providers (Anthropic, OpenAI, ZAI, local vLLM).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from enum import Enum

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Provider presets — used by build_tiers_from_host()
# ---------------------------------------------------------------------------

_PROVIDER_PRESETS: dict[str, dict[ModelTier, TierConfig]] = {
    "anthropic": {
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
    },
    "openai": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="openai/gpt-4o-mini",
            max_tokens=4096,
            cost_per_1k_input=0.00015,
            cost_per_1k_output=0.0006,
            latency_p50_ms=600,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/gpt-4o",
            max_tokens=8192,
            cost_per_1k_input=0.0025,
            cost_per_1k_output=0.01,
            latency_p50_ms=2000,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/o3",
            max_tokens=32768,
            cost_per_1k_input=0.015,
            cost_per_1k_output=0.06,
            latency_p50_ms=5000,
        ),
    },
    "zai": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="openai/glm-4.7-flash",
            max_tokens=8192,
            cost_per_1k_input=0.06,
            cost_per_1k_output=0.4,
            latency_p50_ms=800,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/glm-5.1",
            max_tokens=131072,
            cost_per_1k_input=1.5,
            cost_per_1k_output=5,
            latency_p50_ms=2000,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/glm-5.1",
            max_tokens=131072,
            cost_per_1k_input=1.5,
            cost_per_1k_output=5,
            latency_p50_ms=2000,
        ),
    },
    "local": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="openai/local-small",
            max_tokens=4096,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1000,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/local-medium",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=2000,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/local-large",
            max_tokens=32768,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=3000,
        ),
    },
}


# Default tier configurations (Anthropic) — used only when no host
# has configured the router yet.
DEFAULT_TIERS: dict[ModelTier, TierConfig] = _PROVIDER_PRESETS["anthropic"]


def build_tiers_from_host(config: dict) -> dict[ModelTier, TierConfig]:
    """Build tier configurations from host-provided LLM config.

    The host (OpenClaw, Hermes, etc.) sends its LLM provider details
    via ``POST /v1/host/configure``. This function maps that config
    to TierConfig objects that the LLMRouter can use.

    Expected config shape::

        {
            "provider": "zai",
            "apiKey": "sk-...",
            "baseUrl": "https://...",
            "models": {
                "small": "glm-4.7-flash",
                "medium": "glm-5.1",
                "large": "glm-5.1"
            }
        }

    For OpenAI-compatible providers (zai, local, custom), the function
    sets the ``OPENAI_API_KEY`` and ``OPENAI_API_BASE`` environment
    variables so LiteLLM routes ``openai/*`` model IDs correctly.
    """
    provider = config.get("provider", "anthropic").lower()
    api_key = config.get("apiKey", "")
    base_url = config.get("baseUrl", "")
    models_override = config.get("models", {})

    # Start from the provider preset (or fall back to anthropic)
    tiers = _PROVIDER_PRESETS.get(provider)
    if tiers is None:
        logger.warning(
            "Unknown LLM provider %r — falling back to anthropic preset. "
            "Known providers: %s",
            provider,
            ", ".join(sorted(_PROVIDER_PRESETS)),
        )
        tiers = dict(_PROVIDER_PRESETS["anthropic"])

    # Deep-copy so we don't mutate the preset
    tiers = {tier: replace(cfg) for tier, cfg in tiers.items()}

    # Apply model overrides from host
    # For OpenAI-compatible providers, host sends bare model names (e.g.
    # "glm-5.1") but LiteLLM needs the "openai/" prefix to route correctly.
    needs_prefix = provider in ("zai", "local", "custom", "openai")
    if models_override:
        for tier_name, model_id in models_override.items():
            try:
                tier = ModelTier(tier_name)
            except ValueError:
                logger.warning("Unknown tier %r in model override — skipping", tier_name)
                continue
            if tier in tiers:
                # Auto-prefix "openai/" for OpenAI-compatible providers if
                # the host sent a bare model name without a provider prefix.
                if needs_prefix and "/" not in model_id:
                    model_id = f"openai/{model_id}"
                tiers[tier] = replace(tiers[tier], model_id=model_id)
                logger.info("Host override: %s tier -> %s", tier.value, model_id)

    # For OpenAI-compatible providers, set env vars that LiteLLM reads
    # so that "openai/model-name" model IDs route to the right endpoint.
    if provider in ("zai", "local", "custom"):
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_API_BASE"] = base_url
        elif provider == "zai":
            os.environ["OPENAI_API_BASE"] = "https://api.z.ai/api/paas/v4"
        logger.info(
            "Configured OpenAI-compat provider: base=%s key=%s...%s",
            os.environ.get("OPENAI_API_BASE", "(none)"),
            api_key[:4] if api_key else "(none)",
            api_key[-4:] if len(api_key) > 4 else "",
        )
    elif provider == "anthropic":
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
    elif provider == "openai":
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_API_BASE"] = base_url

    return tiers
