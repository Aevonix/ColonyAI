"""Colony Vector — multimodal embedding providers.

CUDA, CPU, and API-based providers for text+image embedding.
All providers produce vectors in the same embedding space for a given model,
enabling cross-modal search (text query → image result, and vice versa).
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from colony_sidecar.vector.config import EmbeddingConfig
from colony_sidecar.vector.multimodal_types import EmbedInput, EmbedResult, ImageInput, Modality

logger = logging.getLogger(__name__)


class MultimodalEmbeddingProvider(ABC):
    """Base class for multimodal embedding providers."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model = None

    @property
    def model_id(self) -> str:
        return self._config.model_id

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    @property
    def modalities(self) -> list[str]:
        return ["text", "image"]

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text string."""

    @abstractmethod
    async def embed_image(self, image: ImageInput) -> list[float]:
        """Embed a single image."""

    async def embed_batch_text(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Default: sequential call."""
        return [await self.embed_text(t) for t in texts]

    async def embed_batch_image(self, images: list[ImageInput]) -> list[list[float]]:
        """Embed multiple images. Default: sequential with small batches."""
        results = []
        # Images are memory-heavy — process in small batches
        batch_size = 4
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            for img in batch:
                vec = await self.embed_image(img)
                results.append(vec)
        return results

    async def embed_mixed(self, items: list[EmbedInput]) -> list[list[float]]:
        """Embed a mix of text and image inputs."""
        results: list[Optional[list[float]]] = [None] * len(items)

        # Process texts and images in parallel batches
        text_indices = [i for i, item in enumerate(items) if item.modality == Modality.TEXT]
        image_indices = [i for i, item in enumerate(items) if item.modality == Modality.IMAGE]

        if text_indices:
            texts = [items[i].content for i in text_indices]
            text_vecs = await self.embed_batch_text(texts)
            for idx, vec in zip(text_indices, text_vecs):
                results[idx] = vec

        if image_indices:
            # Load images from content (path, URL, or base64)
            image_inputs = []
            for i in image_indices:
                img_input = await self._load_image_input(items[i])
                image_inputs.append(img_input)
            image_vecs = await self.embed_batch_image(image_inputs)
            for idx, vec in zip(image_indices, image_vecs):
                results[idx] = vec

        return [r for r in results if r is not None]

    async def _load_image_input(self, item: EmbedInput) -> ImageInput:
        """Convert an EmbedInput with image modality to ImageInput."""
        from colony_sidecar.vector.image_preprocess import (
            compute_image_hash,
            extract_exif,
            load_image,
            resize_image,
            validate_image,
        )

        data, mime = await load_image(item.content, item.mime_type)

        # Validate
        errors = validate_image(data)
        if errors:
            raise ValueError(f"Invalid image: {'; '.join(errors)}")

        # Resize if needed
        data, width, height = resize_image(data)

        # Extract EXIF
        exif = extract_exif(data)

        # Compute hash
        img_hash = compute_image_hash(data)

        return ImageInput(
            data=data,
            mime_type=mime or "image/jpeg",
            width=width,
            height=height,
            original_path=item.content if not item.content.startswith("data:") else "",
            image_hash=img_hash,
            exif=exif,
        )

    async def warmup(self) -> None:
        """Load model weights. Default: embed a test string."""
        try:
            _ = await self.embed_text("colony multimodal warmup")
            logger.info("Multimodal provider warmed up: %s", self.model_id)
        except Exception as exc:
            logger.warning("Multimodal warmup failed for %s: %s", self.model_id, exc)

    async def close(self) -> None:
        """Release model resources."""
        self._model = None

    async def health_check(self) -> dict[str, Any]:
        """Verify provider is producing valid output for both modalities."""
        import math

        result: dict[str, Any] = {
            "provider": self._config.provider,
            "model": self.model_id,
            "dims": self.dimensions,
            "modalities": self.modalities,
            "status": "error",
        }

        try:
            # Text embedding check
            t0 = time.monotonic()
            text_vec = await self.embed_text("colony health check")
            text_ms = (time.monotonic() - t0) * 1000

            if len(text_vec) != self.dimensions:
                result["error"] = f"text dimension mismatch: expected {self.dimensions}, got {len(text_vec)}"
                return result
            if any(math.isnan(v) or math.isinf(v) for v in text_vec):
                result["error"] = "text vector contains NaN or Inf"
                return result

            result["text_latency_ms"] = round(text_ms, 1)

            # Create a minimal test image (1x1 white JPEG)
            test_image = _test_image_jpeg()
            from colony_sidecar.vector.image_preprocess import compute_image_hash
            test_input = ImageInput(
                data=test_image,
                mime_type="image/jpeg",
                image_hash=compute_image_hash(test_image),
            )

            t0 = time.monotonic()
            img_vec = await self.embed_image(test_input)
            img_ms = (time.monotonic() - t0) * 1000

            if len(img_vec) != self.dimensions:
                result["error"] = f"image dimension mismatch: expected {self.dimensions}, got {len(img_vec)}"
                return result
            if any(math.isnan(v) or math.isinf(v) for v in img_vec):
                result["error"] = "image vector contains NaN or Inf"
                return result

            result["image_latency_ms"] = round(img_ms, 1)
            result["status"] = "ok"

        except Exception as exc:
            result["error"] = str(exc)

        return result


