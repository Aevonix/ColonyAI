"""Resolution semantics: settle-with-outcome, source settlement (concern ->
commitment cascade), re-raise suppression, dedup, and the learning signals.

The scenario that motivated all of this: an overdue commitment is ingested
into the workspace as a concern; the owner resolves the concern on the deck;
nothing settles the commitment, so the next ingest tick re-raises the concern
and the resolve is silently undone. These tests pin the whole chain shut.
"""

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.commitments.store import (
    CommitmentStore, _normalize_desc, _similar_desc,
)
from colony_sidecar.self_model import settlement
from colony_sidecar.self_model.workspace import ConcernStore, WorkspaceEngine


@pytest.fixture
def store(tmp_path):
    return CommitmentStore(db_path=tmp_path / "commitments.db")


@pytest.fixture
def ws(tmp_path):
    cs = ConcernStore(db_path=str(tmp_path / "ws.db"))
    return WorkspaceEngine(cs), cs


@pytest.fixture(autouse=True)
def _clean_settlers():
    saved = dict(settlement._SETTLERS)
    settlement._SETTLERS.clear()
    yield
    settlement._SETTLERS.clear()
    settlement._SETTLERS.update(saved)


def _overdue(store, person="owner", desc="send Sam the build recap"):
    """Create a commitment and backdate its due_at so it reads as overdue."""
    c = store.create(person_id=person, description=desc,
                     due_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    return store.update(c["id"], due_at=past)


# --- store.resolve ----------------------------------------------------------

class TestStoreResolve:
    def test_done_fulfills_and_records_resolution(self, store):
        c = store.create(person_id="owner", description="x")
        r = store.resolve(c["id"], outcome="done", note="handled", resolved_by="owner")
        assert r["status"] == "fulfilled"
        assert r["fulfilled_at"] is not None
        res = r["metadata"]["resolution"]
        assert res["outcome"] == "done" and res["by"] == "owner"
        assert res["note"] == "handled" and res["at"]

    @pytest.mark.parametrize("outcome", ["invalid", "duplicate", "wont_do", "obsolete"])
    def test_non_done_outcomes_cancel(self, store, outcome):
        c = store.create(person_id="owner", description="x")
        r = store.resolve(c["id"], outcome=outcome)
        assert r["status"] == "cancelled"
        assert r["metadata"]["resolution"]["outcome"] == outcome

    def test_resolve_overdue_status_works(self, store):
        c = _overdue(store)
        store.update(c["id"], status="overdue")
        r = store.resolve(c["id"], outcome="done")
        assert r["status"] == "fulfilled"

    def test_idempotent_on_terminal(self, store):
        c = store.create(person_id="owner", description="x")
        first = store.resolve(c["id"], outcome="done")
        again = store.resolve(c["id"], outcome="invalid")
        assert again["status"] == "fulfilled"          # unchanged
        assert again["metadata"]["resolution"]["outcome"] == "done"
        assert first["id"] == again["id"]

    def test_unknown_outcome_raises(self, store):
        c = store.create(person_id="owner", description="x")
        with pytest.raises(ValueError, match="outcome"):
            store.resolve(c["id"], outcome="nope")

    def test_missing_id_returns_none(self, store):
        assert store.resolve("ghost", outcome="done") is None

    def test_resolve_preserves_existing_metadata(self, store):
        c = store.create(person_id="owner", description="x",
                         metadata={"kind": "deliverable"})
        r = store.resolve(c["id"], outcome="done")
        assert r["metadata"]["kind"] == "deliverable"
        assert "resolution" in r["metadata"]


# --- open-status model ------------------------------------------------------

class TestOpenStatuses:
    def test_get_overdue_includes_flipped_rows(self, store):
        c = _overdue(store)
        assert any(x["id"] == c["id"] for x in store.get_overdue())
        store.update(c["id"], status="overdue")      # the condition worker flip
        assert any(x["id"] == c["id"] for x in store.get_overdue())

    def test_pending_for_person_includes_overdue(self, store):
        c = _overdue(store)
        store.update(c["id"], status="overdue")
        open_items = store.get_pending_for_person("owner")
        assert any(x["id"] == c["id"] for x in open_items)

    def test_terminal_items_stay_out(self, store):
        c = _overdue(store)
        store.resolve(c["id"], outcome="done")
        assert store.get_overdue() == []
        assert store.get_pending_for_person("owner") == []


# --- duplicate detection ----------------------------------------------------

class TestDuplicateDetection:
    def test_normalize_and_similar(self):
        a = _normalize_desc("Send Sam the build recap!")
        b = _normalize_desc("send sam the build recap")
        assert a == b and _similar_desc(a, b)
        c = _normalize_desc("send Sam the build recap tomorrow morning")
        assert _similar_desc(a, c)                    # containment/overlap
        d = _normalize_desc("water the plants")
        assert not _similar_desc(a, d)

    def test_find_open_duplicate(self, store):
        store.create(person_id="owner", description="Send Sam the build recap")
        hit = store.find_open_duplicate("owner", "send sam the build recap")
        assert hit is not None
        assert store.find_open_duplicate("owner", "feed the cat") is None
        assert store.find_open_duplicate("alice", "send sam the build recap") is None

    def test_resolved_items_do_not_match(self, store):
        c = store.create(person_id="owner", description="Send the recap")
        store.resolve(c["id"], outcome="done")
        assert store.find_open_duplicate("owner", "send the recap") is None


# --- learning signals -------------------------------------------------------

class TestLearningSignals:
    def test_resolution_stats(self, store):
        a = store.create(person_id="o", description="a", source_type="introspection")
        b = store.create(person_id="o", description="b", source_type="introspection")
        store.create(person_id="o", description="c", source_type="cognition")
        store.resolve(a["id"], outcome="done")
        store.resolve(b["id"], outcome="invalid")
        s = store.resolution_stats(days=7)["by_source"]
        assert s["introspection"]["created"] == 2
        assert s["introspection"]["fulfilled"] == 1
        assert s["introspection"]["cancelled"] == 1
        assert s["introspection"]["outcomes"]["invalid"] == 1
        assert s["cognition"]["open"] == 1

    def test_recent_rejections_only_invalid_and_duplicate(self, store):
        a = store.create(person_id="o", description="bogus item")
        b = store.create(person_id="o", description="twin item")
        c = store.create(person_id="o", description="dropped item")
        store.resolve(a["id"], outcome="invalid", note="never promised")
        store.resolve(b["id"], outcome="duplicate")
        store.resolve(c["id"], outcome="wont_do")
        rej = store.recent_rejections(limit=10)
        descs = {r["description"] for r in rej}
        assert descs == {"bogus item", "twin item"}
        assert all(r["outcome"] in ("invalid", "duplicate") for r in rej)


# --- settlement registry ----------------------------------------------------

class TestSettlement:
    def test_settles_registered_kinds_and_skips_unknown(self, store):
        c = store.create(person_id="o", description="x")
        settlement.register_settler(
            "commitment",
            lambda sid, **kw: ({"status": store.resolve(sid, **{
                k: v for k, v in kw.items()
                if k in ("outcome", "note", "resolved_by")})["status"]}))
        out = settlement.settle_sources(
            [f"commitment:{c['id']}", "anomaly:an1", "garbage"],
            outcome="done", note="n", resolved_by="owner")
        assert out == [{"source": f"commitment:{c['id']}",
                        "settled": True, "status": "fulfilled"}]
        assert store.get(c["id"])["status"] == "fulfilled"

    def test_failing_settler_does_not_block_others(self, store):
        c = store.create(person_id="o", description="x")

        def boom(sid, **kw):
            raise RuntimeError("nope")

        settlement.register_settler("anomaly", boom)
        settlement.register_settler(
            "commitment",
            lambda sid, **kw: {"status": store.resolve(sid)["status"]})
        out = settlement.settle_sources(
            ["anomaly:a1", f"commitment:{c['id']}"])
        assert out[0]["settled"] is False and "error" in out[0]
        assert out[1]["settled"] is True
        assert store.get(c["id"])["status"] == "fulfilled"


# --- workspace re-raise suppression ------------------------------------------

class TestResolveSuppression:
    def test_resolved_key_not_recreated_within_ttl(self, ws):
        engine, cs = ws
        c = engine.bump(kind="goal", summary="overdue commitment: recap",
                        dedup_key="commitment:cm1", salience=0.7)
        cs.record_thought(c.concern_id, "owner resolved", resolved=True,
                          salience=0.0)
        again = engine.bump(kind="goal", summary="overdue commitment: recap",
                            dedup_key="commitment:cm1", salience=0.7)
        assert again.concern_id == c.concern_id
        assert again.status == "resolved"
        assert cs.active() == []                     # stays off her mind

    def test_suppression_expires(self, ws, monkeypatch):
        monkeypatch.setenv("COLONY_WORKSPACE_RESOLVED_TTL_HOURS", "1")
        engine, cs = ws
        c = engine.bump(kind="goal", summary="s", dedup_key="k", salience=0.5)
        cs.record_thought(c.concern_id, "done", resolved=True, salience=0.0)
        with cs._lock:
            cs._conn.execute(
                "UPDATE concerns SET last_touched=? WHERE concern_id=?",
                (time.time() - 2 * 3600, c.concern_id))
            cs._conn.commit()
        again = engine.bump(kind="goal", summary="s", dedup_key="k", salience=0.5)
        assert again.concern_id != c.concern_id      # source still open -> back
        assert len(cs.active()) == 1

    def test_resolve_by_dedup(self, ws):
        engine, cs = ws
        c = engine.bump(kind="goal", summary="s", dedup_key="commitment:cm9",
                        salience=0.5)
        n = cs.resolve_by_dedup("commitment:cm9", "commitment fulfilled")
        assert n == 1
        assert cs.get(c.concern_id).status == "resolved"
        assert cs.resolve_by_dedup("commitment:cm9", "again") == 0


# --- API: the full loop, shut ------------------------------------------------

@asynccontextmanager
async def _client(ws_engine, cstore):
    orig_ws, orig_cs = host_mod._workspace, host_mod._commitment_store
    host_mod._workspace = ws_engine
    host_mod._commitment_store = cstore
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._workspace = orig_ws
        host_mod._commitment_store = orig_cs


def _wire_commitment_settler(cstore):
    def _settle(source_id, *, outcome="done", note="", resolved_by="owner"):
        row = cstore.resolve(source_id, outcome=outcome, note=note,
                             resolved_by=resolved_by)
        return {"kind": "commitment", "status": row["status"]} if row else None
    settlement.register_settler("commitment", _settle)


async def test_deck_resolve_settles_commitment_and_stays_resolved(tmp_path):
    """The original bug, end to end: deck resolve must settle the source
    commitment, and the next ingest tick must NOT resurface the concern."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    cs = ConcernStore(db_path=str(tmp_path / "w.db"))
    engine = WorkspaceEngine(cs)
    _wire_commitment_settler(cstore)

    cm = _overdue(cstore)
    concern = engine.bump(kind="goal",
                          summary=f"overdue commitment: {cm['description']}",
                          dedup_key=f"commitment:{cm['id']}", salience=0.7,
                          sources=[f"commitment:{cm['id']}"])

    async with _client(engine, cstore) as c:
        r = await c.post(f"/v1/host/self/workspace/{concern.concern_id}/resolve",
                         json={"note": "owner says handled", "outcome": "done",
                               "resolved_by": "owner"})
        assert r.status_code == 200
        body = r.json()
        assert body["settled_sources"] == [
            {"source": f"commitment:{cm['id']}", "settled": True,
             "kind": "commitment", "status": "fulfilled"}]

    # the source is settled...
    assert cstore.get(cm["id"])["status"] == "fulfilled"
    assert cstore.get_overdue() == []
    # ...and even if an ingest-shaped bump arrives again, nothing resurfaces
    engine.bump(kind="goal", summary="overdue commitment: recap",
                dedup_key=f"commitment:{cm['id']}", salience=0.7,
                sources=[f"commitment:{cm['id']}"])
    assert cs.active() == []


async def test_deck_resolve_invalid_cancels_commitment(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    cs = ConcernStore(db_path=str(tmp_path / "w.db"))
    engine = WorkspaceEngine(cs)
    _wire_commitment_settler(cstore)
    cm = _overdue(cstore)
    concern = engine.bump(kind="goal", summary="x",
                          dedup_key=f"commitment:{cm['id']}", salience=0.7,
                          sources=[f"commitment:{cm['id']}"])
    async with _client(engine, cstore) as c:
        r = await c.post(f"/v1/host/self/workspace/{concern.concern_id}/resolve",
                         json={"outcome": "invalid", "note": "was never a real ask"})
        assert r.status_code == 200
    row = cstore.get(cm["id"])
    assert row["status"] == "cancelled"
    assert row["metadata"]["resolution"]["outcome"] == "invalid"


async def test_patch_outcome_reverse_cascades_to_concern(tmp_path):
    """Resolving the commitment directly (agent tool / MCP) must clear the
    linked workspace concern too."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    cs = ConcernStore(db_path=str(tmp_path / "w.db"))
    engine = WorkspaceEngine(cs)
    cm = _overdue(cstore)
    engine.bump(kind="goal", summary="x",
                dedup_key=f"commitment:{cm['id']}", salience=0.7,
                sources=[f"commitment:{cm['id']}"])
    async with _client(engine, cstore) as c:
        r = await c.patch(f"/v1/host/commitments/{cm['id']}",
                          json={"outcome": "done", "reason": "sent it",
                                "resolved_by": "agent"})
        assert r.status_code == 200
        assert r.json()["status"] == "fulfilled"
    assert cs.active() == []
    row = cstore.get(cm["id"])
    assert row["metadata"]["resolution"]["by"] == "agent"


async def test_create_dedupe_returns_existing(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    first = cstore.create(person_id="owner",
                          description="Send Sam the build recap")
    async with _client(None, cstore) as c:
        r = await c.post("/v1/host/commitments",
                         json={"person_id": "owner",
                               "description": "send sam the build recap",
                               "dedupe": True})
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == first["id"] and body["deduped"] is True
        # without dedupe a twin is created
        r2 = await c.post("/v1/host/commitments",
                          json={"person_id": "owner",
                                "description": "send sam the build recap"})
        assert r2.json()["id"] != first["id"]


async def test_resolution_stats_endpoint(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    a = cstore.create(person_id="o", description="a", source_type="introspection")
    cstore.resolve(a["id"], outcome="invalid", note="bad extraction")
    async with _client(None, cstore) as c:
        r = await c.get("/v1/host/commitments/stats/resolution")
        assert r.status_code == 200
        body = r.json()
        assert body["by_source"]["introspection"]["outcomes"]["invalid"] == 1
        assert body["recent_rejections"][0]["description"] == "a"


# --- introspection dedup ------------------------------------------------------

class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeAsyncClient:
    payload = "[]"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp(self.payload)


async def test_introspection_skips_open_and_rejected_duplicates(tmp_path, monkeypatch):
    """The extractor must not re-record an item that is already open, nor one
    recently rejected as invalid/duplicate — code-enforced, not prompt-hoped."""
    from colony_sidecar.cognition import introspection as intro

    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    existing = cstore.create(person_id="owner",
                             description="Send Sam the build recap")
    monkeypatch.setenv("COLONY_INTROSPECT_MODEL", "fake-model")
    _FakeAsyncClient.payload = (
        '[{"description": "send Sam the build recap", "due_at": null,'
        '  "priority": 70, "source_type": "cognition", "metadata": null},'
        ' {"description": "Water the plants every day", "due_at": null,'
        '  "priority": 40, "source_type": "cognition", "metadata": null},'
        ' {"description": "Email Bob the quarterly report", "due_at": null,'
        '  "priority": 60, "source_type": "cognition", "metadata": null}]')
    monkeypatch.setattr(intro.httpx, "AsyncClient", _FakeAsyncClient)

    out = await intro.run_turn_introspection(
        user_message="did you ever send that recap?",
        assistant_message="Not yet - it is still on my list.",
        conversation_text="",
        person_id="owner",
        existing_commitments=[existing],
        commitment_store=cstore,
        recent_rejections=[{"description": "water the plants", "outcome": "invalid"}],
    )
    assert out["ok"] is True
    assert out["skipped_duplicates"] == 2
    assert len(out["created"]) == 1
    open_now = cstore.get_pending_for_person("owner")
    descs = sorted(c["description"] for c in open_now)
    assert descs == ["Email Bob the quarterly report",
                     "Send Sam the build recap"]
