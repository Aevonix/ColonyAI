"""Tests: escalation miner + training-corpus exporter."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from colony_sidecar.mining import (
    EscalationMiner,
    EscalationRecord,
    MinedTurn,
    MiningStore,
    export_corpus,
)


@pytest.fixture
def store(tmp_path: Path) -> MiningStore:
    return MiningStore(db_path=str(tmp_path / "colony-mining.db"))


@pytest.fixture
def miner(store: MiningStore, monkeypatch) -> EscalationMiner:
    monkeypatch.setenv("COLONY_ESCALATION_MINING", "shadow")
    return EscalationMiner(store)


def _observe(miner, *, user="please fix the bridge", assistant="done",
             tools=None, model="", session="s1", contact="owner",
             channel="whatsapp:g1"):
    return miner.observe_turn(
        session_id=session, contact_id=contact, channel_id=channel,
        user_text=user, assistant_text=assistant,
        summary=f"User: {user}\nAgent: {assistant}",
        tools_used=tools or [], model=model,
    )


class FakeRouter:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


# -- store ---------------------------------------------------------------------

def test_store_roundtrip(store):
    t = MinedTurn(session_id="s1", contact_id="c1", channel_id="ch",
                  user_text="u", assistant_text="a", tools_used=["terminal"])
    store.add_turn(t)
    got = store.list_turns(contact_id="c1")
    assert len(got) == 1 and got[0].tools_used == ["terminal"]
    e = EscalationRecord(kind="consultation", session_id="s1",
                         task_context="ctx", escalated_answer="ans")
    store.add_escalation(e)
    assert store.list_escalations()[0].id == e.id
    assert store.escalation_stats()["total"] == 1


def test_store_filters(store):
    now = time.time()
    for i, ch in enumerate(["whatsapp:a", "rcs:b", "whatsapp:a"]):
        store.add_turn(MinedTurn(session_id=f"s{i}", contact_id="owner",
                                 channel_id=ch, user_text="u", assistant_text="a",
                                 ts=now - i * 3600))
    assert len(store.list_turns(channels=["whatsapp:a"])) == 2
    assert len(store.list_turns(since_ts=now - 1800)) == 1
    assert len(store.list_turns(contact_id="nobody")) == 0
    assert len(store.list_turns(contact_id="*")) == 3


# -- miner detection -------------------------------------------------------------

def test_mode_off_banks_nothing(store, monkeypatch):
    monkeypatch.setenv("COLONY_ESCALATION_MINING", "off")
    m = EscalationMiner(store)
    assert _observe(m, tools=["terminal"], assistant="ran claude -p fix") is None
    assert store.turn_count() == 0


def test_consultation_detected(miner, store):
    rec = _observe(
        miner, user="the gateway is broken, escalate if needed",
        assistant="I consulted a build agent: claude -p 'fix the adapter' and applied its fix",
        tools=["terminal"],
    )
    assert rec is not None and rec.kind == "consultation"
    assert "claude -p" in rec.matched
    assert store.escalation_stats()["total"] == 1


def test_consultation_needs_terminal_tool(miner):
    rec = _observe(miner, assistant="I could run claude -p but did not", tools=[])
    assert rec is None


def test_provider_escalation(store, monkeypatch):
    monkeypatch.setenv("COLONY_ESCALATION_MINING", "shadow")
    monkeypatch.setenv("COLONY_ESCALATION_HEAVY_RE", r"glm-5|opus")
    m = EscalationMiner(store)
    rec = _observe(m, model="glm-5.2-cloud", assistant="heavy answer")
    assert rec is not None and rec.kind == "provider_escalation"
    assert rec.model == "glm-5.2-cloud"
    monkeypatch.delenv("COLONY_ESCALATION_HEAVY_RE")


def test_provider_detector_off_by_default(miner):
    assert _observe(miner, model="some-huge-model") is None


def test_local_attempt_and_outcome_followup(miner, store):
    _observe(miner, user="fix X", assistant="local try that failed", session="s9")
    rec = _observe(miner, user="still broken, escalate",
                   assistant="claude -p run; fixed properly", tools=["terminal"],
                   session="s9")
    assert rec.local_attempt == "local try that failed"
    assert store.latest_open_escalation("s9") is not None
    _observe(miner, user="perfect, that worked", assistant="great", session="s9")
    got = store.list_escalations()[0]
    assert got.outcome == "followed_up"
    assert "perfect" in got.outcome_note


def test_live_mode_feeds_distiller(store, monkeypatch):
    monkeypatch.setenv("COLONY_ESCALATION_MINING", "live")
    monkeypatch.setenv("COLONY_SKILLS_DISTILL", "live")
    from colony_sidecar.skills_memory import SkillStore
    skills = SkillStore()
    router = FakeRouter(json.dumps({
        "title": "Escalate adapter fixes",
        "situation": "gateway adapter breaks after framework update",
        "steps": ["diagnose", "consult build agent", "apply patch"],
        "gotchas": ["check signatures"],
    }))
    m = EscalationMiner(store, skill_store=skills, router_getter=lambda: router)
    rec = _observe(m, assistant="claude -p 'fix'; applied", tools=["terminal"])
    assert rec is not None
    # _feed_distiller runs the coroutine via asyncio.run in sync context
    assert len(router.calls) == 1
    assert skills.count() == 1
    assert store.list_escalations()[0].distilled == 1


def test_shadow_mode_does_not_distill(store, monkeypatch):
    monkeypatch.setenv("COLONY_ESCALATION_MINING", "shadow")
    router = FakeRouter("{}")
    m = EscalationMiner(store, skill_store=object(), router_getter=lambda: router)
    _observe(m, assistant="claude -p x", tools=["terminal"])
    assert router.calls == []


# -- corpus export -----------------------------------------------------------------

def _seed_turns(store, n=25, contact="owner", channel="whatsapp:g1"):
    now = time.time()
    for i in range(n):
        store.add_turn(MinedTurn(
            session_id=f"sess-{i % 5}", contact_id=contact, channel_id=channel,
            user_text=f"question number {i} about topic {i % 7}?",
            assistant_text=f"answer number {i} with substance and detail.",
            ts=now - (n - i) * 60,
        ))


def test_export_turn_grouping_shape(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    _seed_turns(store, 25)
    stats = export_corpus(store, state_dir=tmp_path)
    assert stats["rows"] == 25 and stats["sessions"] == 5
    lines = open(stats["path"]).read().splitlines()
    assert len(lines) == 25
    for ln in lines:
        row = json.loads(ln)
        conv = row["conversations"]
        assert conv[0]["role"] == "user"          # GeneralParser contract
        assert conv[1]["role"] == "assistant"
        assert all(isinstance(m["content"], str) and m["content"] for m in conv)
    assert Path(stats["path"]).parent.name == "exports"


def test_export_session_grouping(store, tmp_path):
    _seed_turns(store, 10)
    stats = export_corpus(store, state_dir=tmp_path, group="session")
    assert stats["rows"] == 5
    row = json.loads(open(stats["path"]).read().splitlines()[0])
    roles = [m["role"] for m in row["conversations"]]
    assert roles[0] == "user"
    assert roles == [("user" if i % 2 == 0 else "assistant") for i in range(len(roles))]


def test_export_filters_quality_cron_dedup(store, tmp_path):
    _seed_turns(store, 4)
    # cron session excluded by default
    store.add_turn(MinedTurn(session_id="cron_x", contact_id="owner",
                             channel_id="whatsapp:g1", user_text="tick",
                             assistant_text="tock"))
    # system-origin excluded
    store.add_turn(MinedTurn(session_id="sess-0", contact_id="owner",
                             channel_id="whatsapp:g1",
                             user_text="[SYSTEM NOTE: automated] invoked the daily skill",
                             assistant_text="ok"))
    # duplicate exchange deduplicated
    store.add_turn(MinedTurn(session_id="sess-1", contact_id="owner",
                             channel_id="whatsapp:g1",
                             user_text="question number 0 about topic 0?",
                             assistant_text="answer number 0 with substance and detail."))
    # foreign contact excluded by owner-only default
    store.add_turn(MinedTurn(session_id="sess-2", contact_id="stranger",
                             channel_id="whatsapp:g1", user_text="hi hi",
                             assistant_text="hello there"))
    stats = export_corpus(store, state_dir=tmp_path)
    assert stats["rows"] == 4
    assert stats["skipped"]["cron"] == 1
    assert stats["skipped"]["dedup"] == 1
    assert stats["skipped"]["quality"] >= 1


def test_export_strips_rc_marker_and_redacts(store, tmp_path):
    store.add_turn(MinedTurn(
        session_id="s", contact_id="owner", channel_id="c",
        user_text='[replying to you: "earlier"] [[rc id=ABC123]]\nyes do it',
        assistant_text="done, the token was sk-1234567890abcdef1234567890abcdef",
    ))
    stats = export_corpus(store, state_dir=tmp_path, redact=True)
    row = json.loads(open(stats["path"]).read().splitlines()[0])
    assert "[[rc" not in row["conversations"][0]["content"]
    assert "replying to" in row["conversations"][0]["content"]


def test_export_includes_escalations(store, tmp_path):
    _seed_turns(store, 2)
    store.add_escalation(EscalationRecord(
        kind="consultation", session_id="sess-0", contact_id="owner",
        channel_id="whatsapp:g1", task_context="hard task needing escalation",
        escalated_answer="the escalated, correct answer", matched="claude -p"))
    stats = export_corpus(store, state_dir=tmp_path, include_escalations=True)
    assert stats["escalations_included"] == 1
    rows = [json.loads(l) for l in open(stats["path"]).read().splitlines()]
    esc = [r for r in rows if r["id"].startswith("escalation-")]
    assert len(esc) == 1 and esc[0]["meta"]["kind"] == "consultation"


def test_export_time_filters_and_filename_safety(store, tmp_path):
    _seed_turns(store, 5)
    stats = export_corpus(store, state_dir=tmp_path, since="1h",
                          filename="../../evil.jsonl")
    assert stats["rows"] == 5
    assert "/exports/evil.jsonl" in stats["path"]
    with pytest.raises(ValueError):
        export_corpus(store, state_dir=tmp_path, since="not-a-time")


# -- API router -----------------------------------------------------------------

class TestMiningApi:
    @pytest.fixture
    def client(self, store, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import colony_sidecar.api.routers.mining as mining_router

        monkeypatch.setenv("COLONY_ESCALATION_MINING", "shadow")
        app = FastAPI()
        app.include_router(mining_router.router)
        engine = EscalationMiner(store)
        mining_router.set_mining(store, engine, tmp_path)
        yield TestClient(app)
        mining_router.set_mining(None, None, None)

    def test_escalations_endpoint(self, client, store):
        store.add_escalation(EscalationRecord(kind="consultation",
                                              task_context="x", escalated_answer="y"))
        r = client.get("/v1/host/mining/escalations")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["total"] == 1
        assert body["escalations"][0]["kind"] == "consultation"

    def test_export_endpoint(self, client, store):
        _seed_turns(store, 3)
        r = client.post("/v1/host/mining/corpus/export", json={"group": "turn"})
        assert r.status_code == 200
        assert r.json()["rows"] == 3

    def test_export_endpoint_gate(self, client, monkeypatch):
        monkeypatch.setenv("COLONY_CORPUS_EXPORT_ENABLED", "false")
        r = client.post("/v1/host/mining/corpus/export", json={})
        assert r.status_code == 403

    def test_uninitialized_501(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import colony_sidecar.api.routers.mining as mining_router
        app = FastAPI()
        app.include_router(mining_router.router)
        mining_router.set_mining(None, None, None)
        c = TestClient(app)
        assert c.get("/v1/host/mining/escalations").status_code == 501
