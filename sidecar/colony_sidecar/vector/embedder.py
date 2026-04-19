"""Colony Vector Store — embedding providers and pipeline.

Provides hardware-agnostic embedding via swappable backends:
  - CUDAEmbeddingProvider  (NVIDIA GPU via sentence-transformers)
  - CPUEmbeddingProvider   (CPU via sentence-transformers)
  - MLXEmbeddingProvider   (Apple Silicon via mlx-lm)

EmbeddingPipeline wraps a provider and adds LRU caching + latency monitoring.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from colony_sidecar.vector.config import EmbeddingConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------

class EmbeddingProvider(ABC):
    """Abstract interface for all embedding backends."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config

    @property
    @abstractmethod
    def dimensions(self) -> int:
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Embed a single text string.  Returns float32 vector."""
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.  Returns list of float32 vectors."""
        ...

    @abstractmethod
    async def warmup(self) -> None:
        """Load model weights, allocate device memory, run one dummy batch."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...


# ---------------------------------------------------------------------------
# CUDA provider
# ---------------------------------------------------------------------------

class CUDAEmbeddingProvider(EmbeddingProvider):
    """NVIDIA GPU embedding via sentence-transformers."""

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._model = None

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)
        # Warm up with a dummy encode
        await loop.run_in_executor(None, self._model.encode, ["warmup"])

    def _load_model(self):
        from sentence_transformers import SentenceTransformer

        device = self._config.device or "cuda"
        kwargs: dict = {"device": device}
        if self._config.cache_dir:
            kwargs["cache_folder"] = self._config.cache_dir
        model = SentenceTransformer(self._config.model_id, **kwargs)
        return model

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, functools.partial(self._model.encode, [text], normalize_embeddings=True)
        )
        return result[0].tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                self._model.encode,
                texts,
                batch_size=self._config.max_batch_size,
                normalize_embeddings=True,
            ),
        )
        return result.tolist()

    async def close(self) -> None:
        self._model = None


# ---------------------------------------------------------------------------
# CPU provider
# ---------------------------------------------------------------------------

class CPUEmbeddingProvider(EmbeddingProvider):
    """CPU-only embedding via sentence-transformers."""

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._model = None

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)
        await loop.run_in_executor(None, self._model.encode, ["warmup"])

    def _load_model(self):
        from sentence_transformers import SentenceTransformer

        kwargs: dict = {"device": "cpu"}
        if self._config.cache_dir:
            kwargs["cache_folder"] = self._config.cache_dir
        model = SentenceTransformer(self._config.model_id, **kwargs)
        return model

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, functools.partial(self._model.encode, [text], normalize_embeddings=True)
        )
        return result[0].tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                self._model.encode,
                texts,
                batch_size=self._config.max_batch_size,
                normalize_embeddings=True,
            ),
        )
        return result.tolist()

    async def close(self) -> None:
        self._model = None


# ---------------------------------------------------------------------------
# MLX provider (Apple Silicon)
# ---------------------------------------------------------------------------

class MLXEmbeddingProvider(EmbeddingProvider):
    """Apple Silicon embedding via sentence-transformers with MLX backend."""

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._model = None

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)
        await loop.run_in_executor(None, self._model.encode, ["warmup"])

    def _load_model(self):
        from sentence_transformers import SentenceTransformer

        # sentence-transformers auto-detects MPS on Apple Silicon
        device = self._config.device or "mps"
        kwargs: dict = {"device": device}
        if self._config.cache_dir:
            kwargs["cache_folder"] = self._config.cache_dir
        model = SentenceTransformer(self._config.model_id, **kwargs)
        return model

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, functools.partial(self._model.encode, [text], normalize_embeddings=True)
        )
        return result[0].tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                self._model.encode,
                texts,
                batch_size=self._config.max_batch_size,
                normalize_embeddings=True,
            ),
        )
        return result.tolist()

    async def close(self) -> None:
        self._model = None


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def make_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Instantiate the correct provider for the given config.

    Supported providers: cuda, cpu, mlx, openai_api.
    """
    from colony_sidecar.vector.openai_provider import OpenAIAPIEmbeddingProvider

    providers = {
        "cuda": CUDAEmbeddingProvider,
        "cpu": CPUEmbeddingProvider,
        "mlx": MLXEmbeddingProvider,
        "openai_api": OpenAIAPIEmbeddingProvider,
    }
    provider_cls = providers.get(config.provider)
    if provider_cls is None:
        raise ValueError(
            f"Unknown embedding provider {config.provider!r}. "
            f"Choose from: {', '.join(providers)}"
        )
    return provider_cls(config)


