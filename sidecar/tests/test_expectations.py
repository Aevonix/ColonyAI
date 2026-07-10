"""Expectation engine (Mind M3a): predictions, resolution, surprise, calibration."""

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.self_model.expectations import (
    ExpectationEngine, ExpectationStore, Prediction,
)


def store(tmp_path):
    return ExpectationStore(db_path=str(tmp_path / "exp.db"))


def test_create_dedups(tmp_path):
    s = store(tmp_path)
    a = s.create(subject="commitment:c1", domain="commitment",
                 expectation="done", confidence=0.7, horizon=time.time()+10,
                 source="t", dedup_key="commitment:c1")
    b = s.create(subject="commitment:c1", domain="commitment",
                 expectation="done", confidence=0.7, horizon=time.time()+10,
                 source="t", dedup_key="commitment:c1")
    assert a is not None and b is None
    assert len(s.pending()) == 1


def test_resolved_prediction_not_recreated(tmp_path):
    """A scored prediction must never come back for the same subject+horizon,
    or every checker pass re-scores the same miss forever (calibration n
    inflates, one failure reads as dozens)."""
    s = store(tmp_path)
    h = time.time() + 10
    a = s.create(subject="commitment:c1", domain="commitment",
                 expectation="done", confidence=0.7, horizon=h,
                 source="t", dedup_key="commitment:c1")
    s.resolve(a.prediction_id, "miss")
    again = s.create(subject="commitment:c1", domain="commitment",
                     expectation="done", confidence=0.7, horizon=h,
                     source="t", dedup_key="commitment:c1")
    assert again is None
    # a moved due date IS a new prediction
    moved = s.create(subject="commitment:c1", domain="commitment",
                     expectation="done", confidence=0.7, horizon=h + 3600,
                     source="t", dedup_key="commitment:c1")
    assert moved is not None


def test_repeat_surprises_merge_into_one_concern(tmp_path):
    """Misses about the same subject strengthen ONE anomaly concern (keyed by
    subject), never one concern per scoring pass."""
    ws = FakeWS()
    e = engine(tmp_path, ws=ws)
    for i in range(3):
        e._surprise(Prediction(
            prediction_id=f"p-{i}", subject="commitment:c1",
            domain="commitment", expectation="done", confidence=0.7,
            horizon=time.time(), source="t"))
    keys = {b["dedup_key"] for b in ws.bumps}
    assert len(ws.bumps) == 3 and keys == {"surprise:commitment:c1"}


def test_due_and_resolve(tmp_path):
    s = store(tmp_path)
    past = s.create(subject="x:1", domain="d", expectation="e",
                    confidence=0.6, horizon=time.time()-5, source="t",
                    dedup_key="x1")
    s.create(subject="x:2", domain="d", expectation="e", confidence=0.6,
             horizon=time.time()+9999, source="t", dedup_key="x2")
    due = s.due()
    assert len(due) == 1 and due[0].prediction_id == past.prediction_id
    s.resolve(past.prediction_id, "hit")
    assert len(s.due()) == 0


# --- resolvers + check -----------------------------------------------------

class FakeCommitments:
    def __init__(self, statuses):
        self._st = statuses  # id -> status
    def get(self, cid):
        return {"id": cid, "status": self._st.get(cid), "description": "d"}
    def list(self, status=None, limit=50, **kw):
        return {"commitments": [], "total": 0}


class FakeWS:
    def __init__(self): self.bumps = []
    def bump(self, **kw): self.bumps.append(kw)


def engine(tmp_path, commitments=None, ws=None):
    e = ExpectationEngine(store(tmp_path), workspace=ws)
    if commitments is not None:
        e._commitments = lambda: commitments
    else:
        e._commitments = lambda: None
    return e


def test_check_hit_on_fulfilled(tmp_path):
    e = engine(tmp_path, commitments=FakeCommitments({"c1": "fulfilled"}))
    p = e.store.create(subject="commitment:c1", domain="commitment",
                       expectation="e", confidence=0.8,
                       horizon=time.time()-5, source="t", dedup_key="k",
                       detail={"commitment_id": "c1"})
    counts = e.check()
    assert counts["hit"] == 1
    resolved = e.store.resolved_since(0)
    assert resolved and resolved[0].outcome == "hit"


