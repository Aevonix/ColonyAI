"""Tests for adaptive context compression."""

import pytest

from colony_sidecar.compression import (
    CompressionConfig,
    CompressionMode,
    SectionInfo,
    compress_sections,
    estimate_tokens,
    relevance_score,
    _truncate_text,
    _tier1_drop,
    _tier2_truncate,
)


def _section(id: str, body: str, priority: int = 50) -> dict:
    return {"id": id, "title": f"Section {id}", "body": body, "priority": priority}


LOREM = " ".join(f"word{i}" for i in range(100))


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_single_word(self):
        assert estimate_tokens("hello") == 1

    def test_approximate(self):
        tokens = estimate_tokens("the quick brown fox jumps over the lazy dog")
        assert 8 <= tokens <= 12


class TestRelevanceScore:
    def test_preserved_section_gets_max(self):
        score = relevance_score("some body", "query", preserve=True)
        assert score == 1000.0

    def test_empty_query(self):
        score = relevance_score("some body text", "", priority=70)
        assert score == 70.0

    def test_exact_match(self):
        score = relevance_score("memory consolidation happened", "memory consolidation", priority=50)
        assert score > 0.3

    def test_no_overlap(self):
        score = relevance_score("xyz abc", "foo bar", priority=50)
        # Should be low relevance but still have priority weight
        assert score < 0.5


class TestCompressOff:
    def test_off_returns_original(self):
        sections = [_section("a", LOREM), _section("b", LOREM)]
        result = compress_sections(sections, config=CompressionConfig(mode=CompressionMode.OFF))
        assert len(result["sections"]) == 2
        assert result["metadata"]["applied"] is False

    def test_empty_sections(self):
        result = compress_sections([], config=CompressionConfig(mode=CompressionMode.BALANCED))
        assert result["sections"] == []


class TestTier1Conservative:
    def test_drops_lowest_priority(self):
        sections = [
            _section("high", LOREM, priority=90),
            _section("mid", LOREM, priority=70),
            _section("low", LOREM, priority=30),
        ]
        # Budget that only fits ~2 sections
        result = compress_sections(
            sections,
            query="memory",
            config=CompressionConfig(mode=CompressionMode.CONSERVATIVE, max_tokens=300),
        )
        ids = [s["id"] for s in result["sections"]]
        # "low" should be dropped first (lowest relevance * priority)
        assert "low" not in ids or len(result["sections"]) <= 2

    def test_preserves_identity(self):
        sections = [
            _section("colony-identity", "identity info", priority=95),
            _section("low", LOREM, priority=10),
        ]
        result = compress_sections(
            sections,
            query="",
            config=CompressionConfig(
                mode=CompressionMode.CONSERVATIVE,
                max_tokens=10,  # Very tight budget
            ),
        )
        ids = [s["id"] for s in result["sections"]]
        assert "colony-identity" in ids


class TestTier2Balanced:
    def test_truncates_body_text(self):
        long_body = " ".join(f"sentence{i}" for i in range(200))
        sections = [_section("a", long_body, priority=80)]
        result = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(
                mode=CompressionMode.BALANCED,
                max_tokens=50,
            ),
        )
        assert len(result["sections"]) == 1
        # Body should be shorter than original
        assert len(result["sections"][0]["body"]) < len(long_body)

    def test_respects_min_section_tokens(self):
        short_body = "short text"
        sections = [_section("a", short_body, priority=80)]
        result = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(
                mode=CompressionMode.BALANCED,
                max_tokens=50,
                min_section_tokens=5,
            ),
        )
        # Short section shouldn't be truncated below min
        assert result["sections"][0]["body"] == short_body


class TestTier3Aggressive:
    def test_aggressive_tighter_truncation(self):
        long_body = " ".join(f"sentence{i}" for i in range(300))
        sections = [_section("a", long_body, priority=60)]
        result_balanced = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(mode=CompressionMode.BALANCED, max_tokens=30),
        )
        result_aggressive = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(mode=CompressionMode.AGGRESSIVE, max_tokens=30),
        )
        # Aggressive should produce shorter or equal output
        assert len(result_aggressive["sections"][0]["body"]) <= len(result_balanced["sections"][0]["body"])


class TestTruncateText:
    def test_short_text_unchanged(self):
        assert _truncate_text("hello world", 100) == "hello world"

    def test_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = _truncate_text(text, 30)
        # Should break at a sentence boundary
        assert "First" in result
        assert len(result) < len(text)

    def test_word_boundary_fallback(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        result = _truncate_text(text, 15)
        assert result.endswith(" ...")


class TestCompressionStats:
    def test_stats_present(self):
        sections = [_section("a", LOREM, priority=80)]
        result = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(mode=CompressionMode.CONSERVATIVE, max_tokens=500),
        )
        stats = result["stats"]
        assert "original_tokens" in stats
        assert "result_tokens" in stats
        assert "compression_ratio" in stats
        assert stats["mode"] == "conservative"


class TestOverrideMode:
    def test_per_request_override(self):
        sections = [_section("a", LOREM, priority=50)]
        result = compress_sections(
            sections,
            query="test",
            config=CompressionConfig(mode=CompressionMode.OFF),
            override_mode=CompressionMode.BALANCED,
        )
        assert result["metadata"]["applied"] is True
        assert result["metadata"]["mode"] == "balanced"