# ---------------------------------------------------------------------------
# Embedding pipeline (caching + latency monitoring)
# ---------------------------------------------------------------------------

class EmbeddingPipeline:
    """Wraps an EmbeddingProvider with LRU caching and latency monitoring."""

    LATENCY_WARN_MS = 500.0  # Raised for Qwen3-Embedding-8B on CUDA

    def __init__(
        self,
        provider: EmbeddingProvider,
        cache_size: int = 4096,
    ) -> None:
        self._provider = provider
        self._cache: dict[str, list[float]] = {}
        self._cache_order: list[str] = []
        self._cache_size = cache_size

    @property
    def dimensions(self) -> int:
        return self._provider.dimensions

    async def warmup(self) -> None:
        """Delegate warmup to the underlying provider."""
        await self._provider.warmup()

    async def health_check(self) -> dict[str, Any]:
        """Verify the embedder is loaded and producing valid output.

        Returns a dict with keys:
          provider, model, dims, latency_ms, status ("ok"|"error"),
          and optionally error (str) if status is "error".
        """
        import math

        model_id = ""
        provider_name = ""
        dims = 0
        if hasattr(self._provider, "_config"):
            model_id = self._provider._config.model_id
            provider_name = self._provider._config.provider
            dims = self._provider._config.dimensions

        try:
            if self._provider is None:
                return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": 0, "status": "error", "error": "provider not initialized"}

            t0 = time.monotonic()
            vector = await self._provider.embed("colony health check")
            elapsed_ms = (time.monotonic() - t0) * 1000

            # Validate vector
            if not vector:
                return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": elapsed_ms, "status": "error", "error": "empty vector returned"}
            if len(vector) != dims:
                return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": elapsed_ms, "status": "error", "error": f"dimension mismatch: expected {dims}, got {len(vector)}"}
            if any(math.isnan(v) or math.isinf(v) for v in vector):
                return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": elapsed_ms, "status": "error", "error": "vector contains NaN or Inf"}
            if all(v == 0.0 for v in vector):
                return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": elapsed_ms, "status": "error", "error": "vector is all zeros"}

            return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": round(elapsed_ms, 1), "status": "ok"}
        except Exception as exc:
            return {"provider": provider_name, "model": model_id, "dims": dims, "latency_ms": 0, "status": "error", "error": str(exc)}

    async def embed(self, text: str) -> list[float]:
        """Single embed with LRU caching and latency monitoring."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached

        t0 = time.monotonic()
        vector = await self._provider.embed(text)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if elapsed_ms > self.LATENCY_WARN_MS:
            logger.warning(
                "Embedding latency %.0fms exceeds %.0fms threshold — "
                "check COLONY_EMBED_PROVIDER and model config",
                elapsed_ms,
                self.LATENCY_WARN_MS,
            )

        self._put_cache(text, vector)
        return vector

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Explicit batch embed — bypasses auto-batch window."""
        if not texts:
            return []

        # Check cache for each; collect uncached
        results: list[Optional[list[float]]] = []
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is not None:
                results.append(cached)
            else:
                results.append(None)
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            vectors = await self._provider.embed_batch(uncached_texts)
            for idx, vec in zip(uncached_indices, vectors):
                results[idx] = vec
                self._put_cache(uncached_texts[uncached_indices.index(idx)], vec)

        return results  # type: ignore[return-value]

    @property
    def embed_fn(self):
        """Drop-in callable for ColonyGraph.set_embed_fn()."""
        return self.embed

    async def close(self) -> None:
        """Release underlying provider resources."""
        await self._provider.close()
        self._cache.clear()
        self._cache_order.clear()

    def _put_cache(self, key: str, value: list[float]) -> None:
        if key in self._cache:
            return
        if len(self._cache_order) >= self._cache_size:
            evict = self._cache_order.pop(0)
            self._cache.pop(evict, None)
        self._cache[key] = value
        self._cache_order.append(key)