def test_check_miss_emits_surprise(tmp_path):
    ws = FakeWS()
    e = engine(tmp_path, commitments=FakeCommitments({"c1": "overdue"}), ws=ws)
    e.store.create(subject="commitment:c1", domain="commitment",
                   expectation="fulfilled on time", confidence=0.8,
                   horizon=time.time()-5, source="t", dedup_key="k",
                   detail={"commitment_id": "c1"})
    counts = e.check()
    assert counts["miss"] == 1
    assert ws.bumps and ws.bumps[0]["kind"] == "anomaly"
    assert "surprise" in ws.bumps[0]["summary"]


def test_pending_commitment_past_due_is_miss(tmp_path):
    e = engine(tmp_path, commitments=FakeCommitments({"c1": "pending"}))
    e.store.create(subject="commitment:c1", domain="commitment",
                   expectation="e", confidence=0.7, horizon=time.time()-5,
                   source="t", dedup_key="k", detail={"commitment_id": "c1"})
    assert e.check()["miss"] == 1


def test_unresolvable_grace_then_unresolved(tmp_path):
    # no resolver matches -> stays pending until grace elapses
    e = engine(tmp_path)
    e.store.create(subject="mystery:1", domain="d", expectation="e",
                   confidence=0.5, horizon=time.time()-5, source="t",
                   dedup_key="k")
    assert e.check() == {"hit": 0, "miss": 0, "unresolved": 0}  # within grace
    # backdate horizon beyond the grace window
    with e.store._lock:
        e.store._conn.execute("UPDATE predictions SET horizon=?",
                              (time.time() - 90000,))
        e.store._conn.commit()
    assert e.check()["unresolved"] == 1


# --- generation ------------------------------------------------------------

class GenCommitments:
    def get(self, cid): return None
    def list(self, status=None, limit=50, **kw):
        if status == ["pending"]:
            due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            return {"commitments": [
                {"id": "c1", "due_at": due, "description": "ship the audit"},
                {"id": "c2", "due_at": None, "description": "no due date"}],
                "total": 2}
        if status == ["fulfilled"]:
            return {"commitments": [], "total": 8}
        if status == ["overdue"]:
            return {"commitments": [], "total": 2}
        return {"commitments": [], "total": 0}


def test_generate_from_commitments(tmp_path):
    e = engine(tmp_path, commitments=GenCommitments())
    n = e.generate_from_commitments()
    assert n == 1                      # only the one with a due date
    p = e.store.pending()[0]
    assert p.subject == "commitment:c1"
    # confidence reflects the 8/(8+2) track record, smoothed
    assert 0.7 < p.confidence < 0.85


class PastDueCommitments(GenCommitments):
    def list(self, status=None, limit=50, **kw):
        if status == ["pending"]:
            due = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            return {"commitments": [
                {"id": "c1", "due_at": due, "description": "already late"}],
                "total": 1}
        return super().list(status=status, limit=limit, **kw)


def test_generate_skips_past_due_commitments(tmp_path):
    """A commitment whose due date already passed yields NO prediction: there
    is nothing left to predict, and an instantly-due prediction would be
    scored the moment it exists (the re-miss churn loop)."""
    e = engine(tmp_path, commitments=PastDueCommitments())
    assert e.generate_from_commitments() == 0
    assert e.store.pending() == []


# --- calibration -----------------------------------------------------------

