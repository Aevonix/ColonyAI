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
from colony_sidecar.goals.models import GoalStatus


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

    # Mock goals engine — propose_goal is SYNC, returns a Goal-like object
    mock_goal = MagicMock()
    mock_goal.goal_id = "goal-1"
    mock_goals = MagicMock()
    mock_goals.propose_goal = MagicMock(return_value=mock_goal)
    # list_goals is called for both PROPOSED and ACTIVE
    mock_goals.list_goals = MagicMock(return_value=[])
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

    # Verify list_goals was called for both PROPOSED and ACTIVE
    calls = mock_goals.list_goals.call_args_list
    statuses = [c.kwargs.get("status") for c in calls]
    assert GoalStatus.PROPOSED in statuses
    assert GoalStatus.ACTIVE in statuses

    # Verify propose_goal was called with expected args (sync, not async)
    calls = mock_goals.propose_goal.call_args_list
    assert any("report" in str(c.kwargs.get("title", "")).lower() for c in calls)

    # Verify confidence is stored in context, NOT passed as kwarg
    ctx = calls[0].kwargs.get("context", {})
    assert "inferred_confidence" in ctx
    assert "inferred_signals" in ctx


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

    mock_goal = MagicMock()
    mock_goal.goal_id = "goal-1"
    mock_goals = MagicMock()
    mock_goals.propose_goal = MagicMock(return_value=mock_goal)
    mock_goals.list_goals = MagicMock(return_value=[])
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
    mock_goals = MagicMock()
    mock_goals.list_goals = MagicMock(return_value=[])
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
    mock_goals.propose_goal.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_respects_max_goals_per_run():
    """Should stop creating goals once max_goals_per_run is reached."""
    registry = MagicMock()

    mock_memories = [
        {
            "id": f"mem-{i}",
            "content": f"User: I need to finish task {i}\nAssistant: ok",
            "created_at": f"2024-06-15T1{i}:00:00Z",
            "strength": 0.8,
        }
        for i in range(5)
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

    mock_goal = MagicMock()
    mock_goal.goal_id = "goal-1"
    mock_goals = MagicMock()
    mock_goals.propose_goal = MagicMock(return_value=mock_goal)
    mock_goals.list_goals = MagicMock(return_value=[])
    registry.goals = mock_goals

    with tempfile.TemporaryDirectory() as tmp:
        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            max_goals_per_run=2,
            state_file=Path(tmp) / "state.json",
        )
        result = await task.run()

    # Should scan all 5 but only create 2
    assert result["memories_scanned"] == 5
    assert result["goals_created"] == 2
    assert mock_goals.propose_goal.call_count == 2


@pytest.mark.asyncio
async def test_synthesis_skips_duplicate_existing_goal():
    """Should skip creating a goal that already exists as ACTIVE."""
    registry = MagicMock()

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

    # Existing goal with very similar title → should be detected as duplicate
    existing_goal = MagicMock()
    existing_goal.goal_id = "existing-1"
    existing_goal.title = "Finish the quarterly report by Friday"
    existing_goal.description = "Finish the quarterly report by Friday"
    existing_goal.is_terminal = MagicMock(return_value=False)

    mock_goals = MagicMock()
    mock_goals.propose_goal = MagicMock()
    mock_goals.list_goals = MagicMock(return_value=[existing_goal])
    registry.goals = mock_goals

    with tempfile.TemporaryDirectory() as tmp:
        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            state_file=Path(tmp) / "state.json",
        )
        result = await task.run()

    # Should find a candidate but skip it as duplicate
    assert result["memories_scanned"] == 1
    assert result["candidates_found"] >= 1
    assert result["goals_created"] == 0
    mock_goals.propose_goal.assert_not_called()

    # Verify both PROPOSED and ACTIVE were queried
    calls = mock_goals.list_goals.call_args_list
    statuses = [c.kwargs.get("status") for c in calls]
    assert GoalStatus.PROPOSED in statuses
    assert GoalStatus.ACTIVE in statuses


@pytest.mark.asyncio
async def test_synthesis_telemetry_touch():
    """Should touch telemetry.last_synthesis_at if telemetry provided."""
    registry = MagicMock()

    mock_memory = {
        "id": "mem-1",
        "content": "User: I need to do something\nAssistant: ok",
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

    mock_goal = MagicMock()
    mock_goal.goal_id = "goal-1"
    mock_goals = MagicMock()
    mock_goals.propose_goal = MagicMock(return_value=mock_goal)
    mock_goals.list_goals = MagicMock(return_value=[])
    registry.goals = mock_goals

    mock_telemetry = AsyncMock()

    with tempfile.TemporaryDirectory() as tmp:
        task = ConversationSynthesisTask(
            registry=registry,
            lookback_hours=24,
            telemetry=mock_telemetry,
            state_file=Path(tmp) / "state.json",
        )
        result = await task.run()

    mock_telemetry.touch.assert_awaited_once_with("last_synthesis_at")
