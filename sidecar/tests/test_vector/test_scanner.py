"""Tests for colony_sidecar.vector.scanner — hardware detection."""
import pytest
from unittest.mock import patch, MagicMock
from colony_sidecar.vector.scanner import scan, HardwareProfile, _ram_gb


class TestHardwareProfile:
    def test_defaults(self):
        p = HardwareProfile(gpu_type="none", gpu_name="", vram_gb=0, ram_gb=8)
        assert p.gpu_type == "none"
        assert p.cuda_version is None
        assert p.os == ""


class TestScan:
    @patch("colony_sidecar.vector.scanner._has_cuda", return_value=False)
    @patch("colony_sidecar.vector.scanner._has_mlx", return_value=False)
    @patch("colony_sidecar.vector.scanner._ram_gb", return_value=16)
    def test_no_gpu(self, mock_ram, mock_mlx, mock_cuda):
        profile = scan()
        assert profile.gpu_type == "none"
        assert profile.vram_gb == 0
        assert profile.ram_gb == 16

    @patch("colony_sidecar.vector.scanner._has_cuda", return_value=True)
    @patch("colony_sidecar.vector.scanner._cuda_info", return_value=("NVIDIA RTX 4090", 24, "535.0"))
    @patch("colony_sidecar.vector.scanner._has_mlx", return_value=False)
    @patch("colony_sidecar.vector.scanner._ram_gb", return_value=64)
    def test_cuda_gpu(self, mock_ram, mock_mlx, mock_cuda_info, mock_cuda):
        profile = scan()
        assert profile.gpu_type == "cuda"
        assert profile.gpu_name == "NVIDIA RTX 4090"
        assert profile.vram_gb == 24
        assert profile.cuda_version == "535.0"

    @patch("colony_sidecar.vector.scanner._has_cuda", return_value=True)
    @patch("colony_sidecar.vector.scanner._cuda_info", return_value=("NVIDIA GB10", 0, "580.142"))
    @patch("colony_sidecar.vector.scanner._has_mlx", return_value=False)
    @patch("colony_sidecar.vector.scanner._ram_gb", return_value=130)
    def test_unified_memory_gpu(self, mock_ram, mock_mlx, mock_cuda_info, mock_cuda):
        """Grace Blackwell GB10 reports [N/A] for VRAM — should fall back to system RAM."""
        profile = scan()
        assert profile.gpu_type == "cuda"
        assert profile.gpu_name == "NVIDIA GB10"
        # The _cuda_info mock returns 0 (simulating the [N/A] fallback in real code)
        # In production, the real _cuda_info would return ram_gb for unified memory

    @patch("colony_sidecar.vector.scanner._has_cuda", return_value=False)
    @patch("colony_sidecar.vector.scanner._has_mlx", return_value=True)
    @patch("colony_sidecar.vector.scanner._mlx_info", return_value=("Apple M2 Pro", 32))
    @patch("colony_sidecar.vector.scanner._ram_gb", return_value=32)
    def test_mlx_apple_silicon(self, mock_ram, mock_mlx_info, mock_mlx, mock_cuda):
        profile = scan()
        assert profile.gpu_type == "mlx"
        assert profile.gpu_name == "Apple M2 Pro"
        assert profile.vram_gb == 32  # Unified memory == RAM


class TestCudaInfo:
    def test_na_vram_fallback(self):
        """Test that [N/A] VRAM in nvidia-smi output triggers RAM fallback."""
        from colony_sidecar.vector.scanner import _cuda_info
        import subprocess

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "NVIDIA GB10, [N/A]"

        with patch("subprocess.run", return_value=mock_result):
            with patch("colony_sidecar.vector.scanner._ram_gb", return_value=130):
                name, vram, ver = _cuda_info()
                assert name == "NVIDIA GB10"
                assert vram == 130  # Falls back to system RAM
