"""Tests for SurpriseStore and SurpriseScorer."""

import os
import tempfile

import pytest

from colony_sidecar.surprise.store import SurpriseStore
from colony_sidecar.surprise.scorer import compute_surprise
from colony_sidecar.patterns.store import PatternStore


@pytest.fixture
def surprise_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = SurpriseStore(path)
    yield s
    s.close()
    os.unlink(path)


@pytest.fixture
def pattern_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = PatternStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestSurpriseCreate:
    def test_create_basic(self, surprise_store):
        result = surprise_store.create_surprise(
            observation="User mentioned a new project called BlueBio",
            expected="User usually discusses ColonyAI",
            surprise_score=0.8,
        )
        assert result["observation"] == "User mentioned a new project called BlueBio"
        assert result["expected"] == "User usually discusses ColonyAI"
        assert result["surprise_score"] == 0.8
        assert result["resolved"] is False
        assert result["id"]

    def test_score_clamped(self, surprise_store):
        result = surprise_store.create_surprise(observation="x", surprise_score=2.0)
        assert result["surprise_score"] == 1.0

    def test_with_context(self, surprise_store):
        result = surprise_store.create_surprise(
            observation="unexpected",
            surprise_score=0.5,
            context={"session": "main", "turn": 42},
        )
        assert result["context"] == {"session": "main", "turn": 42}


class TestSurpriseGet:
    def test_get_existing(self, surprise_store):
        created = surprise_store.create_surprise(observation="x", surprise_score=0.5)
        result = surprise_store.get_surprise(created["id"])
        assert result is not None

    def test_get_nonexistent(self, surprise_store):
        assert surprise_store.get_surprise("nope") is None


class TestSurpriseList:
    def test_list_all(self, surprise_store):
        surprise_store.create_surprise(observation="a", surprise_score=0.8)
        surprise_store.create_surprise(observation="b", surprise_score=0.3)
        result = surprise_store.list_surprises()
        assert result["total"] == 2
        # Sorted by score descending.
        assert result["surprises"][0]["surprise_score"] == 0.8

    def test_list_min_score(self, surprise_store):
        surprise_store.create_surprise(observation="a", surprise_score=0.8)
        surprise_store.create_surprise(observation="b", surprise_score=0.3)
        result = surprise_store.list_surprises(min_score=0.5)
        assert result["total"] == 1

    def test_list_unresolved_only(self, surprise_store):
        created = surprise_store.create_surprise(observation="a", surprise_score=0.5)
        surprise_store.resolve_surprise(created["id"])
        surprise_store.create_surprise(observation="b", surprise_score=0.6)
        result = surprise_store.list_surprises(resolved=False)
        assert result["total"] == 1

    def test_list_pagination(self, surprise_store):
        for i in range(5):
            surprise_store.create_surprise(observation=f"obs {i}", surprise_score=0.1 * i)
        result = surprise_store.list_surprises(limit=2)
        assert len(result["surprises"]) == 2
        assert result["total"] == 5


class TestSurpriseResolve:
    def test_resolve(self, surprise_store):
        created = surprise_store.create_surprise(observation="x", surprise_score=0.7)
        result = surprise_store.resolve_surprise(created["id"], resolution="explained by context")
        assert result["resolved"] is True
        assert result["resolution"] == "explained by context"

    def test_resolve_nonexistent(self, surprise_store):
        assert surprise_store.resolve_surprise("nope") is None


class TestSurpriseDelete:
    def test_delete_existing(self, surprise_store):
        created = surprise_store.create_surprise(observation="x", surprise_score=0.5)
        assert surprise_store.delete_surprise(created["id"]) is True
        assert surprise_store.get_surprise(created["id"]) is None

    def test_delete_nonexistent(self, surprise_store):
        assert surprise_store.delete_surprise("nope") is False


class TestSurpriseCountUnresolved:
    def test_count_unresolved(self, surprise_store):
        surprise_store.create_surprise(observation="a", surprise_score=0.7)
        surprise_store.create_surprise(observation="b", surprise_score=0.6)
        created = surprise_store.create_surprise(observation="c", surprise_score=0.5)
        surprise_store.resolve_surprise(created["id"])
        assert surprise_store.count_unresolved(since_hours=1) == 2

    def test_count_empty(self, surprise_store):
        assert surprise_store.count_unresolved() == 0


class TestGetUnresolved:
    def test_get_unresolved(self, surprise_store):
        surprise_store.create_surprise(observation="high", surprise_score=0.9)
        surprise_store.create_surprise(observation="low", surprise_score=0.3)
        result = surprise_store.get_unresolved(min_score=0.5)
        assert len(result) == 1
        assert result[0]["surprise_score"] == 0.9


class TestSurpriseScorer:
    def test_no_pattern_store(self):
        result = compute_surprise("something happened")
        assert result["surprise_score"] == 0.5

    def test_no_matching_pattern(self, pattern_store):
        result = compute_surprise("bluebio deployment failed", pattern_store=pattern_store)
        assert result["surprise_score"] == 0.7

    def test_high_frequency_match(self, pattern_store):
        # Create a high-frequency pattern.
        for _ in range(6):
            pattern_store.create_pattern(
                pattern_type="entity_cooccurrence",
                description="User and ColonyAI appear together",
                pattern_key="cooc:User:ColonyAI",
            )
        result = compute_surprise("User ColonyAI discussion", pattern_store=pattern_store)
        assert result["surprise_score"] == 0.0
        assert result["pattern_id"] is not None

    def test_low_frequency_match(self, pattern_store):
        pattern_store.create_pattern(
            pattern_type="relation_frequency",
            description="works_on relationship",
            pattern_key="rel_freq:works_on",
            frequency=3,
        )
        result = compute_surprise("works_on", pattern_store=pattern_store)
        assert result["surprise_score"] == 0.2
