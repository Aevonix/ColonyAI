"""Projects: planner validation, engine pursuit, boundaries, replan (item 1)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.directives import DirectiveManager, DirectiveStore
from colony_sidecar.projects import (
    Project, ProjectEngine, ProjectStore, Step, plan_project, validate_steps,
)
from colony_sidecar.proposals import ProposalStore


class FakeRouter:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


def _plan_json():
    return json.dumps([
        {"ordinal": 1, "description": "Gather existing notes on the topic",
         "action_kind": "analyze", "depends_on": []},
        {"ordinal": 2, "description": "Research current best practice",
         "action_kind": "research", "depends_on": [1]},
        {"ordinal": 3, "description": "Deliver a summary to the owner",
         "action_kind": "deliver", "depends_on": [2]},
    ])


# ---------------------------------------------------------------------------
# Planner validation (LLM proposes, code decides)
# ---------------------------------------------------------------------------

def test_unknown_action_kind_dropped():
    steps = validate_steps([
        {"ordinal": 1, "description": "ok step", "action_kind": "analyze"},
        {"ordinal": 2, "description": "bad step", "action_kind": "launch_missiles"},
    ])
    assert len(steps) == 1 and steps[0].action_kind == "analyze"


def test_dependency_cycle_broken():
    steps = validate_steps([
        {"ordinal": 1, "description": "a", "action_kind": "analyze",
         "depends_on": [2]},
        {"ordinal": 2, "description": "b", "action_kind": "analyze",
         "depends_on": [1]},
    ])
    assert len(steps) == 2
    # after cycle-breaking + renumber the first step has no deps
    assert steps[0].depends_on == []


def test_step_cap_and_renumber():
    raw = [{"ordinal": i, "description": f"step {i}", "action_kind": "analyze",
            "depends_on": []} for i in range(1, 30)]
    steps = validate_steps(raw, max_steps=5)
    assert len(steps) == 5
    assert [s.ordinal for s in steps] == [1, 2, 3, 4, 5]


def test_self_and_unknown_deps_stripped():
    steps = validate_steps([
        {"ordinal": 1, "description": "a", "action_kind": "analyze",
         "depends_on": [1, 99]},
    ])
    assert steps[0].depends_on == []


@pytest.mark.asyncio
async def test_plan_project_parses_and_validates():
    steps = await plan_project(FakeRouter(_plan_json()), "learn a topic",
                               project_id="proj-x")
    assert [s.action_kind for s in steps] == ["analyze", "research", "deliver"]
    assert steps[1].depends_on == [1]
    assert all(s.project_id == "proj-x" for s in steps)


@pytest.mark.asyncio
async def test_plan_project_garbage_yields_empty():
    assert await plan_project(FakeRouter("no json here"), "objective") == []


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    db = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db)
    p = Project(title="t", objective="o", source="owner")
    store.save_project(p)
    store.save_step(Step(project_id=p.id, ordinal=1, description="d",
                         action_kind="analyze", depends_on=[]))
    # fresh handle sees the same state (restart survival)
    store2 = ProjectStore(db_path=db)
    got = store2.get_project(p.id)
    assert got is not None and got.title == "t"
    steps = store2.steps_for(p.id)
    assert len(steps) == 1 and steps[0].action_kind == "analyze"


def test_due_for_review_filters():
    store = ProjectStore()
    due = Project(title="due", status="active", next_review_at=0.0)
    later = Project(title="later", status="active",
                    next_review_at=9999999999.0)
    store.save_project(due)
    store.save_project(later)
    ids = {p.id for p in store.due_for_review()}
    assert due.id in ids and later.id not in ids


# ---------------------------------------------------------------------------
# Engine behavior (shadow mode = calibration)
# ---------------------------------------------------------------------------

def _engine(monkeypatch, router=None, dm=None, deliver=None,
            proposals=None, mode="shadow"):
    monkeypatch.setenv("COLONY_PROJECTS_MODE", mode)
    monkeypatch.setenv("COLONY_PROJECTS_REVIEW_SECS", "30")
    return ProjectEngine(
        ProjectStore(),
        directive_manager=dm,
        llm_router=router,
        proposal_store=proposals,
        delivery_router=deliver,
    )


@pytest.mark.asyncio
async def test_shadow_plans_and_simulates_full_sequence(monkeypatch):
    proposals = ProposalStore()
    sent = []

    async def deliver(payload):
        sent.append(payload)
        return True

    engine = _engine(monkeypatch, router=FakeRouter(_plan_json()),
                     proposals=proposals, deliver=deliver)
    project, reason = engine.create_project("learn a topic end to end")
    assert project is not None and reason == "ok"

    await engine.tick()          # plans (planning -> active) + first step
    steps = engine.store.steps_for(project.id)
    assert len(steps) == 3
    assert steps[0].status == "done" and steps[0].result.startswith("SHADOW")

    # advance the remaining steps (review timer respected via monkeypatched 0)
    monkeypatch.setenv("COLONY_PROJECTS_REVIEW_SECS", "30")
    p = engine.store.get_project(project.id)
    for _ in range(4):
        p.next_review_at = 0.0
        engine.store.save_project(p)
        await engine.tick()
        p = engine.store.get_project(project.id)
    assert p.status == "completed"

    # milestone proposal stored in SHADOW, delivery router NEVER called
    assert sent == []
    stored = proposals.list()
    assert any("Project completed" in x.title for x in stored)
    assert all(x.status == "shadow" for x in stored)


@pytest.mark.asyncio
async def test_dependency_ordering_selects_ready_step(monkeypatch):
    engine = _engine(monkeypatch)
    p = Project(title="t", objective="o", status="active")
    engine.store.save_project(p)
    engine.store.save_step(Step(project_id=p.id, ordinal=1, description="one",
                                action_kind="analyze"))
    engine.store.save_step(Step(project_id=p.id, ordinal=2, description="two",
                                action_kind="analyze", depends_on=[1]))
    await engine.tick()
    steps = {s.ordinal: s for s in engine.store.steps_for(p.id)}
    assert steps[1].status == "done"       # step 1 ran first
    assert steps[2].status == "pending"    # step 2 waited for its dep


@pytest.mark.asyncio
async def test_boundary_blocked_step_blocks_project(monkeypatch):
    dm = DirectiveManager(DirectiveStore())
    dm.add_explicit("the billing spreadsheet", polarity="prohibit",
                    raw_text="leave the billing spreadsheet alone")
    proposals = ProposalStore()
    engine = _engine(monkeypatch, dm=dm, proposals=proposals)
    p = Project(title="billing cleanup", objective="tidy billing",
                status="active")
    engine.store.save_project(p)
    engine.store.save_step(Step(project_id=p.id, ordinal=1,
                                description="analyze the billing spreadsheet",
                                action_kind="analyze"))
    await engine.tick()
    got = engine.store.get_project(p.id)
    assert got.status == "blocked"
    assert "boundary" in got.reason or "billing" in got.reason
    assert any("blocked" in x.title for x in proposals.list())


def test_create_project_refused_by_boundary(monkeypatch):
    dm = DirectiveManager(DirectiveStore())
    dm.add_explicit("the acme migration", polarity="prohibit",
                    raw_text="don't touch the acme migration")
    engine = _engine(monkeypatch, dm=dm)
    project, reason = engine.create_project("plan the acme migration rollout")
    assert project is None and "boundary" in reason


@pytest.mark.asyncio
async def test_replan_on_failure_bounded(monkeypatch):
    monkeypatch.setenv("COLONY_PROJECTS_MAX_REPLANS", "1")
    # replanner returns nothing usable -> replan produces no steps
    engine = _engine(monkeypatch, router=FakeRouter("[]"))
    p = Project(title="t", objective="o", status="active")
    engine.store.save_project(p)
    engine.store.save_step(Step(project_id=p.id, ordinal=1, description="d",
                                action_kind="analyze", status="failed",
                                result="boom"))
    await engine.tick()      # replan 1: no steps + nothing done -> abandoned
    got = engine.store.get_project(p.id)
    assert got.status == "abandoned"
    assert got.replans == 1


@pytest.mark.asyncio
async def test_planning_failure_eventually_abandons(monkeypatch):
    monkeypatch.setenv("COLONY_PROJECTS_MAX_REPLANS", "0")
    engine = _engine(monkeypatch, router=FakeRouter("garbage"))
    project, _ = engine.create_project("do something useful")
    await engine.tick()
    got = engine.store.get_project(project.id)
    assert got.status == "abandoned" and got.reason == "planning_failed"


@pytest.mark.asyncio
async def test_off_mode_does_nothing(monkeypatch):
    engine = _engine(monkeypatch, mode="off")
    report = await engine.tick()
    assert report == {"mode": "off", "adopted": 0, "planned": 0,
                      "steps_dispatched": 0, "deferred": False}


@pytest.mark.asyncio
async def test_trust_graduation_lifts_shadow(monkeypatch):
    """Env shadow is a calibration stage: a graduated trust domain makes the
    engine pursue for real (Amendment 1.2)."""
    from colony_sidecar.self_model import (
        ActionJournal, CompetenceStore, SelfModel, TrustEngine,
    )
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "shadow")
    store = CompetenceStore()
    trust = TrustEngine(store, journal=ActionJournal())
    sm = SelfModel(store, trust=trust)
    engine = _engine(monkeypatch)
    engine._self_model = sm
    assert engine._effective_mode() == "shadow"
    trust.set_stage("project", "ask_first", notify=False)
    assert engine._effective_mode() == "live"
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "off")   # owner override wins
    assert engine._effective_mode() == "off"


class _FakeInitiativeStore:
    def __init__(self):
        self.completed = []
        self._items = [SimpleNamespace(
            id="init-1", description="Build a knowledge base on topic X.",
            rationale="it keeps coming up")]

    def list(self, status=None, type=None, limit=10):
        return list(self._items)

    def complete(self, iid, agent_id="", result=""):
        self.completed.append((iid, result))


@pytest.mark.asyncio
async def test_adopts_project_initiative(monkeypatch):
    fake = _FakeInitiativeStore()
    engine = _engine(monkeypatch, router=FakeRouter(_plan_json()))
    engine._initiatives = fake
    report = await engine.tick()
    assert report["adopted"] == 1
    assert fake.completed and "adopted as project" in fake.completed[0][1]
    projects = engine.store.list_projects()
    assert any(p.source == "thinker" for p in projects)
