"""Tests for colony_sidecar.vector.openai_provider — API embedding provider."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from colony_sidecar.vector.config import EmbeddingConfig
from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider


class TestOpenAIAPIEmbeddingProvider:
    def test_configure(self):
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = OpenAIAPIEmbeddingProvider(config)
        provider.configure("https://api.openai.com", "sk-test")
        assert provider._base_url == "https://api.openai.com"
        assert provider._api_key == "sk-test"

    def test_dimensions(self):
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = OpenAIAPIEmbeddingProvider(config)
        assert provider.dimensions == 1536

    @pytest.mark.asyncio
    async def test_warmup_without_config(self):
        config = EmbeddingConfig(provider="openai_api", model_id="test", dimensions=384)
        provider = OpenAIAPIEmbeddingProvider(config)
        await provider.warmup()  # Should not raise, just warn

    @pytest.mark.asyncio
    async def test_embed_batch_empty(self):
        config = EmbeddingConfig(provider="openai_api", model_id="test", dimensions=384)
        provider = OpenAIAPIEmbeddingProvider(config)
        result = await provider.embed_batch([])
        assert result == []


class TestMakeProvider:
    def test_openai_api_provider(self):
        from colony_sidecar.vector.embedder import make_provider
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = make_provider(config)
        assert isinstance(provider, OpenAIAPIEmbeddingProvider)

    def test_unknown_provider_raises(self):
        from colony_sidecar.vector.embedder import make_provider
        config = EmbeddingConfig(provider="nonexistent", model_id="test", dimensions=384)
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            make_provider(config)
