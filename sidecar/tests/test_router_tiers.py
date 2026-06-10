"""Tests for colony_sidecar.router.tiers — provider presets and host config builds."""

from __future__ import annotations

import os

import pytest

# router.tiers imports litellm (optional dependency) at module level;
# skip the whole file cleanly where it isn't installed.
pytest.importorskip("litellm")

from colony_sidecar.router.tiers import (
    ModelTier,
    TierConfig,
    _PROVIDER_PRESETS,
    _has_litellm_prefix,
    build_tiers_from_host,
    discover_local_models,
)


class TestHasLitellmPrefix:
    """Unit tests for _has_litellm_prefix."""

    @pytest.mark.parametrize(
        "model_id, expected",
        [
            ("openai/gpt-4o", True),
            ("ollama/llama3.2", True),
            ("anthropic/claude-sonnet-4-6", True),
            ("huggingface/mistral-7b", True),
            ("groq/llama-3.1-70b", True),
            ("gpt-4o", False),
            ("llama3.2", False),
            ("meta-llama/Meta-Llama-3-8B-Instruct", False),
            ("claude-sonnet-4-6", False),
        ],
    )
    def test_prefix_detection(self, model_id, expected):
        assert _has_litellm_prefix(model_id) is expected


class TestBuildTiersFromHost:
    """Unit tests for build_tiers_from_host."""

    def test_known_provider_openai(self, monkeypatch):
        """OpenAI provider uses preset and applies overrides."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "openai",
                "apiKey": "sk-test",
                "baseUrl": "https://api.openai.com/v1",
                "models": {"small": "gpt-4o-mini"},
            }
        )

        assert tiers[ModelTier.SMALL].model_id == "openai/gpt-4o-mini"
        assert os.environ.get("OPENAI_API_KEY") == "sk-test"
        assert os.environ.get("OPENAI_API_BASE") == "https://api.openai.com/v1"

    def test_ollama_provider_prefixes_bare_names(self, monkeypatch):
        """Ollama provider prefixes bare model names with 'ollama/'."""
        monkeypatch.delenv("OLLAMA_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "ollama",
                "baseUrl": "http://localhost:11434",
                "models": {"small": "llama3.2", "medium": "mistral"},
            }
        )

        assert tiers[ModelTier.SMALL].model_id == "ollama/llama3.2"
        assert tiers[ModelTier.MEDIUM].model_id == "ollama/mistral"
        assert os.environ.get("OLLAMA_API_BASE") == "http://localhost:11434"

    def test_ollama_preserves_fully_qualified_ids(self, monkeypatch):
        """Fully-qualified LiteLLM model IDs are preserved as-is."""
        monkeypatch.delenv("OLLAMA_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "ollama",
                "models": {"small": "ollama/phi4", "large": "huggingface/mistral-7b"},
            }
        )

        assert tiers[ModelTier.SMALL].model_id == "ollama/phi4"
        assert tiers[ModelTier.LARGE].model_id == "huggingface/mistral-7b"

    def test_local_provider_prefixes_bare_names(self, monkeypatch):
        """Local (OpenAI-compat) provider prefixes bare names with 'openai/'."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "local",
                "baseUrl": "http://localhost:1234/v1",
                "models": {"small": "phi-4", "medium": "mistral-7b"},
            }
        )

        assert tiers[ModelTier.SMALL].model_id == "openai/phi-4"
        assert tiers[ModelTier.MEDIUM].model_id == "openai/mistral-7b"
        assert os.environ.get("OPENAI_API_BASE") == "http://localhost:1234/v1"

    def test_lmstudio_provider(self, monkeypatch):
        """LM Studio is treated as OpenAI-compatible."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "lmstudio",
                "baseUrl": "http://localhost:1234/v1",
                "models": {"large": "qwen2.5-72b"},
            }
        )

        assert tiers[ModelTier.LARGE].model_id == "openai/qwen2.5-72b"
        assert os.environ.get("OPENAI_API_BASE") == "http://localhost:1234/v1"

    def test_vllm_provider(self, monkeypatch):
        """vLLM is treated as OpenAI-compatible."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "vllm",
                "baseUrl": "http://localhost:8000/v1",
                "models": {"medium": "meta-llama/Meta-Llama-3-8B-Instruct"},
            }
        )

        # meta-llama is not a known LiteLLM prefix, so it gets openai/ prefixed
        assert tiers[ModelTier.MEDIUM].model_id == "openai/meta-llama/Meta-Llama-3-8B-Instruct"
        assert os.environ.get("OPENAI_API_BASE") == "http://localhost:8000/v1"

    def test_unknown_provider_with_overrides_builds_generic_tiers(self, monkeypatch):
        """An unknown provider with explicit model overrides builds from overrides."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        tiers = build_tiers_from_host(
            {
                "provider": "my-local-server",
                "baseUrl": "http://localhost:9999",
                "models": {"small": "custom-model-v1", "large": "custom-model-v2"},
            }
        )

        assert tiers[ModelTier.SMALL].model_id == "custom-model-v1"
        # missing medium falls back to a generic placeholder
        assert tiers[ModelTier.MEDIUM].model_id == "openai/gpt-4o-mini"
        assert tiers[ModelTier.LARGE].model_id == "custom-model-v2"
        assert tiers[ModelTier.SMALL].cost_per_1k_input == 0
        assert tiers[ModelTier.SMALL].cost_per_1k_output == 0

    def test_unknown_provider_without_overrides_falls_back_to_anthropic(self):
        """An unknown provider without overrides falls back to anthropic preset."""
        tiers = build_tiers_from_host({"provider": "unknown-provider"})

        assert tiers[ModelTier.SMALL].model_id == _PROVIDER_PRESETS["anthropic"][
            ModelTier.SMALL
        ].model_id

    def test_zai_default_base_url(self, monkeypatch):
        """ZAI provider sets its default base URL when none is provided."""
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)

        build_tiers_from_host({"provider": "zai", "apiKey": "sk-zai"})

        assert os.environ.get("OPENAI_API_BASE") == "https://api.z.ai/api/paas/v4"

    def test_anthropic_sets_api_key(self, monkeypatch):
        """Anthropic provider sets ANTHROPIC_API_KEY."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        build_tiers_from_host({"provider": "anthropic", "apiKey": "sk-ant"})

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant"

    def test_heuristic_override_is_ignored(self):
        """HEURISTIC tier overrides are skipped — it's a non-LLM tier."""
        tiers = build_tiers_from_host(
            {
                "provider": "anthropic",
                "models": {"heuristic": "rule-based"},
            }
        )
        assert ModelTier.HEURISTIC not in tiers
        # Ensure the standard tiers still use anthropic preset
        assert tiers[ModelTier.SMALL].model_id == _PROVIDER_PRESETS["anthropic"][
            ModelTier.SMALL
        ].model_id

    def test_no_mutation_of_presets(self):
        """build_tiers_from_host must not mutate global presets."""
        original_small = _PROVIDER_PRESETS["openai"][ModelTier.SMALL].model_id

        build_tiers_from_host(
            {
                "provider": "openai",
                "models": {"small": "gpt-5-nano"},
            }
        )

        assert _PROVIDER_PRESETS["openai"][ModelTier.SMALL].model_id == original_small

    def test_ollama_default_base_url(self, monkeypatch):
        """Ollama provider sets a default base URL when none is provided."""
        monkeypatch.delenv("OLLAMA_API_BASE", raising=False)

        build_tiers_from_host({"provider": "ollama"})

        assert os.environ.get("OLLAMA_API_BASE") == "http://127.0.0.1:11434"


class TestProviderPresets:
    """Sanity checks on the built-in provider presets."""

    @pytest.mark.parametrize("provider", list(_PROVIDER_PRESETS.keys()))
    def test_all_tiers_present(self, provider):
        preset = _PROVIDER_PRESETS[provider]
        assert ModelTier.SMALL in preset
        assert ModelTier.MEDIUM in preset
        assert ModelTier.LARGE in preset

    @pytest.mark.parametrize("provider", ["ollama", "local", "lmstudio", "vllm"])
    def test_local_providers_have_zero_cost(self, provider):
        preset = _PROVIDER_PRESETS[provider]
        for tier in (ModelTier.SMALL, ModelTier.MEDIUM, ModelTier.LARGE):
            assert preset[tier].cost_per_1k_input == 0
            assert preset[tier].cost_per_1k_output == 0


class TestDiscoverLocalModels:
    """Sanity checks for the discovery helpers (mostly smoke tests)."""

    def test_discover_local_models_returns_list_for_unknown_provider(self):
        """Unknown providers return an empty list gracefully."""
        result = discover_local_models("unknown", "")
        assert result == []
