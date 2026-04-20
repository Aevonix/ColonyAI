"""Tests for skill synthesis body generation."""

from __future__ import annotations

from datetime import datetime, timezone

from colony_sidecar.skills.learning.pattern_extractor import PatternExtractor
from colony_sidecar.skills.models import TaskSolution


def _solution(trace):
    return TaskSolution(
        task_id="t1",
        task_description="Send a polite reminder email",
        inputs={"recipient": "alice@example.com"},
        output={"sent": True},
        trace=trace,
        dependencies=[],
        embedding=None,
        step_fingerprint=[],
        duration_secs=1.0,
        completed_at=datetime.now(timezone.utc),
    )


def test_empty_trace_emits_not_implemented():
    extractor = PatternExtractor()
    pattern = extractor.extract(_solution(trace=[]))
    assert "NotImplementedError" in pattern.source_code
    assert "approval required" in pattern.source_code


def test_nonempty_trace_emits_runnable_body():
    extractor = PatternExtractor()
    trace = [
        {"type": "tool_call", "tool": "send_email",
         "args": {"to": "alice", "subject": "ping"}, "summary": "email"},
        {"type": "tool_call", "tool": "log_event",
         "args": {"event": "email_sent"}, "summary": "log"},
    ]
    pattern = extractor.extract(_solution(trace=trace))
    src = pattern.source_code
    assert "NotImplementedError" not in src
    assert "async def run(colony" in src
    assert "colony.tools.invoke('send_email'" in src
    assert "colony.tools.invoke('log_event'" in src
    assert "return _r1" in src
