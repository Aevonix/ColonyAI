"""Tests for Phase 2 — sidecar subsystems broadcast typed events."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.events import broadcaster
from colony_sidecar.events.broadcaster import emit, reset_broadcaster_for_tests


class _Collector:
    """Captures emitted events for assertions."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


@pytest.fixture
def collector():
    c = _Collector()
    reset_broadcaster_for_tests(c)
    yield c
    reset_broadcaster_for_tests(None)
    broadcaster._broadcast_fn = None  # force lazy-import on next call


def test_emit_wraps_payload_with_type_and_timestamp(collector):
    emit("custom", {"foo": 1})
    assert len(collector.events) == 1
    e = collector.events[0]
    assert e["type"] == "custom"
    assert e["payload"] == {"foo": 1}
    assert "occurred_at" in e


def test_emit_with_no_payload_defaults_to_empty_dict(collector):
    emit("pinged")
    assert collector.events[0]["payload"] == {}


def test_emit_swallows_broadcaster_exceptions(collector):
    def _boom(_event):
        raise RuntimeError("sink dead")
    reset_broadcaster_for_tests(_boom)
    emit("custom", {"foo": 1})  # must not raise


@pytest.mark.asyncio
async def test_consolidator_emits_memory_consolidated(collector):
    from colony_sidecar.intelligence.graph.consolidator import MemoryConsolidator

    class _FakeGraph:
        async def execute(self, *_args, **_kwargs):
            return []

    consolidator = MemoryConsolidator(graph_client=_FakeGraph())
    # Override the private helper to return an empty candidate list so the
    # consolidator takes the no-merge path but still broadcasts on exit.
    consolidator._fetch_recent_memories = lambda: _empty_list()
    consolidator._detect_conflicts = lambda: _empty_list()

    await consolidator.run()
    types = [e["type"] for e in collector.events]
    assert "memory_consolidated" in types
    event = next(e for e in collector.events if e["type"] == "memory_consolidated")
    assert "examined" in event["payload"]
    assert "merged" in event["payload"]


async def _empty_list():
    return []


def test_briefing_save_broadcasts_briefing(collector, tmp_path, monkeypatch):
    from colony_sidecar.briefings.store import BriefingStore
    from colony_sidecar.briefings.models import (
        Briefing, BriefingPriority, BriefingStatus, BriefingType,
    )

    store = BriefingStore(str(tmp_path / "b.db"))
    briefing = Briefing(
        briefing_id="b1",
        briefing_type=BriefingType.DAILY,
        status=BriefingStatus.DRAFT,
        priority=BriefingPriority.NORMAL,
        sections=[],
        triggered_by="test",
        gateway=None,
        created_at=datetime.now(timezone.utc),
    )
    store.save(briefing)

    types = [e["type"] for e in collector.events]
    assert "briefing" in types
    payload = next(e for e in collector.events if e["type"] == "briefing")["payload"]
    assert payload["briefing_id"] == "b1"
    assert payload["priority"] == "normal"


def test_goal_save_broadcasts_goal_update(collector, tmp_path):
    from colony_sidecar.goals.store import GoalStore
    from colony_sidecar.goals.models import (
        Goal, GoalPriority, GoalSource, GoalStatus,
    )

    store = GoalStore(db_path=str(tmp_path / "goals.db"))
    goal = Goal(
        goal_id="g1",
        title="Test goal",
        description="",
        source=GoalSource.EXPLICIT,
        status=GoalStatus.ACTIVE,
        priority=GoalPriority.NORMAL,
        outcome=None,
        progress_pct=0.5,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    store.save_goal(goal)

    types = [e["type"] for e in collector.events]
    assert "goal_update" in types
    payload = next(e for e in collector.events if e["type"] == "goal_update")["payload"]
    assert payload["goal_id"] == "g1"
    assert payload["status"] == "active"
    assert payload["progress_pct"] == 0.5


@pytest.mark.asyncio
async def test_world_model_upsert_entity_broadcasts(collector):
    from colony_sidecar.world_model.store import WorldModelStore

    class _FakeBackend:
        async def upsert_entity(self, e):
            return e

    store = WorldModelStore()
    store._backend = _FakeBackend()
    entity = SimpleNamespace(id="ent-1", name="Alice")
    await store.upsert_entity(entity)
    types = [e["type"] for e in collector.events]
    assert "world_model_changed" in types
    payload = next(e for e in collector.events if e["type"] == "world_model_changed")["payload"]
    assert payload["change_type"] == "entity_upsert"
    assert payload["entity_id"] == "ent-1"


@pytest.mark.asyncio
async def test_skill_approve_broadcasts_skill_draft_approved(collector):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from colony_sidecar.api.routers import host as host_mod

    class _FakeRegistry:
        def __init__(self):
            self._skills = {
                "s1": SimpleNamespace(skill_id="s1", name="MySkill"),
            }

        async def get(self, sid):
            return self._skills.get(sid)

        async def activate(self, _sid):
            return None

    prev = host_mod._skills_registry
    host_mod._skills_registry = _FakeRegistry()
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/v1/host/skills/s1/approve")
        assert resp.status_code == 200
    finally:
        host_mod._skills_registry = prev

    types = [e["type"] for e in collector.events]
    assert "skill_draft_approved" in types
    payload = next(
        e for e in collector.events if e["type"] == "skill_draft_approved"
    )["payload"]
    assert payload["skill_id"] == "s1"
    assert payload["name"] == "MySkill"
