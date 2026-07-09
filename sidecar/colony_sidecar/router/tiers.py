"""Model tier definitions for the LLM cost router.

Tiers are configured by the host (OpenClaw, Hermes, etc.) at startup
via ``POST /v1/host/configure``. Colony does not manage its own LLM
credentials — it inherits them from whichever host mounts it.

Provider presets define sensible model assignments per tier for known
providers (Anthropic, OpenAI, ZAI, Ollama, LM Studio, vLLM).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

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
    # Per-tier endpoint overrides (empty = inherit the provider-wide
    # endpoint configured via OPENAI_API_BASE / provider defaults).
    # These let different tiers live on different servers — e.g. a fast
    # small model on one endpoint and a large reasoning model on another.
    base_url: str = ""
    api_key: str = ""
    # Extra request-body fields forwarded verbatim on every call to this
    # tier (e.g. vLLM's per-request ``priority`` for --scheduling-policy
    # priority, or provider-specific sampling knobs).
    extra_body: dict[str, Any] | None = None
    # The model's *useful* context window in tokens — the point up to
    # which exact retrieval stays reliable, which is often well below the
    # advertised maximum. 0 = unknown/unlimited. Consumed by the context
    # gate to decide when to chunk/retrieve instead of passing whole.
    useful_context_tokens: int = 0


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
            model_id="openai/local-model",
            max_tokens=4096,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1000,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/local-model",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=2000,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/local-model",
            max_tokens=32768,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=3000,
        ),
    },
    "ollama": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="ollama/llama3.2",
            max_tokens=4096,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=800,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="ollama/mistral",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1500,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="ollama/deepseek-r1",
            max_tokens=32768,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=5000,
        ),
    },
    "lmstudio": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="openai/lmstudio-small",
            max_tokens=4096,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=800,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/lmstudio-medium",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1500,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/lmstudio-large",
            max_tokens=32768,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=3000,
        ),
    },
    "vllm": {
        ModelTier.SMALL: TierConfig(
            tier=ModelTier.SMALL,
            model_id="openai/vllm-small",
            max_tokens=4096,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=600,
        ),
        ModelTier.MEDIUM: TierConfig(
            tier=ModelTier.MEDIUM,
            model_id="openai/vllm-medium",
            max_tokens=8192,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=1200,
        ),
        ModelTier.LARGE: TierConfig(
            tier=ModelTier.LARGE,
            model_id="openai/vllm-large",
            max_tokens=32768,
            cost_per_1k_input=0,
            cost_per_1k_output=0,
            latency_p50_ms=2500,
        ),
    },
}


# Default tier configurations (Anthropic) — used only when no host
# has configured the router yet.
DEFAULT_TIERS: dict[ModelTier, TierConfig] = _PROVIDER_PRESETS["anthropic"]


# ---------------------------------------------------------------------------
# Local model discovery
# ---------------------------------------------------------------------------

def discover_ollama_models(base_url: str = "") -> list[dict[str, Any]]:
    """Query an Ollama server for installed models.

    Returns a list of dicts with ``name``, ``size``, and ``digest``.
    The *name* field can be used directly as a LiteLLM model ID
    (``ollama/<name>``).
    """
    import urllib.request

    url = base_url.rstrip("/") + "/api/tags" if base_url else "http://127.0.0.1:11434/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        models = []
        for m in data.get("models", []):
            models.append({
                "name": m.get("name", ""),
                "size": m.get("size", 0),
                "digest": m.get("digest", "")[:12],
            })
        return models
    except Exception as exc:
        logger.debug("Ollama model discovery failed (%s): %s", url, exc)
        return []


def discover_openai_compatible_models(base_url: str, api_key: str = "") -> list[dict[str, Any]]:
    """Query an OpenAI-compatible server (vLLM, LM Studio, etc.) for models.

    Returns a list of dicts with ``id`` and ``owned_by``.
    The *id* can be used as a LiteLLM model ID (``openai/<id>`` when
    routed through the OpenAI compat layer).
    """
    import urllib.request

    url = base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        models = []
        for m in data.get("data", []):
            models.append({
                "id": m.get("id", ""),
                "owned_by": m.get("owned_by", ""),
            })
        return models
    except Exception as exc:
        logger.debug("OpenAI-compat model discovery failed (%s): %s", url, exc)
        return []


def discover_local_models(provider: str, base_url: str = "", api_key: str = "") -> list[dict[str, Any]]:
    """Discover models for a local provider.

    Attempts Ollama discovery first (for ``provider == "ollama"`` or when
    the base URL looks like an Ollama endpoint), then falls back to the
    OpenAI-compatible ``/v1/models`` endpoint.
    """
    if provider == "ollama":
        return discover_ollama_models(base_url)

    if provider in ("local", "custom", "openai", "lmstudio", "vllm") and base_url:
        # Try OpenAI-compatible endpoint first
        models = discover_openai_compatible_models(base_url, api_key)
        if models:
            return models
        # Some local servers (e.g. Ollama with OpenAI compat) may also
        # expose the Ollama API on the same port
        ollama_models = discover_ollama_models(base_url)
        if ollama_models:
            return [{"id": m["name"], "source": "ollama"} for m in ollama_models]

    return []


# ---------------------------------------------------------------------------
# LiteLLM prefix helpers
# ---------------------------------------------------------------------------

_LITELLM_PREFIXES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "ollama",
        "azure",
        "azure_ai",
        "azure_text",
        "azure_chat",
        "bedrock",
        "vertex_ai",
        "gemini",
        "groq",
        "together_ai",
        "huggingface",
        "fireworks_ai",
        "mistral",
        "deepseek",
        "xai",
        "text-completion-openai",
        "cohere",
        "perplexity",
        "ai21",
        "replicate",
        "baseten",
        "vllm",
        "sagemaker",
        "cloudflare",
        "watsonx",
        "databricks",
        "zhipu",
        "clarifai",
        "jina_ai",
        "novita",
        "siliconflow",
        "openrouter",
        "maritalk",
    }
)


def _has_litellm_prefix(model_id: str) -> bool:
    """Return True if *model_id* starts with a known LiteLLM provider prefix."""
    if "/" not in model_id:
        return False
    prefix = model_id.split("/", 1)[0].lower()
    return prefix in _LITELLM_PREFIXES


# ---------------------------------------------------------------------------
# Tier building from host config
# ---------------------------------------------------------------------------

def build_tiers_from_host(config: dict) -> dict[ModelTier, TierConfig]:
    """Build tier configurations from host-provided LLM config.

    The host (OpenClaw, Hermes, etc.) sends its LLM provider details
    via ``POST /v1/host/configure``. This function maps that config
    to TierConfig objects that the LLMRouter can use.

    Supports arbitrary local and remote models. Hosts may send either
    fully-qualified LiteLLM model IDs (``ollama/llama3.2``,
    ``openai/gpt-4o``) or bare model names for known providers.

    When no explicit model overrides are provided for a local provider,
    the function attempts to auto-discover installed models and maps
    them to tiers heuristically (smallest → SMALL, largest → LARGE).

    Expected config shape::

        {
            "provider": "ollama",
            "apiKey": "",
            "baseUrl": "http://localhost:11434",
            "models": {
                "small": "llama3.2",
                "medium": "mistral",
                "large": "deepseek-r1"
            }
        }

    Each ``models`` value may also be an object instead of a bare model
    string, enabling *per-tier endpoints* — different tiers served by
    different servers — plus per-tier request extras::

        {
            "provider": "vllm",
            "baseUrl": "http://fast-host:8000/v1",   # default for all tiers
            "models": {
                "small": "fast-model",
                "large": {
                    "model": "big-model",
                    "baseUrl": "http://big-host:8000/v1",
                    "apiKey": "…",                    # optional
                    "extraBody": {"priority": 10},    # optional, sent verbatim
                    "usefulContextTokens": 65536,     # optional, context gate hint
                    "maxTokens": 32768                # optional, completion cap
                }
            }
        }

    Snake_case keys (``base_url``, ``api_key``, ``extra_body``,
    ``useful_context_tokens``, ``max_tokens``) are accepted as aliases.

    For OpenAI-compatible providers (zai, local, custom, lmstudio, vllm),
    the function sets ``OPENAI_API_KEY`` and ``OPENAI_API_BASE`` so
    LiteLLM routes ``openai/*`` model IDs correctly. For Ollama, it
    sets ``OLLAMA_API_BASE``.
    """
    provider = config.get("provider", "anthropic").lower()
    api_key = config.get("apiKey", "")
    base_url = config.get("baseUrl", "")
    models_override = config.get("models", {})

    def _spec_field(spec: dict, *names: str, default: Any = None) -> Any:
        """First present key among camelCase/snake_case aliases."""
        for name in names:
            if name in spec:
                return spec[name]
        return default

    def _parse_model_spec(value: Any) -> tuple[str, dict[str, Any]]:
        """Normalize a ``models`` entry (string or object) to (model_id, overrides).

        Overrides may contain: base_url, api_key, extra_body,
        useful_context_tokens, max_tokens.
        """
        if isinstance(value, str):
            return value, {}
        if isinstance(value, dict):
            model_id = str(_spec_field(value, "model", "model_id", default="") or "")
            overrides: dict[str, Any] = {}
            v = _spec_field(value, "baseUrl", "base_url")
            if v:
                overrides["base_url"] = str(v)
            v = _spec_field(value, "apiKey", "api_key")
            if v:
                overrides["api_key"] = str(v)
            v = _spec_field(value, "extraBody", "extra_body")
            if isinstance(v, dict) and v:
                overrides["extra_body"] = dict(v)
            v = _spec_field(value, "usefulContextTokens", "useful_context_tokens")
            if v:
                try:
                    overrides["useful_context_tokens"] = int(v)
                except (TypeError, ValueError):
                    logger.warning("Invalid usefulContextTokens %r — ignoring", v)
            v = _spec_field(value, "maxTokens", "max_tokens")
            if v:
                try:
                    overrides["max_tokens"] = int(v)
                except (TypeError, ValueError):
                    logger.warning("Invalid maxTokens %r — ignoring", v)
            return model_id, overrides
        logger.warning("Unsupported model spec %r — expected string or object", value)
        return "", {}

    # Determine whether the provider is OpenAI-compatible or Ollama-native
    openai_compat_providers = {"zai", "local", "custom", "lmstudio", "vllm", "openai"}
    ollama_providers = {"ollama"}

    # ------------------------------------------------------------------
    # Build tier skeleton
    # ------------------------------------------------------------------
    tiers = _PROVIDER_PRESETS.get(provider)
    if tiers is None:
        if models_override:
            # Unknown provider with explicit models — build generic zero-cost tiers
            # rather than silently falling back to anthropic.
            logger.info(
                "Unknown provider %r — building tiers from host model overrides.",
                provider,
            )
            tiers = {}
            for tier in (ModelTier.SMALL, ModelTier.MEDIUM, ModelTier.LARGE):
                raw_spec = models_override.get(tier.value)
                model_id = _parse_model_spec(raw_spec)[0] if raw_spec is not None else None
                if not model_id:
                    # Fallback for missing tier — use a generic placeholder
                    # that will be overridden below if the host provides it.
                    model_id = "openai/gpt-4o-mini"
                tiers[tier] = TierConfig(
                    tier=tier,
                    model_id=model_id,
                    max_tokens=8192,
                    cost_per_1k_input=0,
                    cost_per_1k_output=0,
                    latency_p50_ms=1500,
                )
        else:
            logger.warning(
                "Unknown LLM provider %r and no model overrides — falling back to anthropic preset. "
                "Known providers: %s",
                provider,
                ", ".join(sorted(_PROVIDER_PRESETS)),
            )
            tiers = dict(_PROVIDER_PRESETS["anthropic"])

    # Deep-copy so we don't mutate the preset
    tiers = {tier: replace(cfg) for tier, cfg in tiers.items()}

    # ------------------------------------------------------------------
    # Apply model overrides from host
    # ------------------------------------------------------------------
    if models_override:
        for tier_name, raw_spec in models_override.items():
            try:
                tier = ModelTier(tier_name)
            except ValueError:
                logger.warning("Unknown tier %r in model override — skipping", tier_name)
                continue
            if tier == ModelTier.HEURISTIC:
                # HEURISTIC is a rule-based non-LLM tier — ignore model overrides for it
                continue

            model_id, tier_overrides = _parse_model_spec(raw_spec)
            if not model_id:
                logger.warning("No model in spec for tier %r — skipping", tier_name)
                continue

            if tier not in tiers:
                # Host sent a model for a tier we don't have yet (generic build case)
                tiers[tier] = TierConfig(
                    tier=tier,
                    model_id=model_id,
                    max_tokens=8192,
                    cost_per_1k_input=0,
                    cost_per_1k_output=0,
                    latency_p50_ms=1500,
                )

            # If the model ID already contains a known LiteLLM provider prefix
            # (e.g. "ollama/llama3.2", "huggingface/mistral-7b"), preserve it exactly.
            # Otherwise apply provider-specific prefixing so bare names work.
            if not _has_litellm_prefix(model_id):
                if provider in ollama_providers:
                    model_id = f"ollama/{model_id}"
                elif provider in openai_compat_providers:
                    model_id = f"openai/{model_id}"
                # For anthropic and other providers, leave bare names as-is
                # (LiteLLM accepts "claude-sonnet-4-6" without a prefix when
                # ANTHROPIC_API_KEY is set).

            tiers[tier] = replace(tiers[tier], model_id=model_id, **tier_overrides)
            logger.info(
                "Host override: %s tier -> %s%s",
                tier.value,
                model_id,
                f" @ {tier_overrides['base_url']}" if tier_overrides.get("base_url") else "",
            )

    # ------------------------------------------------------------------
    # Auto-discovery when no overrides are provided for local providers
    # ------------------------------------------------------------------
    elif provider in ("ollama", "local", "custom", "lmstudio", "vllm"):
        discovered = discover_local_models(provider, base_url, api_key)
        if discovered:
            logger.info(
                "Discovered %d local model(s) for provider=%s; applying to tiers",
                len(discovered),
                provider,
            )
            # Map discovered models to tiers heuristically:
            # smallest -> SMALL, largest -> LARGE, middle -> MEDIUM
            sorted_models = sorted(discovered, key=lambda m: m.get("size", 0))
            if len(sorted_models) >= 1 and ModelTier.SMALL in tiers:
                small_id = sorted_models[0].get("name") or sorted_models[0].get("id", "")
                if small_id:
                    tiers[ModelTier.SMALL] = replace(
                        tiers[ModelTier.SMALL],
                        model_id=f"{provider}/{small_id}" if provider == "ollama" else f"openai/{small_id}",
                    )
            if len(sorted_models) >= 2 and ModelTier.LARGE in tiers:
                large_id = sorted_models[-1].get("name") or sorted_models[-1].get("id", "")
                if large_id:
                    tiers[ModelTier.LARGE] = replace(
                        tiers[ModelTier.LARGE],
                        model_id=f"{provider}/{large_id}" if provider == "ollama" else f"openai/{large_id}",
                    )
            if len(sorted_models) >= 3 and ModelTier.MEDIUM in tiers:
                mid_idx = len(sorted_models) // 2
                mid_id = sorted_models[mid_idx].get("name") or sorted_models[mid_idx].get("id", "")
                if mid_id:
                    tiers[ModelTier.MEDIUM] = replace(
                        tiers[ModelTier.MEDIUM],
                        model_id=f"{provider}/{mid_id}" if provider == "ollama" else f"openai/{mid_id}",
                    )
            elif len(sorted_models) == 2 and ModelTier.MEDIUM in tiers:
                # Only two models — use the larger one for medium too
                mid_id = sorted_models[-1].get("name") or sorted_models[-1].get("id", "")
                if mid_id:
                    tiers[ModelTier.MEDIUM] = replace(
                        tiers[ModelTier.MEDIUM],
                        model_id=f"{provider}/{mid_id}" if provider == "ollama" else f"openai/{mid_id}",
                    )
        else:
            logger.warning(
                "No model overrides provided and discovery failed for provider=%s. "
                "The router will use placeholder model IDs which likely do not exist "
                "on your local server. Pass explicit models in the host config, e.g.:\n"
                '  {"models": {"small": "llama3.2", "medium": "mistral", "large": "deepseek-r1"}}',
                provider,
            )

    # ------------------------------------------------------------------
    # Set provider-specific environment variables for LiteLLM
    # ------------------------------------------------------------------
    if provider in openai_compat_providers:
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
    elif provider in ollama_providers:
        if base_url:
            os.environ["OLLAMA_API_BASE"] = base_url
        else:
            os.environ.setdefault("OLLAMA_API_BASE", "http://127.0.0.1:11434")
        logger.info(
            "Configured Ollama provider: base=%s",
            os.environ.get("OLLAMA_API_BASE", "(default localhost:11434)"),
        )
    elif provider == "anthropic":
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

    return tiers
