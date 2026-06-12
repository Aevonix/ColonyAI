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
    """Apple Silicon reranker via sentence-transformers CrossEncoder with PyTorch MPS.

    NOTE: This is the legacy provider. For true native MLX performance,
    use ``NativeMLXRerankerProvider`` (provider="native_mlx").

    NOTE: warmup() runs synchronously in the main thread to avoid
    PyTorch MPS deadlocks in asyncio.run_in_executor (Issue #17).
    """

    async def warmup(self) -> None:
        self._model = self._load_model()

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


# ---------------------------------------------------------------------------
# Native MLX reranker (Apple Silicon via true MLX framework)
# ---------------------------------------------------------------------------

class NativeMLXRerankerProvider(RerankerProvider):
    """Apple Silicon reranker via the native MLX framework (mlx-lm).

    Loads the original HuggingFace CrossEncoder model with ``mlx_lm`` and
    extracts logits for the true/false classification tokens. This avoids
    the PyTorch MPS overhead entirely.

    The model must expose a ``config_sentence_transformers.json`` with
    ``true_token_id`` and ``false_token_id`` (standard for sentence-transformers
    CrossEncoder checkpoints). If absent, the provider falls back to logits
    for generic "yes"/"no" tokens.
    """

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self._model = None
        self._tokenizer = None
        self._true_token_id: int | None = None
        self._false_token_id: int | None = None

    async def warmup(self) -> None:
        self._model, self._tokenizer = self._load_model()

    def _load_model(self):
        from mlx_lm import load
        import json
        from pathlib import Path
        from huggingface_hub import try_to_load_from_cache

        model, tokenizer = load(self._model_id, lazy=False)
        self._instruction = None

        # Load instruction / prompt from sentence-transformers config
        try:
            cache_path = try_to_load_from_cache(
                self._model_id, "config_sentence_transformers.json"
            )
            if cache_path and Path(cache_path).exists():
                with open(cache_path) as f:
                    st_cfg = json.load(f)
            else:
                from huggingface_hub import snapshot_download
                model_path = Path(snapshot_download(self._model_id, allow_patterns=["config_sentence_transformers.json"]))
                with open(model_path / "config_sentence_transformers.json") as f:
                    st_cfg = json.load(f)

            prompts = st_cfg.get("prompts", {})
            default_name = st_cfg.get("default_prompt_name")
            if default_name and default_name in prompts:
                self._instruction = prompts[default_name]
            elif prompts:
                self._instruction = next(iter(prompts.values()))
        except Exception:
            pass

        # Attempt to load sentence-transformers config for token IDs
        try:
            # Check local cache first, then download
            cache_path = try_to_load_from_cache(
                self._model_id, "config_sentence_transformers.json"
            )
            if cache_path and Path(cache_path).exists():
                with open(cache_path) as f:
                    st_cfg = json.load(f)
            else:
                # Try to find in the model directory
                from huggingface_hub import snapshot_download
                model_path = Path(snapshot_download(self._model_id, allow_patterns=["config_sentence_transformers.json"]))
                with open(model_path / "config_sentence_transformers.json") as f:
                    st_cfg = json.load(f)

            # Look for LogitScore module config
            logit_cfg = st_cfg.get("1_LogitScore", {})
            if not logit_cfg:
                # Some models store it directly in the root
                logit_cfg = st_cfg
            self._true_token_id = logit_cfg.get("true_token_id")
            self._false_token_id = logit_cfg.get("false_token_id")
        except Exception:
            pass

        # Qwen3 rerankers store token IDs in 1_LogitScore/config.json
        # rather than inside config_sentence_transformers.json.
        if self._true_token_id is None or self._false_token_id is None:
            try:
                cache_path = try_to_load_from_cache(
                    self._model_id, "1_LogitScore/config.json"
                )
                if cache_path and Path(cache_path).exists():
                    with open(cache_path) as f:
                        logit_cfg = json.load(f)
                else:
                    from huggingface_hub import snapshot_download

                    model_path = Path(
                        snapshot_download(
                            self._model_id, allow_patterns=["1_LogitScore/config.json"]
                        )
                    )
                    with open(model_path / "1_LogitScore" / "config.json") as f:
                        logit_cfg = json.load(f)
                self._true_token_id = logit_cfg.get("true_token_id")
                self._false_token_id = logit_cfg.get("false_token_id")
            except Exception:
                pass

        return model, tokenizer

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        if not documents:
            return []

        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            functools.partial(self._score_documents, query, documents),
        )

        results = [
            RerankResult(index=i, score=float(scores[i]), text=documents[i])
            for i in range(len(documents))
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def _score_documents(self, query: str, documents: list[str]) -> list[float]:
        import mlx.core as mx

        scores: list[float] = []
        for doc in documents:
            # Use chat template if available (Qwen3 rerankers require this)
            if (
                hasattr(self._tokenizer, "apply_chat_template")
                and self._tokenizer.chat_template
            ):
                instruction = (
                    self._instruction
                    or "Given a web search query, retrieve relevant passages that answer the query"
                )
                messages = [
                    {"role": "system", "content": instruction},
                    {"role": "query", "content": query},
                    {"role": "document", "content": doc},
                ]
                prompt = self._tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                # Fallback for models without a chat template
                prompt = f"{query}\n{doc}"

            tokens = mx.array(self._tokenizer.encode(prompt))
            logits = self._model(tokens[None, :])
            last_logits = logits[0, -1, :]

            if self._true_token_id is not None and self._false_token_id is not None:
                true_score = last_logits[self._true_token_id].item()
                false_score = last_logits[self._false_token_id].item()
                scores.append(true_score - false_score)
            else:
                # Fallback: use max logit as relevance proxy
                scores.append(last_logits.max().item())

        return scores


# Qwen3-Reranker scores relevance from yes/no token logits, which are only
# calibrated when the request is wrapped in the model's instruction template
# (vLLM's /v1/rerank does not apply it server-side — raw strings score noise).
QWEN3_RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements "
    'based on the Query and the Instruct provided. Note that the answer can '
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
QWEN3_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
QWEN3_RERANK_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)


