"""System scanner — detect GPU, VRAM, RAM to select embedding tier.

Scans the host hardware and returns a ``HardwareProfile`` that the
tier selector uses to pick the right models.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HardwareProfile:
    """Detected hardware capabilities."""

    gpu_type: str           # "cuda", "mlx", "none"
    gpu_name: str           # e.g. "NVIDIA GB10"
    vram_gb: int            # GPU memory in GB (0 if no GPU)
    ram_gb: int             # System RAM in GB
    cuda_version: str | None = None
    os: str = ""            # "linux", "darwin", "windows"


def scan() -> HardwareProfile:
    """Scan the system and return a HardwareProfile."""
    gpu_type = "none"
    gpu_name = ""
    vram_gb = 0
    cuda_version = None

    # Detect CUDA
    if _has_cuda():
        gpu_type = "cuda"
        gpu_name, vram_gb, cuda_version = _cuda_info()

    # Detect MLX (Apple Silicon)
    if gpu_type == "none" and _has_mlx():
        gpu_type = "mlx"
        gpu_name, vram_gb = _mlx_info()

    ram_gb = _ram_gb()
    system = platform.system().lower()

    profile = HardwareProfile(
        gpu_type=gpu_type,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        cuda_version=cuda_version,
        os=system,
    )

    logger.info(
        "Hardware scan: gpu=%s (%s, %dGB VRAM), ram=%dGB, cuda=%s, os=%s",
        profile.gpu_type, profile.gpu_name, profile.vram_gb,
        profile.ram_gb, profile.cuda_version, profile.os,
    )

    return profile


def _has_cuda() -> bool:
    """Check if CUDA is available."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _cuda_info() -> tuple[str, int, str | None]:
    """Get CUDA GPU name, VRAM, and driver version.

    Handles unified memory GPUs (Grace Blackwell, Grace Hopper, Tegra)
    where nvidia-smi reports [N/A] for memory.total.  On these chips
    the GPU uses system RAM, so we fall back to total system RAM.
    """
    name = "Unknown NVIDIA GPU"
    vram = 0
    cuda_ver = None

    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            if len(parts) >= 2:
                name = parts[0].strip()
                mem_str = parts[1].strip()
                # Handle [N/A] for unified memory GPUs
                if mem_str.lower() in ("[n/a]", "n/a", ""):
                    vram = _ram_gb()  # Unified memory: GPU RAM == system RAM
                else:
                    vram = int(float(mem_str))
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            cuda_ver = result.stdout.strip()
    except Exception:
        pass

    return name, vram, cuda_ver


def _has_mlx() -> bool:
    """Check if Apple MLX is available (M-series chip)."""
    if platform.system() != "Darwin":
        return False
    try:
        # Check for Apple Silicon
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "Apple" in result.stdout:
            return True
    except Exception:
        pass
    return False


def _mlx_info() -> tuple[str, int]:
    """Get Apple Silicon GPU name and unified memory."""
    name = "Apple Silicon"
    vram = 0

    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
    except Exception:
        pass

    # On Apple Silicon, VRAM == RAM (unified memory)
    vram = _ram_gb()

    return name, vram


def _ram_gb() -> int:
    """Get system RAM in GB."""
    system = platform.system().lower()

    try:
        if system == "linux":
            result = subprocess.run(
                ["free", "-g", "--si"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("Mem:"):
                        return int(line.split()[1])
        elif system == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip()) // (1024 ** 3)
    except Exception:
        pass

    # Fallback: parse /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // (1024 * 1024) + 1
    except Exception:
        pass

    return 8  # Safe default
