"""Embedding + Reranker tier definitions.

Each tier maps to a memory range and specifies the best open-weight
models for that budget.  All models are loaded via ``sentence-transformers``
or the OpenAI-compatible API — same runtime path, different weights.

Tier selection is driven by the system scanner (VRAM / RAM / GPU)
or overridden by the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ModelSpec:
    """A single model with enough metadata for download and loading."""

    model_id: str          # HuggingFace repo ID or local path
    params: str            # e.g. "0.6B"
    dims: int              # embedding dimensions
    context: int           # max sequence length
    license: str           # SPDX or custom
    modalities: list[str] = field(default_factory=lambda: ["text"])


@dataclass(frozen=True)
class TierConfig:
    """Retrieval stack for a given memory budget."""

    label: str
    memory_range: str      # e.g. "8-16 GB"
    min_vram_gb: int       # minimum VRAM for GPU-accelerated loading
    min_ram_gb: int        # minimum system RAM for CPU fallback

    text_embedder: Optional[ModelSpec]
    text_reranker: Optional[ModelSpec]
    multimodal_embedder: Optional[ModelSpec]
    multimodal_reranker: Optional[ModelSpec]

    # Human-readable reasons when a component is None
    no_text_reranker_reason: str = ""
    no_multimodal_reason: str = ""
    no_multimodal_reranker_reason: str = ""


# ---------------------------------------------------------------------------
# Tier table — 8 text-only tiers + multimodal placeholders
# ---------------------------------------------------------------------------

TIERS: list[TierConfig] = [
    # ── 0-4 GB ──────────────────────────────────────────────────────────
    TierConfig(
        label="Minimal / CPU-only / Constrained edge",
        memory_range="0-4 GB",
        min_vram_gb=0,
        min_ram_gb=2,
        text_embedder=ModelSpec(
            model_id="sentence-transformers/all-MiniLM-L6-v2",
            params="23M", dims=384, context=256, license="Apache-2.0",
        ),
        text_reranker=None,
        no_text_reranker_reason="Insufficient headroom",
        multimodal_embedder=None,
        no_multimodal_reason="No credible sub-4GB multimodal embedder",
        multimodal_reranker=None,
    ),

    # ── 4-8 GB ─────────────────────────────────────────────────────────
    TierConfig(
        label="Edge / Low-end laptop",
        memory_range="4-8 GB",
        min_vram_gb=0,
        min_ram_gb=4,
        text_embedder=ModelSpec(
            model_id="nomic-ai/nomic-embed-text-v1.5",
            params="137M", dims=768, context=8192, license="Apache-2.0",
        ),
        text_reranker=None,
        no_text_reranker_reason="Reserve headroom for embedder batching",
        multimodal_embedder=ModelSpec(
            model_id="jinaai/jina-clip-v2",
            params="865M", dims=1024, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=None,
    ),

    # ── 8-16 GB ────────────────────────────────────────────────────────
    TierConfig(
        label="Consumer laptop",
        memory_range="8-16 GB",
        min_vram_gb=0,
        min_ram_gb=8,
        text_embedder=ModelSpec(
            model_id="Qwen/Qwen3-Embedding-0.6B",
            params="0.6B", dims=1024, context=32768, license="Apache-2.0",
        ),
        text_reranker=ModelSpec(
            model_id="BAAI/bge-reranker-v2-m3",
            params="568M", dims=0, context=8192, license="MIT",
        ),
        multimodal_embedder=ModelSpec(
            model_id="nomic-ai/colnomic-embed-multimodal-3b",
            params="3B", dims=2048, context=8192, license="Apache-2.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=None,
        no_multimodal_reranker_reason="Text-only reranker used on captions/metadata for image results",
    ),

    # ── 16-32 GB ───────────────────────────────────────────────────────
    TierConfig(
        label="Consumer GPU / Workstation laptop",
        memory_range="16-32 GB",
        min_vram_gb=6,
        min_ram_gb=16,
        text_embedder=ModelSpec(
            model_id="Qwen/Qwen3-Embedding-4B",
            params="4B", dims=2560, context=32768, license="Apache-2.0",
        ),
        text_reranker=ModelSpec(
            model_id="Qwen/Qwen3-Reranker-0.6B",
            params="0.6B", dims=0, context=32768, license="Apache-2.0",
        ),
        multimodal_embedder=ModelSpec(
            model_id="jinaai/jina-embeddings-v4",
            params="3.8B", dims=2048, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=ModelSpec(
            model_id="jinaai/jina-reranker-m0",
            params="0.6B", dims=0, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
    ),

    # ── 32-64 GB ───────────────────────────────────────────────────────
    TierConfig(
        label="Prosumer GPU / M-series Pro",
        memory_range="32-64 GB",
        min_vram_gb=12,
        min_ram_gb=32,
        text_embedder=ModelSpec(
            model_id="Qwen/Qwen3-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
        ),
        text_reranker=ModelSpec(
            model_id="Qwen/Qwen3-Reranker-4B",
            params="4B", dims=0, context=32768, license="Apache-2.0",
        ),
        multimodal_embedder=ModelSpec(
            model_id="Qwen/Qwen3-VL-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=ModelSpec(
            model_id="jinaai/jina-reranker-m0",
            params="0.6B", dims=0, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
    ),

    # ── 64-128 GB ──────────────────────────────────────────────────────
    TierConfig(
        label="Workstation / M-Ultra / Half DGX Spark",
        memory_range="64-128 GB",
        min_vram_gb=24,
        min_ram_gb=64,
        text_embedder=ModelSpec(
            model_id="Qwen/Qwen3-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
        ),
        text_reranker=ModelSpec(
            model_id="Qwen/Qwen3-Reranker-8B",
            params="8B", dims=0, context=32768, license="Apache-2.0",
        ),
        multimodal_embedder=ModelSpec(
            model_id="Qwen/Qwen3-VL-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=ModelSpec(
            model_id="jinaai/jina-reranker-m0",
            params="0.6B", dims=0, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
    ),

    # ── 128-256 GB ─────────────────────────────────────────────────────
    TierConfig(
        label="Server / Mac Studio Ultra / DGX Spark",
        memory_range="128-256 GB",
        min_vram_gb=48,
        min_ram_gb=128,
        text_embedder=ModelSpec(
            model_id="microsoft/harrier-oss-v1-27b",
            params="27B", dims=5376, context=32768, license="MIT",
        ),
        text_reranker=ModelSpec(
            model_id="Qwen/Qwen3-Reranker-8B",
            params="8B", dims=0, context=32768, license="Apache-2.0",
        ),
        multimodal_embedder=ModelSpec(
            model_id="Qwen/Qwen3-VL-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=ModelSpec(
            model_id="jinaai/jina-reranker-m0",
            params="0.6B", dims=0, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
    ),

    # ── 256 GB+ ────────────────────────────────────────────────────────
    TierConfig(
        label="Multi-GPU server / DGX Spark cluster",
        memory_range="256 GB+",
        min_vram_gb=96,
        min_ram_gb=256,
        text_embedder=ModelSpec(
            model_id="microsoft/harrier-oss-v1-27b",
            params="27B", dims=5376, context=32768, license="MIT",
        ),
        text_reranker=ModelSpec(
            model_id="Qwen/Qwen3-Reranker-8B",
            params="8B", dims=0, context=32768, license="Apache-2.0",
        ),
        multimodal_embedder=ModelSpec(
            model_id="Qwen/Qwen3-VL-Embedding-8B",
            params="8B", dims=4096, context=32768, license="Apache-2.0",
            modalities=["text", "image"],
        ),
        multimodal_reranker=ModelSpec(
            model_id="jinaai/jina-reranker-m0",
            params="0.6B", dims=0, context=8192, license="CC-BY-NC-4.0",
            modalities=["text", "image"],
        ),
    ),
]


def get_tier(index: int) -> TierConfig:
    """Get tier by index (0-7)."""
    if 0 <= index < len(TIERS):
        return TIERS[index]
    return TIERS[0]


def get_tier_by_memory(vram_gb: int, ram_gb: int) -> TierConfig:
    """Select the best tier for available memory.

    Picks the highest tier where both VRAM and RAM requirements are met.
    If no tier matches (extremely constrained), falls back to tier 0.
    """
    best = TIERS[0]
    for tier in TIERS:
        if vram_gb >= tier.min_vram_gb and ram_gb >= tier.min_ram_gb:
            best = tier
    return best
