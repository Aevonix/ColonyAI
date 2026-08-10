"""Built-in bridge uses the same exact-attempt lifecycle as HTTP workers."""

from __future__ import annotations

import pytest

from colony_sidecar.services.agent_bridge import AgentBridgeService
from colony_sidecar.task_queue.models import Job, JobType


class _Queue:
    def __init__(self, *, starts=True, payload=None):
        self.calls = []
        self.starts = starts
        self.job = Job(
            job_type=JobType.AGENT_ACTION,
            payload=payload or {"description": "dispatch"},
            claim_attempt_id="attempt-1",
        )

    async def claim_job(self, node_id, capabilities):
        self.calls.append((
            "claim", node_id, capabilities.node_id,
            frozenset(capabilities.capabilities),
        ))
        return self.job

    async def start_job(self, job_id, node_id, *, claim_attempt_id):
        self.calls.append(("start", job_id, node_id, claim_attempt_id))
        return self.starts

    async def release_job(self, job_id, node_id, *, claim_attempt_id):
        self.calls.append(("release", job_id, node_id, claim_attempt_id))
        return True


class _Bridge(AgentBridgeService):
    def __init__(self, queue, *, webhook_ok=True):
        super().__init__(
            initiative_store=None,
            task_queue=type("Facade", (), {"queue": queue})(),
            webhook_url="http://agent/initiatives",
            jobs_webhook_url="http://agent/jobs",
            node_id="sidecar-bridge",
        )
        self.queue = queue
        self.webhook_ok = webhook_ok
        self.payloads = []

    async def _post_webhook(self, url, payload):
        self.queue.calls.append(("webhook", url))
        self.payloads.append(payload)
        return self.webhook_ok


@pytest.mark.asyncio
async def test_builtin_bridge_starts_exact_attempt_before_webhook():
    queue = _Queue()
    bridge = _Bridge(queue)
    await bridge._dispatch_jobs()
    assert [call[0] for call in queue.calls] == ["claim", "start", "webhook"]
    payload = bridge.payloads[0]["payload"]
    assert payload["claim_attempt_id"] == "attempt-1"
    assert payload["heartbeat_url"].endswith("/heartbeat")


@pytest.mark.asyncio
async def test_builtin_bridge_uses_only_configured_generic_route(monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_WORKER_ROUTES", "agent_sync")
    queue = _Queue()
    await _Bridge(queue)._dispatch_jobs()
    claim = queue.calls[0]
    assert claim[3] >= {"agent_sync:v1"}
    assert "hermes_run:v1" not in claim[3]
    assert "action_plane:v1" not in claim[3]


@pytest.mark.asyncio
async def test_builtin_bridge_claim_loop_obeys_global_kill_switch(monkeypatch):
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    queue = _Queue()
    bridge = _Bridge(queue)
    await bridge._dispatch_jobs()
    assert queue.calls == []
    assert bridge.payloads == []


@pytest.mark.asyncio
async def test_builtin_bridge_releases_when_start_or_webhook_fails():
    start_queue = _Queue(starts=False)
    await _Bridge(start_queue)._dispatch_jobs()
    assert [call[0] for call in start_queue.calls] == [
        "claim", "start", "release",
    ]

    webhook_queue = _Queue()
    await _Bridge(webhook_queue, webhook_ok=False)._dispatch_jobs()
    assert [call[0] for call in webhook_queue.calls] == [
        "claim", "start", "webhook", "release",
    ]


@pytest.mark.asyncio
async def test_builtin_bridge_forwards_exact_work_order_contract():
    params = {
        "schema": "WorkOrderV1",
        "version": 1,
        "source": "project_engine",
        "work_order_id": "work-1",
        "work_order_digest": "d" * 64,
        "project_id": "project-1",
        "step_id": "step-1",
        "step_ordinal": 1,
        "objective": "bounded task",
        "success_criteria": ["return evidence"],
        "context_refs": ["memory:one"],
        "capability_allowlist": ["memory:read", "reasoning"],
        "risk_class": "internal",
        "recipient_scope": "owner",
        "max_runtime_seconds": 60,
        "max_attempts": 2,
        "issued_at": "2026-07-12T00:00:00+00:00",
        "deadline": "2026-07-13T00:00:00+00:00",
        "action_hint": "agent_project_analyze",
    }
    bridge = _Bridge(_Queue(payload=params))
    await bridge._dispatch_jobs()
    payload = bridge.payloads[0]["payload"]
    assert payload["work_order"]["source"] == "project_engine"
    assert payload["work_order"]["step_ordinal"] == 1
    shape = payload["result_contract"]["complete_body_shape"]
    assert shape["output"]["execution_result"]["work_order_digest"] == "d" * 64
