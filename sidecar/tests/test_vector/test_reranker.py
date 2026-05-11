"""Tests for colony_sidecar.vector.reranker — reranker providers and factory."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from colony_sidecar.vector.reranker import (
    RerankResult,
    RerankerProvider,
    CUDARerankerProvider,
    CPURerankerProvider,
    NativeMLXRerankerProvider,
    MLXRerankerProvider,
    OpenAIAPIRerankerProvider,
    make_reranker_provider,
)
from colony_sidecar.vector.tiers import ModelSpec


class TestRerankResult:
    def test_fields(self):
        r = RerankResult(index=0, score=0.95, text="hello")
        assert r.index == 0
        assert r.score == 0.95


class TestMakeRerankerProvider:
    def test_none_spec_returns_none(self):
        assert make_reranker_provider(spec=None) is None

    def test_cpu_provider(self):
        spec = ModelSpec(model_id="BAAI/bge-reranker-v2-m3", params="568M", dims=0, context=8192, license="MIT")
        provider = make_reranker_provider(spec=spec, gpu_type="none")
        assert isinstance(provider, CPURerankerProvider)

    def test_cuda_provider(self):
        spec = ModelSpec(model_id="Qwen/Qwen3-Reranker-0.6B", params="0.6B", dims=0, context=32768, license="Apache-2.0")
        provider = make_reranker_provider(spec=spec, gpu_type="cuda")
        assert isinstance(provider, CUDARerankerProvider)

    def test_mlx_provider(self):
        spec = ModelSpec(model_id="BAAI/bge-reranker-v2-m3", params="568M", dims=0, context=8192, license="MIT")
        provider = make_reranker_provider(spec=spec, gpu_type="mlx")
        assert isinstance(provider, MLXRerankerProvider)

    def test_native_mlx_provider(self):
        spec = ModelSpec(model_id="Qwen/Qwen3-Reranker-8B", params="8B", dims=0, context=32768, license="Apache-2.0")
        provider = make_reranker_provider(spec=spec, gpu_type="native_mlx")
        assert isinstance(provider, NativeMLXRerankerProvider)

    def test_api_provider(self):
        spec = ModelSpec(model_id="rerank-model", params="1B", dims=0, context=8192, license="MIT")
        provider = make_reranker_provider(spec=spec, api_base_url="https://api.example.com", api_key="sk-test")
        assert isinstance(provider, OpenAIAPIRerankerProvider)


class TestOpenAIAPIRerankerProvider:
    def test_configure(self):
        provider = OpenAIAPIRerankerProvider("test-model")
        provider.configure("https://api.example.com", "sk-test")
        assert provider._base_url == "https://api.example.com"
        assert provider._api_key == "sk-test"

    @pytest.mark.asyncio
    async def test_warmup_is_noop(self):
        provider = OpenAIAPIRerankerProvider("test-model")
        await provider.warmup()  # Should not raise
