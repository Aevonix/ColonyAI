"""Phase 6c — completed agent work feeds back into memory (v0.17.0)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.task_queue.models import JobResult, JobStatus


def _job(job_id="j1", status=JobStatus.COMPLETED, action="coding_check_ci",
         tags=None, output=None, error=None):
    result = JobResult(
        job_id=job_id, worker_node_id="w1", status=status,
        output=output or {"summary": "CI green"}, error=error)
    return SimpleNamespace(
        job_id=job_id, job_type="agent_action", status=status,
        payload={"action_hint": action, "description": "check the CI",
                 "initiative_id": "init-1"},
        tags=tags or {}, result=result, claimed_by="hermes-agent")


def _loop(jobs, graph=None, goals=None, store=None):
    registry = MagicMock()
    qm = MagicMock()

    async def by_status(status):
        return [j for j in jobs if j.status == status]

    qm.get_jobs_by_status = AsyncMock(side_effect=by_status)
    qm.update_job_status = AsyncMock()
    registry.task_queue = SimpleNamespace(queue=qm)
    registry.graph = graph
    registry.goals = goals
    registry.initiative_store = store
    loop = AutonomyLoop(registry=registry)
    return loop, qm


@pytest.mark.asyncio
async def test_completed_job_writes_memory_and_closes_initiative():
    graph = MagicMock()
    graph.store_memory = AsyncMock(return_value="mem-1")
    store = MagicMock()
    loop, qm = _loop([_job()], graph=graph, store=store)

    await loop._phase_job_writeback()

    graph.store_memory.assert_awaited_once()
    kwargs = graph.store_memory.await_args.kwargs
    assert "coding_check_ci" in kwargs["content"]
    assert kwargs["memory_type"] == "episodic"
    assert kwargs["source_uri"] == "colony://jobs/j1"
    store.complete.assert_called_once()
    qm.update_job_status.assert_awaited()  # memory_synced tag
    tag_call = qm.update_job_status.await_args
    assert tag_call.kwargs.get("tags", tag_call.args[-1]) == {"memory_synced": "true"}


@pytest.mark.asyncio
async def test_failed_job_records_failure():
    graph = MagicMock()
    graph.store_memory = AsyncMock(return_value="mem-2")
    store = MagicMock()
    job = _job(status=JobStatus.FAILED, output={}, error="tool exploded")
    loop, _ = _loop([job], graph=graph, store=store)

    await loop._phase_job_writeback()

    content = graph.store_memory.await_args.kwargs["content"]
    assert "FAILED" in content and "tool exploded" in content
    store.update.assert_called_once()


@pytest.mark.asyncio
async def test_already_synced_jobs_skipped():
    graph = MagicMock()
    graph.store_memory = AsyncMock()
    loop, qm = _loop([_job(tags={"memory_synced": "true"})], graph=graph)
    await loop._phase_job_writeback()
    graph.store_memory.assert_not_awaited()
    qm.update_job_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_observation_sync_jobs_tagged_not_memorized():
    graph = MagicMock()
    graph.store_memory = AsyncMock()
    loop, qm = _loop([_job(action="agent_sync_coding")], graph=graph)
    await loop._phase_job_writeback()
    graph.store_memory.assert_not_awaited()
    qm.update_job_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_goal_progress_propagates():
    goals = MagicMock()
    job = _job(output={"goal_id": "g1", "subtask_id": "s1"})
    loop, _ = _loop([job], goals=goals)
    await loop._phase_job_writeback()
    goals.on_job_completed.assert_called_once_with(job.result)


@pytest.mark.asyncio
async def test_poison_job_gives_up_after_three_attempts():
    graph = MagicMock()
    graph.store_memory = AsyncMock(side_effect=RuntimeError("neo4j down"))
    job = _job(tags={"memory_sync_attempts": "2"})
    loop, qm = _loop([job], graph=graph)
    await loop._phase_job_writeback()
    tags = qm.update_job_status.await_args.kwargs["tags"]
    assert tags["memory_sync_attempts"] == "3"
    assert tags["memory_synced"] == "true"


@pytest.mark.asyncio
async def test_no_queue_is_safe():
    registry = MagicMock()
    registry.task_queue = None
    loop = AutonomyLoop(registry=registry)
    await loop._phase_job_writeback()  # must not raise
