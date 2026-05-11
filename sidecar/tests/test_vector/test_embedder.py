"""Tests for colony_sidecar.vector.embedder — embedding providers and factory."""
import pytest
from colony_sidecar.vector.embedder import (
    CUDAEmbeddingProvider,
    CPUEmbeddingProvider,
    MLXEmbeddingProvider,
    NativeMLXEmbeddingProvider,
    make_provider,
)
from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider
from colony_sidecar.vector.config import EmbeddingConfig


class TestMakeProvider:
    def test_cuda_provider(self):
        config = EmbeddingConfig(provider="cuda", model_id="test", dimensions=384)
        provider = make_provider(config)
        assert isinstance(provider, CUDAEmbeddingProvider)

    def test_cpu_provider(self):
        config = EmbeddingConfig(provider="cpu", model_id="test", dimensions=384)
        provider = make_provider(config)
        assert isinstance(provider, CPUEmbeddingProvider)

    def test_mlx_provider(self):
        config = EmbeddingConfig(provider="mlx", model_id="test", dimensions=384)
        provider = make_provider(config)
        assert isinstance(provider, MLXEmbeddingProvider)

    def test_native_mlx_provider(self):
        config = EmbeddingConfig(provider="native_mlx", model_id="test", dimensions=4096)
        provider = make_provider(config)
        assert isinstance(provider, NativeMLXEmbeddingProvider)

    def test_openai_provider(self):
        config = EmbeddingConfig(provider="openai_api", model_id="test", dimensions=1536)
        provider = make_provider(config)
        assert isinstance(provider, OpenAIAPIEmbeddingProvider)

    def test_skip_returns_none(self):
        config = EmbeddingConfig(provider="skip", model_id="test", dimensions=384)
        provider = make_provider(config)
        assert provider is None

    def test_unknown_provider_raises(self):
        config = EmbeddingConfig(provider="unknown", model_id="test", dimensions=384)
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            make_provider(config)
