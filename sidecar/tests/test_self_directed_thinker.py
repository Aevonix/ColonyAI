"""Self-directed thinking component (v0.17.0)."""

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.intelligence.components.initiative_engine import InitiativeType
from colony_sidecar.intelligence.components.self_directed_thinker import (
    SelfDirectedThinker,
)


class FakeRouter:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


def _payload(n=2):
    return json.dumps([
        {"title": f"Investigate stalled goal {i}", "type": "task",
         "priority": 0.7, "rationale": "goal has been blocked 3 days"}
        for i in range(n)
    ])


@pytest.mark.asyncio
async def test_think_produces_initiatives():
    thinker = SelfDirectedThinker(FakeRouter(_payload(2)), interval_secs=0)
    out = await thinker.think({"goals": [{"id": "g1", "status": "blocked"}]})
    assert len(out) == 2
    assert out[0].type == InitiativeType.TASK
    assert out[0].action_hint is None
    assert out[0].dedup_key.startswith("thinking:")
    assert out[0].rationale.startswith("[self-directed thinking]")


@pytest.mark.asyncio
async def test_priority_capped_and_types_validated():
    content = json.dumps([
        {"title": "Do something huge", "type": "task", "priority": 1.0,
         "rationale": "r"},
        {"title": "Forbidden direct action", "type": "agent_action",
         "priority": 0.5, "rationale": "r"},
        {"title": "", "type": "task", "priority": 0.5, "rationale": "r"},
    ])
    thinker = SelfDirectedThinker(FakeRouter(content))
    out = await thinker.think({})
    assert len(out) == 1
    assert out[0].priority <= 0.85


@pytest.mark.asyncio
async def test_max_per_cycle_enforced():
    thinker = SelfDirectedThinker(FakeRouter(_payload(10)), max_per_cycle=3)
    out = await thinker.think({})
    assert len(out) == 3


@pytest.mark.asyncio
async def test_dedup_across_cycles():
    router = FakeRouter(_payload(1))
    thinker = SelfDirectedThinker(router)
    first = await thinker.think({})
    second = await thinker.think({})
    assert len(first) == 1 and len(second) == 0


@pytest.mark.asyncio
async def test_markdown_fenced_and_garbage_output():
    fenced = "Sure! Here you go:\n```json\n" + _payload(1) + "\n```"
    assert len(await SelfDirectedThinker(FakeRouter(fenced)).think({})) == 1
    assert await SelfDirectedThinker(FakeRouter("no json here")).think({}) == []
    assert await SelfDirectedThinker(FakeRouter("[{broken")).think({}) == []


@pytest.mark.asyncio
async def test_router_failure_is_safe():
    class ExplodingRouter:
        async def complete(self, *a, **k):
            raise RuntimeError("llm down")

    assert await SelfDirectedThinker(ExplodingRouter()).think({}) == []


def test_due_interval():
    thinker = SelfDirectedThinker(FakeRouter("[]"), interval_secs=100)
    assert thinker.due(now=1000.0)
    thinker.mark_ran(now=1000.0)
    assert not thinker.due(now=1050.0)
    assert thinker.due(now=1101.0)