def test_calibration_brier(tmp_path):
    e = engine(tmp_path)
    # two confident-correct, one confident-wrong
    for i, (conf, out) in enumerate([(0.9, "hit"), (0.9, "hit"), (0.9, "miss")]):
        p = e.store.create(subject=f"x:{i}", domain="d", expectation="e",
                           confidence=conf, horizon=time.time(), source="t",
                           dedup_key=f"k{i}")
        e.store.resolve(p.prediction_id, out)
    cal = e.calibration()
    # brier = mean((0.9-1)^2, (0.9-1)^2, (0.9-0)^2) = (.01+.01+.81)/3
    assert cal["d"]["brier"] == pytest.approx((0.01 + 0.01 + 0.81) / 3, abs=1e-4)
    assert cal["d"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(eng):
    orig = host_mod._expectations
    host_mod._expectations = eng
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._expectations = orig


async def test_api_snapshot(tmp_path):
    e = engine(tmp_path)
    e.store.create(subject="x:1", domain="d", expectation="a thing happens",
                   confidence=0.6, horizon=time.time()+9999, source="t",
                   dedup_key="k")
    async with _client(e) as c:
        r = await c.get("/v1/host/self/expectations")
        body = r.json()
        assert body["available"] and body["pending"][0]["expectation"] == "a thing happens"


async def test_api_unavailable():
    async with _client(None) as c:
        assert (await c.get("/v1/host/self/expectations")).json() == {"available": False}


# ---------------------------------------------------------------------------
# World-model resolvers (U24): relationship-still-active, property-unchanged
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from colony_sidecar.world_model import expectation_resolvers as wr


class FakeWorldStore:
    def __init__(self, relationships=None, entities=None):
        self.relationships = relationships or []
        self.entities = entities or {}

    async def query_relationships(self, **kw):
        return self.relationships

    async def get_entity(self, entity_id, min_confidence=0.0):
        return self.entities.get(entity_id)


def _pred(subject, detail):
    return Prediction(prediction_id="p-1", subject=subject, domain="world",
                      expectation="x", confidence=0.6,
                      horizon=time.time(), source="t", detail=detail)


def _rel(active=True):
    return SimpleNamespace(is_active=active)


def test_register_world_resolvers(tmp_path):
    e = ExpectationEngine(store(tmp_path))
    wr.register_world_resolvers(e)
    assert wr.RELATIONSHIP_PREFIX in e._resolvers
    assert wr.PROPERTY_PREFIX in e._resolvers
    # guarded on engine presence: a missing engine is a clean no-op
    wr.register_world_resolvers(None)


def test_relationship_resolver_hit_miss_unknown(monkeypatch):
    detail = {"source_id": "we-1", "target_id": "we-2",
              "relationship_type": "WM_WORKS_AT"}
    p = _pred("world-relationship:we-1:we-2", detail)
    monkeypatch.setattr(host_mod, "_world_store",
                        FakeWorldStore(relationships=[_rel(True)]))
    assert wr.resolve_relationship_still_active(p) is True
    monkeypatch.setattr(host_mod, "_world_store",
                        FakeWorldStore(relationships=[_rel(False)]))
    assert wr.resolve_relationship_still_active(p) is False
    # never observed -> unresolvable, never a fabricated miss
    monkeypatch.setattr(host_mod, "_world_store", FakeWorldStore())
    assert wr.resolve_relationship_still_active(p) is None
    # no world store wired -> unresolvable
    monkeypatch.setattr(host_mod, "_world_store", None)
    assert wr.resolve_relationship_still_active(p) is None


def test_property_resolver_hit_miss_unknown(monkeypatch):
    ent = SimpleNamespace(id="we-1", properties={"status": "Active"})
    p = _pred("world-property:we-1:status",
              {"entity_id": "we-1", "key": "status", "value": "active"})
    monkeypatch.setattr(host_mod, "_world_store",
                        FakeWorldStore(entities={"we-1": ent}))
    assert wr.resolve_property_unchanged(p) is True  # value-normalized match
    ent.properties["status"] = "paused"
    assert wr.resolve_property_unchanged(p) is False
    # property no longer tracked -> visibility loss, not a miss
    del ent.properties["status"]
    assert wr.resolve_property_unchanged(p) is None
    # entity gone -> unresolvable
    monkeypatch.setattr(host_mod, "_world_store", FakeWorldStore())
    assert wr.resolve_property_unchanged(p) is None


def test_world_prediction_scored_via_engine_check(tmp_path, monkeypatch):
    """End-to-end: a due world-relationship prediction resolves through
    engine.check() using the registered resolver."""
    e = engine(tmp_path)
    wr.register_world_resolvers(e)
    e.store.create(
        subject="world-relationship:we-1:we-2", domain="world",
        expectation="relationship stays active", confidence=0.7,
        horizon=time.time() - 1, source="t", dedup_key="wr:we-1:we-2",
        detail={"source_id": "we-1", "target_id": "we-2"})
    monkeypatch.setattr(host_mod, "_world_store",
                        FakeWorldStore(relationships=[_rel(True)]))
    assert e.check()["hit"] == 1
