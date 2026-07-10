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


# ---------------------------------------------------------------------------
# U25: surprise loop closure — TTL auto-resolve + accumulation consumer
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from colony_sidecar.events import broadcaster
from colony_sidecar.surprise.accumulation import (
    handle_surprise_accumulation, register as register_consumer,
)


def _backdate(store, surprise_id, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store._conn.execute("UPDATE surprises SET timestamp = ? WHERE id = ?",
                        (old, surprise_id))
    store._conn.commit()


class TestResolveStale:
    def test_stale_auto_resolved_fresh_untouched(self, surprise_store):
        stale = surprise_store.create_surprise(observation="old one")
        fresh = surprise_store.create_surprise(observation="new one")
        _backdate(surprise_store, stale["id"], days=30)
        assert surprise_store.resolve_stale(14) == 1
        s = surprise_store.get_surprise(stale["id"])
        assert s["resolved"] is True
        assert "auto-resolved" in s["resolution"]
        assert surprise_store.get_surprise(fresh["id"])["resolved"] is False

    def test_already_resolved_not_touched(self, surprise_store):
        s = surprise_store.create_surprise(observation="handled")
        surprise_store.resolve_surprise(s["id"], resolution="owner ack")
        _backdate(surprise_store, s["id"], days=30)
        assert surprise_store.resolve_stale(14) == 0
        assert surprise_store.get_surprise(s["id"])["resolution"] == "owner ack"

    def test_ttl_zero_disables(self, surprise_store):
        """Regression-lock: ttl <= 0 mutates nothing (opt-out path)."""
        s = surprise_store.create_surprise(observation="old")
        _backdate(surprise_store, s["id"], days=365)
        assert surprise_store.resolve_stale(0) == 0
        assert surprise_store.resolve_stale(-3) == 0
        assert surprise_store.get_surprise(s["id"])["resolved"] is False


class _FakeWorkspace:
    def __init__(self):
        self.bumps = []

    def bump(self, **kw):
        self.bumps.append(kw)


def _event(count):
    return {"type": "surprise.accumulation",
            "payload": {"unresolved_count": count}}


class TestAccumulationConsumer:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(broadcaster, "_subscribers", {})
        monkeypatch.setattr(broadcaster, "_broadcast_fn", lambda _e: None)
        monkeypatch.delenv("COLONY_WORKSPACE", raising=False)
        monkeypatch.delenv("COLONY_AUTONOMY_PRESET", raising=False)

    def test_noop_when_workspace_off(self, monkeypatch):
        """Regression-lock: default posture (workspace off) consumes
        nothing and never touches the host router."""
        assert handle_surprise_accumulation(_event(7)) is False

    def test_noop_when_workspace_enabled_but_unwired(self, monkeypatch):
        import colony_sidecar.api.routers.host as host_mod
        monkeypatch.setenv("COLONY_WORKSPACE", "shadow")
        monkeypatch.setattr(host_mod, "_workspace", None)
        assert handle_surprise_accumulation(_event(7)) is False

    def test_raises_concern_when_workspace_on(self, monkeypatch):
        import colony_sidecar.api.routers.host as host_mod
        ws = _FakeWorkspace()
        monkeypatch.setenv("COLONY_WORKSPACE", "shadow")
        monkeypatch.setattr(host_mod, "_workspace", ws)
        assert handle_surprise_accumulation(_event(7)) is True
        assert len(ws.bumps) == 1
        b = ws.bumps[0]
        assert b["kind"] == "anomaly"
        assert b["dedup_key"] == "surprise:accumulation"  # stable: merges
        assert "7 unresolved" in b["summary"]
        assert b["salience"] <= 0.9  # capped even for huge counts
        assert handle_surprise_accumulation(_event(100)) is True
        assert ws.bumps[1]["salience"] == 0.9

    def test_end_to_end_via_emit(self, monkeypatch):
        """register() + emit('surprise.accumulation') lands as a concern."""
        import colony_sidecar.api.routers.host as host_mod
        ws = _FakeWorkspace()
        monkeypatch.setenv("COLONY_WORKSPACE", "shadow")
        monkeypatch.setattr(host_mod, "_workspace", ws)
        register_consumer()
        broadcaster.emit("surprise.accumulation", {"unresolved_count": 5})
        assert len(ws.bumps) == 1 and "5 unresolved" in ws.bumps[0]["summary"]

    def test_consumer_error_never_breaks_emit(self, monkeypatch):
        def boom(_event):
            raise RuntimeError("consumer bug")
        broadcaster.subscribe("surprise.accumulation", boom)
        broadcaster.emit("surprise.accumulation", {"unresolved_count": 5})
        # no raise = pass


class TestPatternsScheduleDefault:
    def test_default_is_off(self, monkeypatch):
        """Regression-lock: without COLONY_PATTERNS_SCHEDULE=on the daily
        pattern_extract task is not registered (server gate condition)."""
        monkeypatch.delenv("COLONY_PATTERNS_SCHEDULE", raising=False)
        assert os.environ.get(
            "COLONY_PATTERNS_SCHEDULE", "off").strip().lower() != "on"