def format_qwen3_rerank(query: str, documents: list[str]) -> tuple[str, list[str]]:
    """Wrap a query and documents in the Qwen3-Reranker instruction template."""
    wrapped_query = (
        f"{QWEN3_RERANK_PREFIX}<Instruct>: {QWEN3_RERANK_INSTRUCTION}"
        f"\n<Query>: {query}\n"
    )
    wrapped_docs = [f"<Document>: {d}{QWEN3_RERANK_SUFFIX}" for d in documents]
    return wrapped_query, wrapped_docs


class OpenAIAPIRerankerProvider(RerankerProvider):
    """Reranker via an OpenAI-compatible API endpoint."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self._base_url: str = ""
        self._api_key: str = ""
        self._prompt_style: str = ""

    def configure(self, base_url: str, api_key: str, prompt_style: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._prompt_style = prompt_style

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

        send_query, send_docs = query, documents
        if self._prompt_style == "qwen3":
            send_query, send_docs = format_qwen3_rerank(query, documents)

        url = f"{self._base_url}/v1/rerank"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_id,
            "query": send_query,
            "documents": send_docs,
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

    # Check if this is a multimodal reranker
    if "image" in spec.modalities:
        return _make_multimodal_reranker(spec, gpu_type, api_base_url, api_key)

    if api_base_url and api_key:
        provider = OpenAIAPIRerankerProvider(spec.model_id)
        provider.configure(api_base_url, api_key)
        return provider

    if gpu_type == "cuda":
        return CUDARerankerProvider(spec.model_id)
    elif gpu_type == "native_mlx":
        return NativeMLXRerankerProvider(spec.model_id)
    elif gpu_type == "mlx":
        return MLXRerankerProvider(spec.model_id)
    else:
        return CPURerankerProvider(spec.model_id)


def _make_multimodal_reranker(
    spec: ModelSpec,
    gpu_type: str = "none",
    api_base_url: str | None = None,
    api_key: str | None = None,
) -> RerankerProvider:
    """Create a multimodal reranker provider.

    For multimodal rerankers (e.g. jina-reranker-m0), uses the CrossEncoder
    interface but supports image+text pairs. Falls back to text-only reranking
    on image captions when a true multimodal cross-encoder isn't available.
    """
    if api_base_url and api_key:
        provider = OpenAIAPIRerankerProvider(spec.model_id)
        provider.configure(api_base_url, api_key)
        return provider

    # Multimodal rerankers use the same CrossEncoder interface
    # but with image+text pair support
    if gpu_type == "cuda":
        return CUDAMultimodalRerankerProvider(spec.model_id)
    else:
        return CPUMultimodalRerankerProvider(spec.model_id)


class CUDAMultimodalRerankerProvider(RerankerProvider):
    """Multimodal reranker on NVIDIA GPU.

    Uses CrossEncoder with vision-language models that support
    text+image pairs. For image documents, uses caption as text
    representation.
    """

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
        scores = await loop.run_in_executor(None, self._model.predict, pairs)
        results = [
            RerankResult(index=i, score=float(s), text=documents[i])
            for i, s in enumerate(scores)
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class CPUMultimodalRerankerProvider(RerankerProvider):
    """Multimodal reranker on CPU."""

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
        scores = await loop.run_in_executor(None, self._model.predict, pairs)
        results = [
            RerankResult(index=i, score=float(s), text=documents[i])
            for i, s in enumerate(scores)
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]
