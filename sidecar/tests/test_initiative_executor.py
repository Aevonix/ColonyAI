"""Tests for the InitiativeExecutorService.

Covers:
- factory: disabled by default, requires reasoning loop + store
- claiming: only claims allowed types, skips others
- execution: completes on success, fails with retry on error
- cycle: processes multiple initiatives per cycle
- stats tracking
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from colony_sidecar.services.initiative_executor import (
    InitiativeExecutorService,
    create_from_env,
    _build_initiative_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class FakeInitiative:
    id: str = "init-1"
    type: str = "follow_up"
    description: str = "Follow up with Alice about the project"
    rationale: str = "No contact in 14 days"
    action_hint: str = ""
    entity_id: str = "contact-alice"
    priority: float = 0.7
    status: str = "pending"
    context: Optional[Dict[str, Any]] = None
    initiative_type: str = ""


class FakeStore:
    def __init__(self, initiatives=None):
        self._initiatives = initiatives or []
        self._assigned = []
        self._completed = []
        self._failed = []

    def list(self, status=None, limit=100):
        if status:
            return [i for i in self._initiatives if i.status in status][:limit]
        return self._initiatives[:limit]

    def assign(self, initiative_id, agent_id, agent_name=None):
        for i in self._initiatives:
            if i.id == initiative_id:
                i.status = "assigned"
                self._assigned.append(initiative_id)
                return i
        return None

    def complete(self, initiative_id, agent_id, result=None, result_metadata=None):
        self._completed.append((initiative_id, result))

    def fail(self, initiative_id, agent_id, reason, retry=False):
        self._failed.append((initiative_id, reason, retry))


@dataclass
class FakeReasoningResult:
    status: str = "completed"
    message: Optional[Dict[str, Any]] = None
    tool_calls: List = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=lambda: {"total_tokens": 42})
    error: Optional[str] = None


class FakeReasoningLoop:
    def __init__(self, result=None):
        self.result = result or FakeReasoningResult(
            message={"role": "assistant", "content": "Done. Followed up with Alice."}
        )
        self.calls = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_factory_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COLONY_EXECUTOR_ENABLED", raising=False)
    result = create_from_env(
        initiative_store=FakeStore(),
        reasoning_loop=FakeReasoningLoop(),
    )
    assert result is None


def test_factory_enabled(monkeypatch):
    monkeypatch.setenv("COLONY_EXECUTOR_ENABLED", "true")
    result = create_from_env(
        initiative_store=FakeStore(),
        reasoning_loop=FakeReasoningLoop(),
    )
    assert result is not None
    assert isinstance(result, InitiativeExecutorService)


def test_factory_needs_reasoning_loop(monkeypatch):
    monkeypatch.setenv("COLONY_EXECUTOR_ENABLED", "true")
    result = create_from_env(
        initiative_store=FakeStore(),
        reasoning_loop=None,
    )
    assert result is None


def test_factory_needs_store(monkeypatch):
    monkeypatch.setenv("COLONY_EXECUTOR_ENABLED", "true")
    result = create_from_env(
        initiative_store=None,
        reasoning_loop=FakeReasoningLoop(),
    )
    assert result is None


def test_factory_custom_types(monkeypatch):
    monkeypatch.setenv("COLONY_EXECUTOR_ENABLED", "true")
    monkeypatch.setenv("COLONY_EXECUTOR_TYPES", "follow_up,commitment")
    svc = create_from_env(
        initiative_store=FakeStore(),
        reasoning_loop=FakeReasoningLoop(),
    )
    assert svc._allowed_types == {"follow_up", "commitment"}


def test_factory_custom_config(monkeypatch):
    monkeypatch.setenv("COLONY_EXECUTOR_ENABLED", "true")
    monkeypatch.setenv("COLONY_EXECUTOR_CYCLE_SECS", "15")
    monkeypatch.setenv("COLONY_EXECUTOR_MAX_PER_CYCLE", "3")
    monkeypatch.setenv("COLONY_EXECUTOR_MODEL_TIER", "medium")
    monkeypatch.setenv("COLONY_EXECUTOR_AGENT_ID", "my-executor")
    svc = create_from_env(
        initiative_store=FakeStore(),
        reasoning_loop=FakeReasoningLoop(),
    )
    assert svc._cycle_secs == 15.0
    assert svc._max_per_cycle == 3
    assert svc._model_tier == "medium"
    assert svc._agent_id == "my-executor"


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def test_build_prompt_includes_fields():
    init = FakeInitiative(
        description="Check in with Bob",
        rationale="Scheduled follow-up",
        action_hint="send_message",
        entity_id="bob-123",
        priority=0.8,
        context={"last_contact": "2026-06-01"},
    )
    prompt = _build_initiative_prompt(init)
    assert "follow_up" in prompt
    assert "Check in with Bob" in prompt
    assert "Scheduled follow-up" in prompt
    assert "send_message" in prompt
    assert "bob-123" in prompt
    assert "0.80" in prompt
    assert "last_contact" in prompt


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_one_success():
    store = FakeStore([FakeInitiative()])
    reasoning = FakeReasoningLoop()
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up"},
    )

    await svc._execute_one(FakeInitiative())
    assert len(reasoning.calls) == 1
    assert store._completed == [("init-1", "Done. Followed up with Alice.")]
    assert svc._stats["initiatives_completed"] == 1
    assert svc._stats["initiatives_processed"] == 1
    assert svc._stats["total_tokens"] == 42


@pytest.mark.asyncio
async def test_execute_one_error():
    store = FakeStore([FakeInitiative()])
    reasoning = FakeReasoningLoop(
        result=FakeReasoningResult(status="error", error="LLM unavailable")
    )
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up"},
    )

    await svc._execute_one(FakeInitiative())
    assert svc._stats["initiatives_failed"] == 1
    assert len(store._failed) == 1
    assert store._failed[0][1] == "LLM unavailable"
    assert store._failed[0][2] is True  # retry=True


class SequenceReasoningLoop:
    """Returns a queued sequence of results, one per run_turn call."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0)