def _test_image_jpeg() -> bytes:
    """Generate a minimal 1x1 white JPEG for health checks."""
    # Smallest valid JPEG
    return bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0x7B, 0x94, 0x11, 0x00, 0x00, 0x00, 0x00, 0xFF,
        0xD9,
    ])


# ---------------------------------------------------------------------------
# CUDA Provider
# ---------------------------------------------------------------------------


class CUDAMultimodalProvider(MultimodalEmbeddingProvider):
    """Multimodal embedding on NVIDIA GPU via sentence-transformers or transformers."""

    async def embed_text(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._sync_embed_text, text)

    async def embed_image(self, image: ImageInput) -> list[float]:
        return await asyncio.to_thread(self._sync_embed_image, image)

    def _ensure_model(self):
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self._config.model_id, device="cuda")

    def _sync_embed_text(self, text: str) -> list[float]:
        self._ensure_model()
        vec = self._model.encode(text, normalize_embeddings=True)
        return vec.tolist()

    def _sync_embed_image(self, image: ImageInput) -> list[float]:
        self._ensure_model()
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image.data))
        vec = self._model.encode(img, normalize_embeddings=True)
        return vec.tolist()


# ---------------------------------------------------------------------------
# CPU Provider
# ---------------------------------------------------------------------------


class CPUMultimodalProvider(MultimodalEmbeddingProvider):
    """Multimodal embedding on CPU. Slow but works everywhere."""

    async def embed_text(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._sync_embed_text, text)

    async def embed_image(self, image: ImageInput) -> list[float]:
        return await asyncio.to_thread(self._sync_embed_image, image)

    def _ensure_model(self):
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self._config.model_id, device="cpu")

    def _sync_embed_text(self, text: str) -> list[float]:
        self._ensure_model()
        vec = self._model.encode(text, normalize_embeddings=True)
        return vec.tolist()

    def _sync_embed_image(self, image: ImageInput) -> list[float]:
        self._ensure_model()
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image.data))
        vec = self._model.encode(img, normalize_embeddings=True)
        return vec.tolist()


# ---------------------------------------------------------------------------
# API Provider (Jina, or any OpenAI-compatible endpoint with image support)
# ---------------------------------------------------------------------------


class APIMultimodalProvider(MultimodalEmbeddingProvider):
    """Multimodal embedding via API (Jina, or OpenAI-compatible with image support).

    Inherits API key from host configuration.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._base_url = config.base_url or "https://api.jina.ai/v1"
        self._api_key = config.api_key or ""

    async def embed_text(self, text: str) -> list[float]:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._config.model_id,
                    "input": [{"text": text}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]

    async def embed_image(self, image: ImageInput) -> list[float]:
        import base64
        import httpx

        b64 = base64.b64encode(image.data).decode()

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._config.model_id,
                    "input": [{"image": b64}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_multimodal_provider(config: EmbeddingConfig) -> MultimodalEmbeddingProvider:
    """Create a multimodal embedding provider based on config."""
    provider = config.provider.lower()

    if provider == "cuda":
        return CUDAMultimodalProvider(config)
    elif provider == "cpu":
        return CPUMultimodalProvider(config)
    elif provider in ("openai_api", "api", "jina"):
        return APIMultimodalProvider(config)
    else:
        logger.warning("Unknown multimodal provider '%s', falling back to CPU", provider)
        return CPUMultimodalProvider(config)
