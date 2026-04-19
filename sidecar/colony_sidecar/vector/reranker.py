"""Reranker pipeline — scores and re-ranks retrieved documents.

Uses the same provider pattern as the embedding pipeline:
CUDA (sentence-transformers) for GPU, CPU for fallback,
and an OpenAI-compatible API provider for remote reranking.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from colony_sidecar.vector.tiers import ModelSpec

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """A single reranked document with score."""

    index: int          # Original index in the input list
    score: float        # Relevance score (higher = more relevant)
    text: str           # The document text


class RerankerProvider(ABC):
    """Base class for reranker providers."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id
        self._model = None

    @abstractmethod
    async def warmup(self) -> None: ...

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]: ...


class CUDARerankerProvider(RerankerProvider):
    """NVIDIA GPU reranker via sentence-transformers CrossEncoder."""

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)

    def _load_model(self):
        from sentence_transformers import CrossEncoder
        return CrossEncoder(self._model_id, device="cuda")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        if not documents:
            return []

        pairs = [(query, doc) for doc in documents]
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            functools.partial(self._model.predict, pairs),
        )

        results = [
            RerankResult(index=i, score=float(scores[i]), text=documents[i])
            for i in range(len(documents))
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class CPURerankerProvider(RerankerProvider):
    """CPU reranker via sentence-transformers CrossEncoder."""

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)

    def _load_model(self):
        from sentence_transformers import CrossEncoder
        return CrossEncoder(self._model_id, device="cpu")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        if not documents:
            return []

        pairs = [(query, doc) for doc in documents]
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            functools.partial(self._model.predict, pairs),
        )

        results = [
            RerankResult(index=i, score=float(scores[i]), text=documents[i])
            for i in range(len(documents))
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class MLXRerankerProvider(RerankerProvider):
    """Apple MLX reranker (uses CPU path until MLX CrossEncoder exists)."""

    async def warmup(self) -> None:
        # MLX doesn't have CrossEncoder yet — fall back to CPU
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)

    def _load_model(self):
        from sentence_transformers import CrossEncoder
        return CrossEncoder(self._model_id, device="mps")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        if not documents:
            return []

        pairs = [(query, doc) for doc in documents]
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            functools.partial(self._model.predict, pairs),
        )

        results = [
            RerankResult(index=i, score=float(scores[i]), text=documents[i])
            for i in range(len(documents))
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class OpenAIAPIRerankerProvider(RerankerProvider):
    """Reranker via an OpenAI-compatible API endpoint."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self._base_url: str = ""
        self._api_key: str = ""

    def configure(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def warmup(self) -> None:
        pass  # No local model to warm up

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        if not documents:
            return []

        # Use the Jina/Cohere rerank API format (widely supported)
        import httpx

        url = f"{self._base_url}/v1/rerank"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_id,
            "query": query,
            "documents": documents,
            "top_n": top_k,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("results", []):
            idx = item.get("index", 0)
            results.append(RerankResult(
                index=idx,
                score=item.get("relevance_score", 0.0),
                text=documents[idx],
            ))

        return results


def make_reranker_provider(
    spec: ModelSpec,
    gpu_type: str = "none",
    api_base_url: str | None = None,
    api_key: str | None = None,
) -> Optional[RerankerProvider]:
    """Create the appropriate reranker provider based on spec and hardware.

    Returns None if the spec is None (tier doesn't have a reranker).
    """
    if spec is None:
        return None

    if api_base_url and api_key:
        provider = OpenAIAPIRerankerProvider(spec.model_id)
        provider.configure(api_base_url, api_key)
        return provider

    if gpu_type == "cuda":
        return CUDARerankerProvider(spec.model_id)
    elif gpu_type == "mlx":
        return MLXRerankerProvider(spec.model_id)
    else:
        return CPURerankerProvider(spec.model_id)
