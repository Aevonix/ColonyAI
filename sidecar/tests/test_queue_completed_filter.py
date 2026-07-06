"""get_completed_jobs_since must honor a job_type filter (the API declared
the param but the query silently dropped it)."""

import pytest

from colony_sidecar.task_queue.models import Job, JobType
from colony_sidecar.task_queue.queue_manager import QueueManager


@pytest.fixture()
async def queue(tmp_path):
    q = QueueManager(db_path=tmp_path / "q.db")
    await q.start()
    yield q
    await q.stop()


async def _complete(q, job_type):
    from colony_sidecar.task_queue.models import WorkerCapabilities
    job = Job(job_type=job_type, payload={"description": f"{job_type.value} job"})
    jid = await q.post(job)
    caps = WorkerCapabilities(node_id="w", capabilities=set(),
                              job_types={job_type})
    claimed = await q.claim_job("w", caps)   # sets claimed_by, required by complete_job
    assert claimed is not None and claimed.job_id == jid
    await q.complete_job(jid, "w", {"result": "ok"})
    return jid


async def test_completed_filter_by_type(queue):
    await _complete(queue, JobType.RESEARCH)
    await _complete(queue, JobType.RESEARCH)
    await _complete(queue, JobType.INFERENCE)
    import datetime
    since = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    research = await queue.get_completed_jobs_since(since, job_type="research")
    assert len(research) == 2
    assert all(j["job_type"] == "research" for j in research)

    inference = await queue.get_completed_jobs_since(since, job_type="inference")
    assert len(inference) == 1

    everything = await queue.get_completed_jobs_since(since)
    assert len(everything) == 3