class FakeToolExecutor:
    def __init__(self):
        self.batches = []

    async def execute_batch(self, tool_calls, *, session_id=""):
        self.batches.append(tool_calls)
        return [
            {"tool_call_id": tc.get("id", "x"), "content": "ok"}
            for tc in tool_calls
        ]


@pytest.mark.asyncio
async def test_execute_one_needs_tool_then_completes():
    """needs_tool must drive the tool executor and continue to completion."""
    store = FakeStore([FakeInitiative()])
    reasoning = SequenceReasoningLoop([
        FakeReasoningResult(
            status="needs_tool",
            message={"role": "assistant", "content": "looking up Alice"},
            tool_calls=[{"id": "tc-1", "name": "get_relationship",
                         "arguments": {"entity_id": "contact-alice"}}],
        ),
        FakeReasoningResult(
            status="completed",
            message={"role": "assistant", "content": "Sent the follow-up to Alice."},
        ),
    ])
    tools = FakeToolExecutor()
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        tool_executor=tools,
        allowed_types={"follow_up"},
    )

    await svc._execute_one(FakeInitiative())

    # The pending tool call was executed via the injected executor.
    assert len(tools.batches) == 1
    assert tools.batches[0][0]["name"] == "get_relationship"
    assert svc._stats["total_tool_calls"] == 1
    # run_turn was re-entered after feeding the tool result back.
    assert len(reasoning.calls) == 2
    # Second call carried the assistant tool_calls turn + tool result turn.
    second_msgs = reasoning.calls[1]["messages"]
    roles = [m["role"] for m in second_msgs]
    assert "tool" in roles
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in second_msgs)
    # And the initiative completed (was silently dropped before the fix).
    assert store._completed == [("init-1", "Sent the follow-up to Alice.")]
    assert svc._stats["initiatives_completed"] == 1
    assert svc._stats["initiatives_failed"] == 0


