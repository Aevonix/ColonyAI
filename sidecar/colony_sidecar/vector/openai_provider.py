"""OpenAI-compatible API embedding provider.

Calls an OpenAI-compatible ``/v1/embeddings`` endpoint using the
host's API key.  This lets Colony inherit embeddings from whichever
LLM provider the host is configured with — no separate API key needed.
"""

from __future__ import annotations

import logging
from typing import Any

from colony_sidecar.vector.config import EmbeddingConfig
from colony_sidecar.vector.embedder import EmbeddingProvider

logger = logging.getLogger(__name__)


class OpenAIAPIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that calls an OpenAI-compatible API endpoint.

    Uses ``httpx`` for async HTTP requests.  Inherits the API key and
    base URL from the host configuration (same key used for LLM calls).
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._base_url: str = ""
        self._api_key: str = ""

    def configure(
        self,
        base_url: str,
        api_key: str,
    ) -> None:
        """Set the API endpoint and key (called by the host plugin)."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def warmup(self) -> None:
        """Verify connectivity with a test embedding."""
        if not self._base_url or not self._api_key:
            logger.warning("OpenAI API embedder not configured — set base_url and api_key")
            return
        result = await self.embed("warmup")
        logger.info(
            "OpenAI API embedder warmed up (dims=%d)", len(result),
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a single text via the API."""
        results = await self.embed_batch([text])
        return results[0]

    async def close(self) -> None:
        """No resources to release for API provider."""
        pass

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via the API."""
        if not texts:
            return []

        import httpx

        url = f"{self._base_url}/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._config.model_id,
            "input": texts,
        }
        if self._config.dimensions:
            payload["dimensions"] = self._config.dimensions

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        embeddings: list[list[float]] = []
        for item in data.get("data", []):
            embeddings.append(item["embedding"])

        return embeddings
