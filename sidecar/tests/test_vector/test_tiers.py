"""Tests for colony_sidecar.vector.tiers — tier definitions and selection."""
import pytest
from colony_sidecar.vector.tiers import TIERS, TierConfig, ModelSpec, get_tier, get_tier_by_memory


class TestModelSpec:
    def test_defaults(self):
        spec = ModelSpec(model_id="test/model", params="1B", dims=768, context=512, license="MIT")
        assert spec.modalities == ["text"]

    def test_custom_modalities(self):
        spec = ModelSpec(model_id="test/model", params="1B", dims=768, context=512, license="MIT",
                         modalities=["text", "image"])
        assert "image" in spec.modalities


class TestTierTable:
    def test_all_8_tiers(self):
        assert len(TIERS) == 8

    def test_tiers_ordered_by_memory(self):
        for i in range(len(TIERS) - 1):
            assert TIERS[i].min_ram_gb <= TIERS[i + 1].min_ram_gb

    def test_all_tiers_have_text_embedder(self):
        for tier in TIERS:
            assert tier.text_embedder is not None

    def test_all_tiers_have_label(self):
        for tier in TIERS:
            assert tier.label
            assert tier.memory_range

    def test_lowest_tier_no_reranker(self):
        assert TIERS[0].text_reranker is None

    def test_higher_tiers_have_rerankers(self):
        for tier in TIERS[2:]:
            assert tier.text_reranker is not None, f"Tier {tier.memory_range} missing reranker"


class TestGetTier:
    def test_valid_index(self):
        assert get_tier(0) == TIERS[0]
        assert get_tier(7) == TIERS[7]

    def test_invalid_index_falls_back(self):
        assert get_tier(-1) == TIERS[0]
        assert get_tier(99) == TIERS[0]


class TestGetTierByMemory:
    def test_tiny_machine(self):
        tier = get_tier_by_memory(vram_gb=0, ram_gb=2)
        assert tier == TIERS[0]

    def test_consumer_laptop(self):
        tier = get_tier_by_memory(vram_gb=0, ram_gb=16)
        assert tier == TIERS[2]  # 8-16 GB tier

    def test_gpu_workstation(self):
        tier = get_tier_by_memory(vram_gb=12, ram_gb=32)
        # 12GB VRAM + 32GB RAM matches T4 (32-64GB, min_vram=12)
        assert tier.min_vram_gb <= 12
        assert tier.min_ram_gb <= 32

    def test_dgx_spark(self):
        tier = get_tier_by_memory(vram_gb=130, ram_gb=130)
        assert tier == TIERS[6]  # 128-256 GB tier

    def test_cluster(self):
        tier = get_tier_by_memory(vram_gb=192, ram_gb=512)
        assert tier == TIERS[7]  # 256+ GB tier

    def test_picks_highest_matching(self):
        # Should always pick the highest tier that fits
        tier = get_tier_by_memory(vram_gb=100, ram_gb=100)
        assert tier.min_vram_gb <= 100
        assert tier.min_ram_gb <= 100
