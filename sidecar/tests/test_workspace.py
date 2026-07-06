"""Cognitive workspace (Mind M2): concerns, salience, thinking, sleep window."""

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.self_model.workspace import (
    ConcernStore, WorkspaceEngine, in_sleep_window,
)
from colony_sidecar.self_model.thinker import _parse


def make(tmp_path, thinker=None, journal=None):
    store = ConcernStore(db_path=str(tmp_path / "ws.db"))
    return WorkspaceEngine(store, thinker=thinker, journal=journal), store


# --- store + salience ------------------------------------------------------

def test_bump_dedups_and_raises(tmp_path):
    ws, store = make(tmp_path)
    a = ws.bump(kind="question", summary="why did X", dedup_key="k1",
                salience=0.5)
    b = ws.bump(kind="question", summary="why did X", dedup_key="k1",
                salience=0.4, sources=["s2"])
    assert a.concern_id == b.concern_id       # deduped
    assert b.salience > 0.5                    # raised, not replaced
    assert "s2" in b.concern_id or "s2" in b.sources
    assert len(store.active()) == 1


def test_kind_falls_back(tmp_path):
    ws, _ = make(tmp_path)
    c = ws.bump(kind="nonsense", summary="x", dedup_key="k")
    assert c.kind == "thread"


def test_decay_reduces_and_evicts(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKSPACE_HALFLIFE_HOURS", "12")
    monkeypatch.setenv("COLONY_WORKSPACE_EVICT_FLOOR", "0.2")
    ws, store = make(tmp_path)
    c = ws.bump(kind="thread", summary="fading", dedup_key="k", salience=0.3)
    # backdate last_touched by 24h (two half-lives -> ~0.075)
    store.set_salience(c.concern_id, 0.3)
    with store._lock:
        store._conn.execute(
            "UPDATE concerns SET last_touched=? WHERE concern_id=?",
            (time.time() - 24 * 3600, c.concern_id))
        store._conn.commit()
    evicted = ws.decay()
    assert evicted == 1
    assert store.active() == []


def test_capacity_evicts_lowest(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKSPACE_CAPACITY", "3")
    monkeypatch.setenv("COLONY_WORKSPACE_EVICT_FLOOR", "0.0")
    ws, store = make(tmp_path)
    for i in range(5):
        ws.bump(kind="thread", summary=f"c{i}", dedup_key=f"k{i}",
                salience=0.1 * (i + 1))
    ws.decay()
    active = store.active()
    assert len(active) == 3
    # the three highest-salience survive
    assert {c.summary for c in active} == {"c4", "c3", "c2"}


# --- thinking --------------------------------------------------------------

async def test_think_progress_sustains(tmp_path):
    async def thinker(c):
        return {"progress": True, "resolve": False, "note": "made progress"}
    ws, store = make(tmp_path, thinker=thinker)
    c = ws.bump(kind="question", summary="q", dedup_key="k", salience=0.8)
    out = await ws.think_once()
    assert out["progress"] and not out["resolved"]
    after = store.get(c.concern_id)
    assert after.thoughts_spent == 1
    assert after.salience == pytest.approx(0.8 * 0.9)   # gentle decay
    assert after.last_note == "made progress"


async def test_think_no_progress_decays_harder(tmp_path):
    async def thinker(c):
        return {"progress": False, "resolve": False, "note": "stuck"}
    ws, store = make(tmp_path, thinker=thinker)
    c = ws.bump(kind="question", summary="q", dedup_key="k", salience=0.8)
    await ws.think_once()
    assert store.get(c.concern_id).salience == pytest.approx(0.8 * 0.6)


async def test_think_resolves(tmp_path):
    async def thinker(c):
        return {"progress": True, "resolve": True, "note": "done"}
    ws, store = make(tmp_path, thinker=thinker)
    c = ws.bump(kind="question", summary="q", dedup_key="k", salience=0.8)
    await ws.think_once()
    assert store.get(c.concern_id).status == "resolved"
    assert store.active() == []            # resolved leaves the mind


async def test_think_respects_budget(tmp_path):
    calls = {"n": 0}
    async def thinker(c):
        calls["n"] += 1
        return {"progress": True, "resolve": False, "note": "x"}
    ws, store = make(tmp_path, thinker=thinker)
    c = ws.bump(kind="question", summary="q", dedup_key="k", salience=0.9,
                max_thoughts=2)
    assert await ws.think_once() is not None
    assert await ws.think_once() is not None
    assert await ws.think_once() is None    # budget exhausted
    assert calls["n"] == 2


async def test_think_picks_most_salient_thinkable(tmp_path):
    seen = []
    async def thinker(c):
        seen.append(c.summary)
        return {"progress": True, "resolve": False, "note": ""}
    ws, store = make(tmp_path, thinker=thinker)
    ws.bump(kind="thread", summary="low", dedup_key="lo", salience=0.2)
    ws.bump(kind="thread", summary="high", dedup_key="hi", salience=0.9)
    await ws.think_once()
    assert seen == ["high"]


async def test_think_none_without_thinker(tmp_path):
    ws, _ = make(tmp_path)
    ws.bump(kind="thread", summary="x", dedup_key="k", salience=0.9)
    assert await ws.think_once() is None


# --- sleep window ----------------------------------------------------------

def test_sleep_window(monkeypatch):
    monkeypatch.setenv("COLONY_SLEEP_WINDOW", "02:00-06:00")
    assert in_sleep_window(datetime(2026, 7, 6, 3, 30))
    assert not in_sleep_window(datetime(2026, 7, 6, 9, 0))


def test_sleep_window_wraps_midnight(monkeypatch):
    monkeypatch.setenv("COLONY_SLEEP_WINDOW", "22:00-06:00")
    assert in_sleep_window(datetime(2026, 7, 6, 23, 30))
    assert in_sleep_window(datetime(2026, 7, 6, 1, 0))
    assert not in_sleep_window(datetime(2026, 7, 6, 12, 0))


def test_sleep_window_disabled(monkeypatch):
    monkeypatch.delenv("COLONY_SLEEP_WINDOW", raising=False)
    assert not in_sleep_window(datetime(2026, 7, 6, 3, 0))


# --- thinker parse ---------------------------------------------------------

def test_thinker_parse():
    d = _parse('sure: {"progress": true, "resolve": false, "note": "n", '
               '"action": {"kind": "none"}}')
    assert d["progress"] and not d["resolve"] and d["note"] == "n"
    bad = _parse("no json here")
    assert bad["progress"] is False and bad["action"] == {"kind": "none"}


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(ws):
    orig = host_mod._workspace
    host_mod._workspace = ws
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._workspace = orig


async def test_api_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKSPACE", "shadow")
    ws, _ = make(tmp_path)
    ws.bump(kind="question", summary="on my mind", dedup_key="k",
            salience=0.7)
    async with _client(ws) as c:
        r = await c.get("/v1/host/self/workspace")
        body = r.json()
        assert body["available"] and body["mode"] == "shadow"
        assert body["concerns"][0]["summary"] == "on my mind"


async def test_api_unavailable():
    async with _client(None) as c:
        assert (await c.get("/v1/host/self/workspace")).json() == {"available": False}
