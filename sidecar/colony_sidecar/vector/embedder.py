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
import os
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
        # PyTorch MPS backend deadlocks when model initialization runs in
        # asyncio's ThreadPoolExecutor during FastAPI lifespan startup.
        # Load synchronously in the main thread — startup is not serving
        # requests yet, so brief blocking is acceptable.
        # See: https://github.com/Aevonix/ColonyAI/issues/17
        self._model = self._load_model()
        self._model.encode(["warmup"])

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
        if config.provider == "skip":
            logger.info("Embedding provider set to 'skip' — embeddings disabled")
            return None
        raise ValueError(
            f"Unknown embedding provider {config.provider!r}. "
            f"Choose from: {', '.join(providers)}, skip"
        )
    return provider_cls(config)


# ---------------------------------------------------------------------------
# Embedding pipeline (caching + latency monitoring)
# ---------------------------------------------------------------------------

class EmbeddingPipeline:
    """Wraps an EmbeddingProvider with LRU caching and latency monitoring.

    Supports both text-only and multimodal embedding. When multimodal is
    enabled, the pipeline holds both a text provider and a multimodal provider.
    """

    LATENCY_WARN_MS = 500.0  # Raised for Qwen3-Embedding-8B on CUDA

    def __init__(
        self,
        provider: EmbeddingProvider,
        cache_size: int = 4096,
        multimodal_provider: Any = None,  # MultimodalEmbeddingProvider
        image_store: Any = None,  # LocalImageStore or EmbedOnlyStore
    ) -> None:
        self._provider = provider
        self._multimodal_provider = multimodal_provider
        self._image_store = image_store
        self._cache: dict[str, list[float]] = {}
        self._cache_order: list[str] = []
        self._cache_size = cache_size
        self._multimodal_enabled = multimodal_provider is not None

    @property
    def is_multimodal(self) -> bool:
        """True when multimodal embedding is active."""
        return self._multimodal_enabled

    @property
    def modalities(self) -> list[str]:
        """Supported modalities."""
        if self._multimodal_enabled and self._multimodal_provider:
            return self._multimodal_provider.modalities
        return ["text"]

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

    # ------------------------------------------------------------------
    # Multimodal methods
    # ------------------------------------------------------------------

    async def embed_image(self, source: str | bytes, mime_type: str = "", caption: str = "") -> tuple[list[float], dict[str, Any]]:
        """Embed a single image. Returns (vector, metadata).

        Parameters
        ----------
        source : str | bytes
            File path, URL, base64 string, or raw bytes.
        mime_type : str
            Optional MIME type hint.
        caption : str
            Optional user-provided caption.

        Returns
        -------
        tuple[list[float], dict]
            Vector and metadata dict with image_hash, image_ref, caption,
            thumbnail_ref, width, height, exif.
        """
        if not self._multimodal_enabled or not self._multimodal_provider:
            raise ValueError("Multimodal not enabled. Set COLONY_MULTIMODAL=true and restart.")

        from colony_sidecar.vector.multimodal_types import EmbedInput, Modality
        from colony_sidecar.vector.image_preprocess import (
            compute_image_hash, extract_exif, load_image, resize_image,
            strip_gps_exif, validate_image,
        )
        from colony_sidecar.vector.safety_image import check_image, ImageCheckLevel

        # Load image
        data, detected_mime = await load_image(source, mime_type)
        mime = detected_mime or "image/jpeg"

        # Validate
        errors = validate_image(data)
        if errors:
            raise ValueError(f"Invalid image: {'; '.join(errors)}")

        # Safety check
        check_level = ImageCheckLevel(os.environ.get("COLONY_IMAGE_CHECK", "basic"))
        result = await check_image(data, mime, level=check_level)
        if not result.safe:
            raise ValueError(f"Image check failed: {result.reason}")

        # Resize if needed
        data, width, height = resize_image(data)

        # Strip GPS from stored image if configured
        strip_gps = os.environ.get("COLONY_STRIP_EXIF_GPS", "true").lower() == "true"
        stored_data = strip_gps_exif(data) if strip_gps else data

        # Extract EXIF before stripping
        exif = extract_exif(data)

        # Compute hash
        img_hash = compute_image_hash(data)

        # Build ImageInput
        from colony_sidecar.vector.multimodal_types import ImageInput
        image_input = ImageInput(
            data=data, mime_type=mime, width=width, height=height,
            original_path=source if isinstance(source, str) and not source.startswith("data:") else "",
            image_hash=img_hash, exif=exif, caption=caption,
        )

        # Store image
        image_ref = ""
        thumbnail_ref = ""
        if self._image_store:
            stored = await self._image_store.store(image_input)
            image_ref = stored.path
            thumbnail_ref = stored.thumbnail_path

        # Auto-caption if no caption provided
        if not caption:
            from colony_sidecar.vector.caption import caption_image
            llm_cfg = getattr(self, "_llm_config", None)
            caption = await caption_image(image_input, llm_config=llm_cfg)

        # Embed
        vector = await self._multimodal_provider.embed_image(image_input)

        metadata = {
            "modality": "image",
            "image_hash": img_hash,
            "image_ref": image_ref,
            "thumbnail_ref": thumbnail_ref,
            "caption": caption,
            "width": width,
            "height": height,
            "model_id": self._multimodal_provider.model_id,
            "embedded_at": time.time(),
        }
        # Add EXIF data
        if exif.get("captured_at"):
            metadata["captured_at"] = exif["captured_at"]
        if exif.get("gps_lat") and exif.get("gps_lon"):
            metadata["gps_lat"] = exif["gps_lat"]
            metadata["gps_lon"] = exif["gps_lon"]

        return vector, metadata

    async def embed_images(
        self,
        sources: list[str | bytes],
        mime_types: list[str] | None = None,
        captions: list[str] | None = None,
    ) -> list[tuple[list[float], dict[str, Any]]]:
        """Embed multiple images. Returns list of (vector, metadata) tuples."""
        mimes = mime_types or [""] * len(sources)
        caps = captions or [""] * len(sources)
        results = []
        for src, mime, cap in zip(sources, mimes, caps):
            vec, meta = await self.embed_image(src, mime_type=mime, caption=cap)
            results.append((vec, meta))
        return results

    async def embed_mixed(self, items: list[dict]) -> list[tuple[list[float], dict[str, Any]]]:
        """Embed a mix of text and image inputs.

        Each item: {"type": "text", "content": "..."} or
                    {"type": "image", "content": "<path/url/base64>", "mime_type": "...", "caption": "..."}
        """
        results = []
        for item in items:
            item_type = item.get("type", "text")
            content = item.get("content", "")
            if item_type == "text":
                vec = await self.embed(content)
                meta = {"modality": "text", "model_id": self._provider._config.model_id, "embedded_at": time.time()}
                results.append((vec, meta))
            elif item_type == "image":
                vec, meta = await self.embed_image(
                    content,
                    mime_type=item.get("mime_type", ""),
                    caption=item.get("caption", ""),
                )
                results.append((vec, meta))
            else:
                raise ValueError(f"Unsupported input type: {item_type}")
        return results

    def set_llm_config(self, config: dict[str, Any]) -> None:
        """Set LLM config for auto-captioning."""
        self._llm_config = config

    @property
    def embed_fn(self):
        """Drop-in callable for ColonyGraph.set_embed_fn()."""
        return self.embed

    async def close(self) -> None:
        """Release underlying provider resources."""
        await self._provider.close()
        if self._multimodal_provider:
            await self._multimodal_provider.close()
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
