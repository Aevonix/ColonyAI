"""Colony Vector Store — hardware detection, config recommendation, and CLI.

Provides:
  - detect_hardware()    — probe CUDA, MLX, CPU resources
  - recommend_config()   — select best EmbeddingConfig for detected hardware
  - ensure_embed_config() — resolve config from env or auto-detect
  - ensure_model_ready()  — download model weights if missing
  - CLI entry point for ``colony vector setup`` and ``colony vector rebuild``
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from colony_sidecar.vector.config import EmbeddingConfig, HardwareProfile, VectorStoreConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

def detect_hardware() -> HardwareProfile:
    """Probe available compute resources.  Returns a structured profile."""
    profile = HardwareProfile()

    # CUDA
    try:
        import torch
        if torch.cuda.is_available():
            profile.cuda_available = True
            profile.cuda_device_count = torch.cuda.device_count()
            profile.cuda_vram_gb = sum(
                torch.cuda.get_device_properties(i).total_memory
                for i in range(profile.cuda_device_count)
            ) / 1e9
    except ImportError:
        pass

    # Apple Silicon (MLX)
    try:
        import mlx.core as mx  # noqa: F401
        profile.mlx_available = True
        profile.mlx_memory_gb = _probe_mlx_memory()
    except ImportError:
        pass

    # CPU fallback — always available
    try:
        import psutil
        profile.cpu_ram_gb = psutil.virtual_memory().total / 1e9
        profile.cpu_cores = psutil.cpu_count(logical=False) or 1
    except ImportError:
        import multiprocessing
        profile.cpu_cores = multiprocessing.cpu_count() or 1

    return profile


def _probe_mlx_memory() -> float:
    """Attempt to detect unified memory on Apple Silicon."""
    try:
        import subprocess
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip()) / 1e9
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Model recommendation
# ---------------------------------------------------------------------------

# Tiered recommendation matrix from spec §4.4.
# Each entry: (provider, model_id, dimensions, quantization)
_TIER_MATRIX = {
    "gpu-high": ("cuda", "Qwen/Qwen3-Embedding-8B", 4096, "fp8"),
    "gpu-balanced": ("cuda", "BAAI/bge-m3", 1024, None),
    "gpu-fast": ("cuda", "nomic-ai/nomic-embed-text-v1.5", 768, None),
    "native-mlx-high": ("native_mlx", "Qwen/Qwen3-Embedding-8B", 4096, None),
    "native-mlx-balanced": ("native_mlx", "BAAI/bge-m3", 1024, None),
    "mlx": ("mlx", "nomic-ai/nomic-embed-text-v1.5", 768, None),
    "cpu-quality": ("cpu", "nomic-ai/nomic-embed-text-v1.5", 768, "int8"),
    "cpu-lightweight": ("cpu", "BAAI/bge-small-en-v1.5", 384, None),
}


def _detect_tier(profile: HardwareProfile) -> str:
    """Select the best tier for the given hardware profile."""
    if profile.cuda_available:
        if profile.cuda_vram_gb >= 24:
            return "gpu-high"
        if profile.cuda_vram_gb >= 8:
            return "gpu-balanced"
        return "gpu-fast"
    if profile.mlx_available:
        # Use high tier for machines with 64GB+ unified memory
        if profile.mlx_memory_gb >= 64:
            return "native-mlx-high"
        if profile.mlx_memory_gb >= 32:
            return "native-mlx-balanced"
        return "mlx"
    if profile.cpu_ram_gb >= 16:
        return "cpu-quality"
    return "cpu-lightweight"


def recommend_config(profile: HardwareProfile) -> EmbeddingConfig:
    """Return the best EmbeddingConfig for the detected hardware profile."""
    tier = _detect_tier(profile)
    provider, model_id, dims, quant = _TIER_MATRIX[tier]
    return EmbeddingConfig(
        provider=provider,
        model_id=model_id,
        dimensions=dims,
        quantization=quant,
    )


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

async def ensure_embed_config() -> Optional[EmbeddingConfig]:
    """Resolve embedding config from env vars or auto-detect.

    Returns ``None`` if no config is set and the process is non-interactive,
    allowing Colony to start without embeddings (graceful degradation).
    """
    config = EmbeddingConfig.from_env()
    if config is not None:
        return config

    # Auto-detect hardware and recommend
    profile = detect_hardware()
    recommended = recommend_config(profile)

    # Non-interactive: degrade gracefully
    if not sys.stdin.isatty():
        logger.warning(
            "Vector search disabled — no COLONY_EMBED_MODEL configured. "
            "Run 'colony vector setup' to enable. "
            "Recommended: COLONY_EMBED_MODEL=%s COLONY_EMBED_DIMS=%d COLONY_EMBED_PROVIDER=%s",
            recommended.model_id,
            recommended.dimensions,
            recommended.provider,
        )
        return None

    # Interactive: print recommendation
    print("\n--- Colony Vector Store Setup ---")
    print(f"Detected hardware: {profile}")
    print(f"Recommended model: {recommended.model_id}")
    print(f"Provider: {recommended.provider}, Dimensions: {recommended.dimensions}")
    print("\nSet these environment variables to enable vector search:")
    print(f"  COLONY_EMBED_PROVIDER={recommended.provider}")
    print(f"  COLONY_EMBED_MODEL={recommended.model_id}")
    print(f"  COLONY_EMBED_DIMS={recommended.dimensions}")
    if recommended.quantization:
        print(f"  COLONY_EMBED_QUANTIZATION={recommended.quantization}")
    print()
    return None


async def ensure_model_ready(config: EmbeddingConfig) -> None:
    """Download model weights if not present in cache_dir.  Run warmup."""
    try:
        from huggingface_hub import snapshot_download

        cache_dir = config.cache_dir or None
        snapshot_download(config.model_id, cache_dir=cache_dir)
        logger.info("Model %s ready (cache=%s)", config.model_id, cache_dir or "default")
    except ImportError:
        logger.warning("huggingface_hub not installed — skipping model download check")
    except Exception as exc:
        logger.warning("Model download check failed (may already be cached): %s", exc)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def cli_setup(args: list[str] | None = None) -> None:
    """CLI handler for ``colony vector setup``."""
    import argparse

    parser = argparse.ArgumentParser(description="Colony vector store setup")
    parser.add_argument("--model", help="HuggingFace model ID to use")
    parser.add_argument(
        "--tier",
        choices=list(_TIER_MATRIX.keys()),
        help="Hardware tier (auto-detected if not specified)",
    )
    parsed = parser.parse_args(args if args is not None else [])

    profile = detect_hardware()
    print(f"\nHardware profile:")
    print(f"  CUDA: {'Yes' if profile.cuda_available else 'No'}"
          + (f" ({profile.cuda_device_count} device(s), {profile.cuda_vram_gb:.1f}GB VRAM)" if profile.cuda_available else ""))
    print(f"  MLX:  {'Yes' if profile.mlx_available else 'No'}"
          + (f" ({profile.mlx_memory_gb:.1f}GB unified)" if profile.mlx_available else ""))
    print(f"  CPU:  {profile.cpu_cores} cores, {profile.cpu_ram_gb:.1f}GB RAM")

    if parsed.tier:
        tier = parsed.tier
    else:
        tier = _detect_tier(profile)
    print(f"\nSelected tier: {tier}")

    if parsed.model:
        provider, _, dims, quant = _TIER_MATRIX[tier]
        config = EmbeddingConfig(
            provider=provider,
            model_id=parsed.model,
            dimensions=dims,
            quantization=quant,
        )
    else:
        config = recommend_config(profile)

    print(f"Model: {config.model_id}")
    print(f"Provider: {config.provider}, Dimensions: {config.dimensions}")
    if config.quantization:
        print(f"Quantization: {config.quantization}")

    print("\nAdd to your .env:")
    print(f"  COLONY_EMBED_PROVIDER={config.provider}")
    print(f"  COLONY_EMBED_MODEL={config.model_id}")
    print(f"  COLONY_EMBED_DIMS={config.dimensions}")
    if config.quantization:
        print(f"  COLONY_EMBED_QUANTIZATION={config.quantization}")

    # Download model
    try:
        print(f"\nDownloading model weights for {config.model_id}...")
        from huggingface_hub import snapshot_download
        cache_dir = config.cache_dir or None
        snapshot_download(config.model_id, cache_dir=cache_dir)
        print("Model downloaded successfully.")
    except ImportError:
        print("huggingface_hub not installed — install with: pip install huggingface-hub")
    except Exception as exc:
        print(f"Download failed (model may already be cached): {exc}")


def cli_rebuild(args: list[str] | None = None) -> None:
    """CLI handler for ``colony vector rebuild``."""
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild vector collections")
    parser.add_argument(
        "--collection",
        default="all",
        help="Collection to rebuild (default: all)",
    )
    parsed = parser.parse_args(args if args is not None else [])

    print(f"Rebuilding vector collection(s): {parsed.collection}")
    print("This re-embeds all data from Neo4j source text.")
    print("Use 'python scripts/vector_backfill.py' for the full backfill process.")


def cli_main() -> None:
    """Entry point for ``colony vector <subcommand>``."""
    if len(sys.argv) < 3:
        print("Usage: colony vector {setup|rebuild}")
        sys.exit(1)

    subcommand = sys.argv[2]
    remaining = sys.argv[3:]

    if subcommand == "setup":
        cli_setup(remaining)
    elif subcommand == "rebuild":
        cli_rebuild(remaining)
    else:
        print(f"Unknown subcommand: {subcommand}")
        print("Usage: colony vector {setup|rebuild}")
        sys.exit(1)
