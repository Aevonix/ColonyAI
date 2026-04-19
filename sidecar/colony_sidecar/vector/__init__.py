"""Colony Vector Store — local LanceDB-backed vector search.

Public API:
  - VectorStore         — LanceDB wrapper, CRUD + ANN search
  - EmbeddingPipeline   — provider + caching + latency monitoring
  - Collection          — enum of named vector collections
  - get_store()         — retrieve the singleton VectorStore instance
  - get_pipeline()      — retrieve the singleton EmbeddingPipeline instance
"""

from colony_sidecar.vector.collections import Collection
from colony_sidecar.vector.config import EmbeddingConfig, HardwareProfile, VectorStoreConfig
from colony_sidecar.vector.embedder import (
    CPUEmbeddingProvider,
    CUDAEmbeddingProvider,
    EmbeddingPipeline,
    EmbeddingProvider,
    MLXEmbeddingProvider,
    make_provider,
)
from colony_sidecar.vector.query import HybridQuery, VectorItem, VectorQuery, VectorResult

# Lazy import — VectorStore depends on pyarrow/lancedb which may not be installed
from typing import Any, Optional
_store: Any = None
_pipeline: Optional[EmbeddingPipeline] = None


def _VectorStore():
    """Lazy import for VectorStore."""
    from colony_sidecar.vector.store import VectorStore
    return VectorStore


def set_store(store: Any) -> None:
    global _store
    _store = store


def set_pipeline(pipeline: EmbeddingPipeline) -> None:
    global _pipeline
    _pipeline = pipeline


def get_store() -> Any:
    """Retrieve the singleton VectorStore, or None if not configured."""
    return _store


def get_pipeline() -> Optional[EmbeddingPipeline]:
    """Retrieve the singleton EmbeddingPipeline, or None if not configured."""
    return _pipeline


__all__ = [
    "Collection",
    "EmbeddingConfig",
    "EmbeddingPipeline",
    "EmbeddingProvider",
    "CUDAEmbeddingProvider",
    "CPUEmbeddingProvider",
    "MLXEmbeddingProvider",
    "HardwareProfile",
    "HybridQuery",
    "VectorItem",
    "VectorQuery",
    "VectorResult",
    "VectorStore",
    "VectorStoreConfig",
    "get_pipeline",
    "get_store",
    "make_provider",
    "set_pipeline",
    "set_store",
]
