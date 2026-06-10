"""Autonomy-loop wiring of the self-directed thinking phase (v0.17.0)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop


def _payload():
    return json.dumps([{"title": "Research vector index compaction",
                        "type": "research", "priority": 0.7,
                        "rationale": "memory growth observed"}])


class FakeRouter:
    def __init__(self, content):
        self._content = content

    async def complete(self, messages, **kwargs):
        return SimpleNamespace(content=self._content)


def _loop_with_router(router):
    registry = MagicMock()
    registry.llm_router = router
    registry.goals = None
    loop = AutonomyLoop(registry=registry)
    loop._pending_initiatives = []
    return loop


@pytest.mark.asyncio
async def test_phase_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COLONY_ENABLE_INTERNAL_THINKING", raising=False)
    loop = _loop_with_router(FakeRouter(_payload()))
    await loop._phase_thinking()
    assert loop._pending_initiatives == []
    assert getattr(loop, "_thinker", None) is None


@pytest.mark.asyncio
async def test_phase_appends_to_pending_batch(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_INTERNAL_THINKING", "true")
    loop = _loop_with_router(FakeRouter(_payload()))
    existing = SimpleNamespace(description="existing", priority=0.6)
    loop._pending_initiatives = [existing]
    await loop._phase_thinking()
    assert len(loop._pending_initiatives) == 2
    assert loop._pending_initiatives[0] is existing
    novel = loop._pending_initiatives[1]
    assert novel.action_hint is None
    assert novel.dedup_key.startswith("thinking:")


@pytest.mark.asyncio
async def test_phase_respects_cadence(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_INTERNAL_THINKING", "true")
    monkeypatch.setenv("COLONY_THINKING_INTERVAL_SECS", "3600")
    loop = _loop_with_router(FakeRouter(_payload()))
    await loop._phase_thinking()
    first_count = len(loop._pending_initiatives)
    await loop._phase_thinking()  # immediately again — not due
    assert len(loop._pending_initiatives) == first_count


@pytest.mark.asyncio
async def test_phase_safe_without_router(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_INTERNAL_THINKING", "true")
    loop = _loop_with_router(None)
    await loop._phase_thinking()
    assert loop._pending_initiatives == []


def test_situation_report_shape():
    registry = MagicMock()
    registry.goals = None
    loop = AutonomyLoop(registry=registry)
    loop._last_initiative_context = {
        "pending_tasks": [{"entity_id": "g1"}] * 15,
        "irrelevant": ["x"],
    }
    loop._pending_initiatives = [SimpleNamespace(description="d1")]
    situation = loop._build_thinking_situation()
    assert len(situation["pending_tasks"]) == 10
    assert situation["current_initiatives"] == ["d1"]
    assert "irrelevant" not in situation
