"""Skill-capture wiring from the job writeback phase (v0.17.0)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.task_queue.models import JobResult, JobStatus


def _job(job_id="j1"):
    return SimpleNamespace(
        job_id=job_id, job_type="agent_action", status=JobStatus.COMPLETED,
        payload={"action_hint": "coding_check_ci", "description": "novel work",
                 "initiative_id": None},
        tags={},
        result=JobResult(job_id=job_id, worker_node_id="w", status=JobStatus.COMPLETED,
                         output={"summary": "did the thing"}),
        claimed_by="hermes-agent")


def _loop():
    registry = MagicMock()
    registry.graph = None
    registry.goals = None
    registry.initiative_store = None
    qm = MagicMock()
    qm.get_jobs_by_status = AsyncMock(side_effect=lambda s: [_job()] if s == JobStatus.COMPLETED else [])
    qm.update_job_status = AsyncMock()
    registry.task_queue = SimpleNamespace(queue=qm)
    return AutonomyLoop(registry=registry)


@pytest.mark.asyncio
async def test_capture_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COLONY_ENABLE_SKILL_SYNTHESIS", raising=False)
    loop = _loop()
    service = MagicMock()
    service.handle = AsyncMock(return_value="skill-x")
    # even with a service cached, the env gate wins
    loop._skill_learning = service
    await loop._phase_job_writeback()
    service.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_creates_deferred_review_initiative(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_SKILL_SYNTHESIS", "true")
    loop = _loop()
    service = MagicMock()
    service.handle = AsyncMock(return_value="parse-ci-logs_ab12cd34")
    loop._skill_learning = service

    await loop._phase_job_writeback()

    service.handle.assert_awaited_once()
    event = service.handle.await_args.args[0]
    assert event.solution.task_id == "j1"
    assert event.solution.task_description == "novel work"
    deferred = loop._deferred_initiatives
    assert len(deferred) == 1
    assert deferred[0].dedup_key == "skill_review:parse-ci-logs_ab12cd34"
    assert deferred[0].action_hint is None
    assert "cannot run until you approve" in deferred[0].rationale


@pytest.mark.asyncio
async def test_novelty_skip_creates_nothing(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_SKILL_SYNTHESIS", "true")
    loop = _loop()
    service = MagicMock()
    service.handle = AsyncMock(return_value=None)  # novelty gate said skip
    loop._skill_learning = service
    await loop._phase_job_writeback()
    assert getattr(loop, "_deferred_initiatives", []) == []


@pytest.mark.asyncio
async def test_capture_errors_do_not_break_writeback(monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_SKILL_SYNTHESIS", "true")
    loop = _loop()
    service = MagicMock()
    service.handle = AsyncMock(side_effect=RuntimeError("packager exploded"))
    loop._skill_learning = service
    await loop._phase_job_writeback()  # must not raise
