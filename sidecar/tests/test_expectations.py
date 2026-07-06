"""Expectation engine (Mind M3a): predictions, resolution, surprise, calibration."""

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.self_model.expectations import (
    ExpectationEngine, ExpectationStore,
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
