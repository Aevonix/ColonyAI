"""GoalEngine.on_message must preserve inference provenance + any detected
deadline hint in the proposed goal's context.

Regression: inferred-goal deadlines (and all inference provenance) were silently
dropped because the two propose_goal() calls forwarded only title/description/
source/priority. suggested_deadline is a matched phrase ("due by", "before the
meeting"), not a parseable datetime, so it is carried as a context hint.
"""
from colony_sidecar.goals.engine import GoalEngine
from colony_sidecar.goals.config import GoalEngineConfig
from colony_sidecar.goals.inference import (
    ConversationMessage,
    InferenceCandidate,
    IntentSignal,
)
from colony_sidecar.goals.models import GoalSource, GoalStatus, GoalPriority


class _StubInference:
    """Return a fixed candidate so the test targets on_message's forwarding
    logic, not the regex extraction heuristics."""

    def __init__(self, candidate):
        self._candidate = candidate

    def process_message(self, message, history=None):
        return self._candidate


def _engine_with_candidate(candidate):
    engine = GoalEngine(config=GoalEngineConfig(db_path=None, inference_enabled=True))
    engine._inference = _StubInference(candidate)
    return engine


def test_on_message_preserves_deadline_hint_and_provenance():
    candidate = InferenceCandidate(
        title="Finish the quarterly report",
        description="user delegated finishing the quarterly report",
        signals=[IntentSignal.DELEGATION],
        confidence=0.92,
        source_messages=["m-1"],
        suggested_deadline="due by Friday",
        priority=GoalPriority.HIGH,
    )
    engine = _engine_with_candidate(candidate)
    msg = ConversationMessage(
        message_id="m-1", role="user",
        content="can you finish the report, due by Friday",
    )

    result = engine.on_message(msg)
    assert result is candidate

    goals = engine._store.list_goals(limit=10)
    assert len(goals) == 1
    g = goals[0]
    assert g.source == GoalSource.INFERRED
    assert g.context.get("deadline_hint") == "due by Friday"
    assert g.context.get("inference_confidence") == 0.92
    assert "delegation" in g.context.get("inference_signals", [])
    assert g.context.get("source_messages") == ["m-1"]
    # high confidence + delegation -> auto-accepted
    assert g.status == GoalStatus.ACCEPTED


def test_on_message_without_deadline_omits_hint_and_stays_proposed():
    candidate = InferenceCandidate(
        title="Learn Rust",
        description="aspirational, no delegation",
        signals=[IntentSignal.DESIRE],
        confidence=0.40,
        source_messages=["m-2"],
        suggested_deadline=None,
        priority=GoalPriority.NORMAL,
    )
    engine = _engine_with_candidate(candidate)
    msg = ConversationMessage(
        message_id="m-2", role="user", content="i'd like to learn rust someday",
    )

    engine.on_message(msg)
    goals = engine._store.list_goals(limit=10)
    assert len(goals) == 1
    g = goals[0]
    assert "deadline_hint" not in g.context
    assert g.context.get("inference_confidence") == 0.40
    # low confidence / no delegation -> proposed, awaiting user
    assert g.status == GoalStatus.PROPOSED