@pytest.mark.asyncio
async def test_execute_one_needs_tool_hits_cap():
    """A model that never stops requesting tools must fail bounded + logged."""
    store = FakeStore([FakeInitiative()])
    reasoning = SequenceReasoningLoop([
        FakeReasoningResult(
            status="needs_tool",
            message={"role": "assistant", "content": "still working"},
            tool_calls=[{"id": f"tc-{n}", "name": "noop", "arguments": {}}],
        )
        for n in range(20)
    ])
    tools = FakeToolExecutor()
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        tool_executor=tools,
        allowed_types={"follow_up"},
        max_tool_iterations=3,
    )

    await svc._execute_one(FakeInitiative())

    assert len(reasoning.calls) == 3  # bounded by the cap
    assert svc._stats["initiatives_failed"] == 1
    assert svc._stats["initiatives_completed"] == 0
    assert "cap reached" in store._failed[0][1]


@pytest.mark.asyncio
async def test_execute_one_unexpected_status_logs_and_fails():
    store = FakeStore([FakeInitiative()])
    reasoning = FakeReasoningLoop(
        result=FakeReasoningResult(status="banana")
    )
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up"},
    )

    await svc._execute_one(FakeInitiative())
    assert svc._stats["initiatives_failed"] == 1
    assert "unexpected status: banana" in store._failed[0][1]


@pytest.mark.asyncio
async def test_execute_one_exception():
    store = FakeStore([FakeInitiative()])
    reasoning = FakeReasoningLoop()
    reasoning.run_turn = AsyncMock(side_effect=RuntimeError("boom"))
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up"},
    )

    await svc._execute_one(FakeInitiative())
    assert svc._stats["initiatives_failed"] == 1
    assert "boom" in store._failed[0][1]


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_filters_by_type():
    store = FakeStore([
        FakeInitiative(id="i1", type="follow_up"),
        FakeInitiative(id="i2", type="agent_action"),
        FakeInitiative(id="i3", type="commitment"),
    ])
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=FakeReasoningLoop(),
        allowed_types={"follow_up", "commitment"},
    )

    claimed = await svc._claim_pending()
    claimed_ids = [c.id for c in claimed]
    assert "i1" in claimed_ids
    assert "i3" in claimed_ids
    assert "i2" not in claimed_ids


@pytest.mark.asyncio
async def test_claim_respects_max_per_cycle():
    initiatives = [FakeInitiative(id=f"i{n}", type="follow_up") for n in range(10)]
    store = FakeStore(initiatives)
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=FakeReasoningLoop(),
        max_per_cycle=3,
        allowed_types={"follow_up"},
    )

    claimed = await svc._claim_pending()
    assert len(claimed) <= 3


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_processes_pending():
    store = FakeStore([
        FakeInitiative(id="i1", type="follow_up"),
        FakeInitiative(id="i2", type="relationship"),
    ])
    reasoning = FakeReasoningLoop()
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up", "relationship"},
    )

    await svc._cycle()
    assert svc._stats["cycles"] == 1
    assert svc._stats["initiatives_processed"] == 2
    assert svc._stats["initiatives_completed"] == 2
    assert len(reasoning.calls) == 2


@pytest.mark.asyncio
async def test_cycle_no_pending():
    store = FakeStore([])
    reasoning = FakeReasoningLoop()
    svc = InitiativeExecutorService(
        initiative_store=store,
        reasoning_loop=reasoning,
        allowed_types={"follow_up"},
    )

    await svc._cycle()
    assert svc._stats["cycles"] == 1
    assert svc._stats["initiatives_processed"] == 0
    assert len(reasoning.calls) == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_stats_property():
    svc = InitiativeExecutorService(
        initiative_store=FakeStore(),
        reasoning_loop=FakeReasoningLoop(),
    )
    stats = svc.stats
    assert isinstance(stats, dict)
    assert "cycles" in stats
    assert "initiatives_completed" in stats
    assert "initiatives_failed" in stats
    assert "total_tokens" in stats
