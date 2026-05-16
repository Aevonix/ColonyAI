"""Unit tests for ConversationSynthesisTask."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.autonomy.synthesis import (
    ConversationSynthesisTask,
    SynthesisState,
    _parse_turn_content,
)
from colony_sidecar.goals.inference import ConversationMessage, IntentSignal


# ── Parse turn content ───────────────────────────────────────────────────────


def test_parse_turn_content_standard():
    content = "User: I need to finish the report by Friday\nAssistant: I'll help you track that."
    messages = _parse_turn_content(content)
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert "finish the report" in messages[0].content
    assert messages[1].role == "assistant"
    assert "help you track" in messages[1].content


def test_parse_turn_content_multiline():
    content = "User: I want to\nplan a trip\nAssistant: Where would you like to go?"
    messages = _parse_turn_content(content)
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert "plan a trip" in messages[0].content


def test_parse_turn_content_fallback():
    content = "Just some raw text without prefixes"
    messages = _parse_turn_content(content)
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == content


def test_parse_turn_content_empty():
    assert _parse_turn_content("") == []
    assert _parse_turn_content("   ") == []


# ── State persistence ───────────────────────────────────────────────────────


def test_state_load_save():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        state = SynthesisState(
            last_processed_at="2024-01-01T00:00:00Z",
            memories_processed=5,
            goals_created=2,
        )
        state.save(path)

        loaded = SynthesisState.load(path)
        assert loaded.last_processed_at == "2024-01-01T00:00:00Z"
        assert loaded.memories_processed == 5
        assert loaded.goals_created == 2


def test_state_load_missing():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nonexistent.json"
        loaded = SynthesisState.load(path)
        assert loaded.last_processed_at is None
        assert loaded.memories_processed == 0


def test_state_load_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text("not json")
        loaded = SynthesisState.load(path)
        assert loaded.last_processed_at is None
        assert loaded.memories_processed == 0


# ── Synthesis task ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_no_graph():
    registry = MagicMock()
    registry.graph = None
    task = ConversationSynthesisTask(registry=registry)
    result = await task.run()
    assert result["memories_scanned"] == 0
    assert result["error"] == "no_graph"


@pytest.mark.asyncio
async def test_synthesis_no_goals():
    registry = MagicMock()
    registry.graph = MagicMock()
    registry.goals = None
    task = ConversationSynthesisTask(registry=registry)
    result = await task.run()
    assert result["memories_scanned"] == 0
    assert result["error"] == "no_goals"


@pytest.mark.asyncio
async def test_synthesis_finds_and_creates_goals():
    """End-to-end: memory with goal-like language → candidate → goal created."""
    registry = MagicMock()

    # Mock graph with one episodic memory containing goal-like language
    mock_memory = {
        "id": "mem-1",
        "content": "User: I need to finish the quarterly report by Friday\nAssistant: I'll remind you.",
        "created_at": "2024-06-15T10:00:00Z",
        "strength": 0.8,
    }

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def run(self, query, **params):
            class FakeResult:
                async def __aiter__(self):
                    yield mock_memory
            return FakeResult()

    class FakeGraph:
        driver = MagicMock()
        database = "colony"

        def __init__(self):
            self.driver.session = lambda **kw: FakeSession()

    registry.graph = FakeGraph()

    # Mock goals engine
    mock_goals = AsyncMock()
    mock_goals.propose_goal = AsyncMock(return_value=True)
    registry.goals = mock_goals

    with tempfile.TemporaryDirectory() as tmp:
        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            state_file=Path(tmp) / "state.json",
        )
        result = await task.run()

    assert result["memories_scanned"] == 1
    assert result["candidates_found"] >= 1
    assert result["goals_created"] >= 1

    # Verify propose_goal was called with expected args
    calls = mock_goals.propose_goal.await_args_list
    assert any("report" in str(c.kwargs.get("title", "")).lower() for c in calls)


@pytest.mark.asyncio
async def test_synthesis_deduplicates_candidates():
    """Same title from multiple memories should only create one goal."""
    registry = MagicMock()

    mock_memories = [
        {
            "id": f"mem-{i}",
            "content": "User: I need to finish the report\nAssistant: ok",
            "created_at": f"2024-06-15T1{i}:00:00Z",
            "strength": 0.8,
        }
        for i in range(3)
    ]

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def run(self, query, **params):
            class FakeResult:
                async def __aiter__(self):
                    for m in mock_memories:
                        yield m
            return FakeResult()

    class FakeGraph:
        driver = MagicMock()
        database = "colony"

        def __init__(self):
            self.driver.session = lambda **kw: FakeSession()

    registry.graph = FakeGraph()
    mock_goals = AsyncMock()
    mock_goals.propose_goal = AsyncMock(return_value=True)
    registry.goals = mock_goals

    with tempfile.TemporaryDirectory() as tmp:
        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            state_file=Path(tmp) / "state.json",
        )
        result = await task.run()

    # Should dedupe to 1 unique candidate even though 3 memories scanned
    assert result["memories_scanned"] == 3
    assert result["candidates_found"] == 1
    assert result["goals_created"] == 1


@pytest.mark.asyncio
async def test_synthesis_respects_watermark():
    """Memories older than last_processed_at should be skipped."""
    registry = MagicMock()

    mock_memory = {
        "id": "mem-old",
        "content": "User: I need to do something\nAssistant: ok",
        "created_at": "2024-06-10T10:00:00Z",  # older than watermark
        "strength": 0.8,
    }

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def run(self, query, **params):
            class FakeResult:
                async def __aiter__(self):
                    yield mock_memory
            return FakeResult()

    class FakeGraph:
        driver = MagicMock()
        database = "colony"

        def __init__(self):
            self.driver.session = lambda **kw: FakeSession()

    registry.graph = FakeGraph()
    mock_goals = AsyncMock()
    registry.goals = mock_goals

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "state.json"
        # Pre-seed state with a watermark newer than the memory
        SynthesisState(last_processed_at="2024-06-15T10:00:00Z").save(state_file)

        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            state_file=state_file,
        )
        result = await task.run()

    assert result["memories_scanned"] == 0
    assert result["candidates_found"] == 0
    mock_goals.propose_goal.assert_not_awaited()
