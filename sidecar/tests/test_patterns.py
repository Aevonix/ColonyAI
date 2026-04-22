"""Tests for PatternStore."""

import os
import tempfile

import pytest

from colony_sidecar.patterns.store import PatternStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = PatternStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestPatternCreate:
    def test_create_basic(self, store):
        result = store.create_pattern(
            pattern_type="entity_cooccurrence",
            description="Marc and ColonyAI appear together",
            pattern_key="cooc:Marc→ColonyAI",
            confidence=0.7,
        )
        assert result["pattern_type"] == "entity_cooccurrence"
        assert result["description"] == "Marc and ColonyAI appear together"
        assert result["frequency"] == 1
        assert result["confidence"] == 0.7
        assert result["id"]
        assert result["active"] is True

    def test_upsert_increments_frequency(self, store):
        store.create_pattern(
            pattern_type="relation_frequency",
            description="works_on appears 3 times",
            pattern_key="rel_freq:works_on",
        )
        result = store.create_pattern(
            pattern_type="relation_frequency",
            description="works_on appears 4 times",
            pattern_key="rel_freq:works_on",
            confidence=0.8,
        )
        assert result["frequency"] == 2
        assert result["description"] == "works_on appears 4 times"

    def test_confidence_clamped(self, store):
        result = store.create_pattern(
            pattern_type="test", description="t", pattern_key="k1", confidence=2.0,
        )
        assert result["confidence"] == 1.0

    def test_with_metadata(self, store):
        result = store.create_pattern(
            pattern_type="test",
            description="t",
            pattern_key="k2",
            metadata={"entities": ["A", "B"]},
        )
        assert result["metadata"] == {"entities": ["A", "B"]}


class TestPatternGet:
    def test_get_existing(self, store):
        created = store.create_pattern(pattern_type="test", description="t", pattern_key="k1")
        result = store.get_pattern(created["id"])
        assert result is not None
        assert result["id"] == created["id"]

    def test_get_nonexistent(self, store):
        assert store.get_pattern("nope") is None


class TestPatternList:
    def test_list_all(self, store):
        store.create_pattern(pattern_type="a", description="t1", pattern_key="k1")
        store.create_pattern(pattern_type="b", description="t2", pattern_key="k2")
        result = store.list_patterns()
        assert result["total"] == 2

    def test_list_by_type(self, store):
        store.create_pattern(pattern_type="a", description="t1", pattern_key="k1")
        store.create_pattern(pattern_type="b", description="t2", pattern_key="k2")
        result = store.list_patterns(pattern_type="a")
        assert result["total"] == 1

    def test_list_min_frequency(self, store):
        store.create_pattern(pattern_type="a", description="t1", pattern_key="k1")
        store.create_pattern(pattern_type="a", description="t1", pattern_key="k1")  # freq=2
        store.create_pattern(pattern_type="b", description="t2", pattern_key="k2")  # freq=1
        result = store.list_patterns(min_frequency=2)
        assert result["total"] == 1

    def test_list_active_only(self, store):
        created = store.create_pattern(pattern_type="a", description="t1", pattern_key="k1")
        store.update_pattern(created["id"], active=False)
        result = store.list_patterns(active_only=True)
        assert result["total"] == 0

    def test_list_pagination(self, store):
        for i in range(5):
            store.create_pattern(pattern_type="test", description=f"t{i}", pattern_key=f"k{i}")
        result = store.list_patterns(limit=2, offset=0)
        assert len(result["patterns"]) == 2
        assert result["total"] == 5


class TestPatternUpdate:
    def test_update_description(self, store):
        created = store.create_pattern(pattern_type="test", description="old", pattern_key="k1")
        result = store.update_pattern(created["id"], description="new")
        assert result["description"] == "new"

    def test_update_confidence(self, store):
        created = store.create_pattern(pattern_type="test", description="t", pattern_key="k1")
        result = store.update_pattern(created["id"], confidence=0.9)
        assert result["confidence"] == 0.9

    def test_deactivate(self, store):
        created = store.create_pattern(pattern_type="test", description="t", pattern_key="k1")
        result = store.update_pattern(created["id"], active=False)
        assert result["active"] is False

    def test_update_nonexistent(self, store):
        assert store.update_pattern("nope", description="x") is None


class TestPatternDelete:
    def test_delete_existing(self, store):
        created = store.create_pattern(pattern_type="test", description="t", pattern_key="k1")
        assert store.delete_pattern(created["id"]) is True
        assert store.get_pattern(created["id"]) is None

    def test_delete_nonexistent(self, store):
        assert store.delete_pattern("nope") is False


class TestDeactivateStale:
    def test_deactivate_low_frequency(self, store):
        store.create_pattern(pattern_type="test", description="rare", pattern_key="k1", frequency=1)
        store.create_pattern(pattern_type="test", description="common", pattern_key="k2", frequency=5)
        # Deactivate patterns with freq < 3
        count = store.deactivate_stale(min_frequency=3)
        assert count == 1
        result = store.list_patterns(active_only=True)
        assert result["total"] == 1
