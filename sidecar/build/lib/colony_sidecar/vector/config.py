"""Colony Vector Store — configuration dataclasses.

All embedding and vector store configuration is driven by environment
variables.  No model names, device strings, or hardware assumptions
appear in source code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HardwareProfile:
    """Detected compute resources on the current machine."""

    cuda_available: bool = False
    cuda_device_count: int = 0
    cuda_vram_gb: float = 0.0

    mlx_available: bool = False
    mlx_memory_gb: float = 0.0

    cpu_ram_gb: float = 0.0
    cpu_cores: int = 0

    @property
    def best_provider(self) -> str:
        """Return the recommended provider string for this hardware."""
        if self.cuda_available:
            return "cuda"
        if self.mlx_available:
            return "mlx"
        return "cpu"


@dataclass
class EmbeddingConfig:
    """Resolved at runtime from env/settings.  Never hardcoded."""

    provider: str  # "cuda" | "cpu" | "mlx"
    model_id: str  # HuggingFace model ID
    dimensions: int  # output embedding dimension
    max_batch_size: int = 64
    device: Optional[str] = None  # auto-detected if None
    quantization: Optional[str] = None  # "int8" | "fp8" | None
    cache_dir: Optional[str] = None  # local model weights directory

    @classmethod
    def from_env(cls) -> Optional[EmbeddingConfig]:
        """Build config from COLONY_EMBED_* environment variables.

        Returns ``None`` if the required variables (MODEL, DIMS) are not set.
        """
        model_id = os.environ.get("COLONY_EMBED_MODEL")
        dims_str = os.environ.get("COLONY_EMBED_DIMS")
        if not model_id or not dims_str:
            return None
        return cls(
            provider=os.environ.get("COLONY_EMBED_PROVIDER", "cpu"),
            model_id=model_id,
            dimensions=int(dims_str),
            max_batch_size=int(os.environ.get("COLONY_EMBED_BATCH_SIZE", "64")),
            device=os.environ.get("COLONY_EMBED_DEVICE") or None,
            quantization=os.environ.get("COLONY_EMBED_QUANTIZATION") or None,
            cache_dir=os.environ.get("COLONY_EMBED_CACHE_DIR") or None,
        )


@dataclass
class VectorStoreConfig:
    """Configuration for the LanceDB vector store."""

    data_dir: str = ""  # path to Lance files directory
    cache_size: int = 4096  # LRU cache entries for EmbeddingPipeline
    batch_window_ms: float = 5.0  # auto-batch collection window

    @classmethod
    def from_env(cls) -> VectorStoreConfig:
        """Build from COLONY_VECTOR_* environment variables."""
        colony_home = os.path.expanduser(
            os.environ.get("COLONY_HOME", os.path.join(os.path.expanduser("~"), ".colony"))
        )
        return cls(
            data_dir=os.environ.get(
                "COLONY_VECTOR_STORE_PATH",
                os.path.join(colony_home, "vector"),
            ),
            cache_size=int(os.environ.get("COLONY_EMBED_CACHE_SIZE", "4096")),
            batch_window_ms=float(os.environ.get("COLONY_EMBED_BATCH_WINDOW_MS", "5")),
        )
