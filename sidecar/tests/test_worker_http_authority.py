"""HTTP worker authority and claimant-bound queue lifecycle tests.

These reproduce the pre-hardening boundary: any ``api:access`` caller could
claim as an arbitrary node with invented capabilities, then mutate another
worker's job by naming its job id.  Migration remains usable in shadow mode,
while enforce mode derives node/capability authority from the private API
keyring.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.authority import KeyringError, load_keyring, required_scope
from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager


def _worker_principal(
    principal: str,
    secret: str,
    node_id: str,
    *,
    capabilities: list[str] | None = None,
    job_types: list[str] | None = None,
    allow_unscoped_api: bool = True,
) -> dict:
    return {
        "principal": principal,
        "status": "active",
        # api:access keeps the migration test meaningful before exact route
        # scopes are selected by COLONY_WORKER_AUTHORITY_MODE=enforce.
        "scopes": [
            "api:access",
            "workers:contract",
            "workers:register",
            "workers:claim",
            "workers:lifecycle",
        ],
        "audiences": [],
        "allow_unscoped_api": allow_unscoped_api,
        "worker_grants": [
            {
                "node_id": node_id,
                "capabilities": capabilities or ["research"],
                "capacity": {"ram_gb": 16.0},
                "max_concurrent": 2,
                "job_types": job_types or ["research"],
            }
        ],
        "credentials": [
            {"id": "current", "secret": secret, "status": "active"}
        ],
    }


def _write_keyring(path, principals: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "principals": principals}))
    path.chmod(0o600)


def _app(keyring, *, legacy_key: str | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=legacy_key,
        keyring_path=str(keyring) if keyring else None,
    )
    app.include_router(queue_router.router)
    return app


def _headers(secret: str, principal: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if principal:
        headers["X-Colony-Principal"] = principal
    return headers


async def _manager(tmp_path) -> TaskQueueManager:
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")


async def _research_job(manager: TaskQueueManager) -> Job:
    job = Job(
        job_type=JobType.RESEARCH,
        payload={"risk": "read_only", "description": "summarize evidence"},
        capabilities=[JobCapabilityRequirement(name="research")],
    )
    await manager.queue.post(job)
    return job


@pytest.mark.asyncio
async def test_job_post_cannot_forge_authority_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    app = _app(None, legacy_key="legacy-key")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    "job_type": "research",
                    "tags": {
                        "approved_by": "forged-owner",
                        "auto_approved_by_policy": "true",
                        "action_digest": "forged-digest",
                        "governor_mode": "live",
                        "success_attested": "true",
                    },
                },
            )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "reserved_job_tags"
        assert (await manager.queue.get_queue_stats()).by_status == {}
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_action_attestation_requires_separate_scoped_verifier(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "strict")
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    manager = await _manager(tmp_path)
    job = Job(
        job_type=JobType.AGENT_ACTION,
        payload={
            "action_hint": "commitment_mark_complete",
            "ID": "commitment-http",
        },
    )
    await manager.queue.post(job)
    await queue_router.approve_job(
        job.job_id, queue_router.JobApproveRequest(),
    )
    claimed = await manager.queue.claim_job(
        "action-node",
        WorkerCapabilities(
            node_id="action-node",
            capabilities={"action_plane:v1"},
            job_types={JobType.AGENT_ACTION},
        ),
    )
    assert claimed is not None
    assert await manager.queue.start_job(
        job.job_id, "action-node", claimed.claim_attempt_id,
    )
    completion = await manager.queue.complete_job(
        job.job_id,
        "action-node",
        {"status": "completed", "action_plane": {"state": "completed"}},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert completion["job_status"] == "neutral"
    pending = await manager.queue.get_job(job.job_id)

    verifier = {
        "principal": "receipt-verifier",
        "status": "active",
        "scopes": ["workers:attest", "workers:inspect"],
        "audiences": [],
        "credentials": [{
            "id": "current", "secret": "verifier-secret", "status": "active",
        }],
    }
    denied = {
        "principal": "ordinary-reader",
        "status": "active",
        "scopes": ["api:access"],
        "audiences": [],
        "credentials": [{
            "id": "current", "secret": "reader-secret", "status": "active",
        }],
    }
    ring = tmp_path / "attestation-keyring.json"
    _write_keyring(ring, [verifier, denied])
    app = _app(ring, legacy_key="legacy-secret")
    body = {
        "schema": "ActionReceiptAttestationV1",
        "version": 1,
        "job_id": job.job_id,
        "claim_attempt_id": claimed.claim_attempt_id,
        "action_digest": pending.tags["action_digest"],
        "effect_class": "mutation",
        "terminal_outcome": "succeeded",
        "receipt_refs": ["commitment-ledger:commitment-http:complete"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "summary": "read-only verifier observed the ledger transition",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            missing = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}", json=body,
            )
            insufficient = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("reader-secret"), json=body,
            )
            legacy = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("legacy-secret"), json=body,
            )
            inspection = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
                headers=_headers("verifier-secret"),
            )
            caller_digest = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("verifier-secret"),
                json={**body, "evidence_sha256": "f" * 64},
            )
            malformed_ref = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("verifier-secret"),
                json={**body, "receipt_refs": ["not-qualified"]},
            )
            accepted = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("verifier-secret"), json=body,
            )
            replay = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("verifier-secret"), json=body,
            )
            changed_body = {**body, "summary": "different assertion"}
            changed = await client.post(
                f"/v1/host/queue/attestations/jobs/{job.job_id}",
                headers=_headers("verifier-secret"), json=changed_body,
            )
        assert missing.status_code == 401
        assert insufficient.status_code == 403
        assert legacy.status_code == 403
        assert inspection.status_code == 200
        assert caller_digest.status_code == 422
        assert malformed_ref.status_code == 422
        inspected = inspection.json()
        assert inspected["tags"] == {
            "action_digest": pending.tags["action_digest"],
            "action_result_contract": "ActionReceiptAttestationV1",
            "agent_action_route": "action_plane:v1",
            "verification_pending": "true",
            "success_attestation_schema": "ActionReceiptAttestationV1",
        }
        assert accepted.status_code == 200
        assert accepted.json()["replayed"] is False
        assert len(accepted.json()["evidence_sha256"]) == 64
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["evidence_sha256"] == (
            accepted.json()["evidence_sha256"]
        )
        assert changed.status_code == 409
        assert (await manager.queue.get_job(job.job_id)).status is (
            JobStatus.COMPLETED
        )
        assert required_scope(
            "POST", f"/v1/host/queue/attestations/jobs/{job.job_id}",
        ) == "workers:attest"
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.parametrize(
    "extra",
    [
        {"scopes": ["workers:attest", "workers:lifecycle"]},
        {
            "scopes": [
                "workers:attest", "workers:register",
                "workers:claim", "workers:lifecycle",
            ],
            "worker_grants": [{
                "node_id": "worker-a",
                "capabilities": ["research"],
                "capacity": {},
                "max_concurrent": 1,
                "job_types": ["research"],
            }],
        },
    ],
)
def test_attestation_principal_cannot_also_execute(tmp_path, extra):
    principal = {
        "principal": "mixed-role",
        "status": "active",
        "audiences": [],
        "credentials": [{
            "id": "current", "secret": "mixed-secret", "status": "active",
        }],
        **extra,
    }
    ring = tmp_path / "mixed-role.json"
    _write_keyring(ring, [principal])
    with pytest.raises(KeyringError, match="verifier-only principal"):
        load_keyring(ring)


@pytest.mark.asyncio
async def test_public_agent_action_uses_registry_and_cannot_spoof_work_order(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    app = _app(None, legacy_key="legacy-key")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            spoof = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    "job_type": "agent_action",
                    "payload": {
                        "schema": "WorkOrderV1",
                        "action_hint": "agent_project_analyze",
                    },
                },
            )
            unknown = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    "job_type": "agent_action",
                    "payload": {"action_hint": "run whatever this says"},
                },
            )
            effect = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    "job_type": "agent_action",
                    "payload": {
                        "action_hint": "agent_deliver_message",
                        "risk": "outbound",
                    },
                },
            )
        assert spoof.status_code == 400
        assert spoof.json()["detail"]["code"] == "work_order_authority_reserved"
        assert unknown.status_code == 400
        assert unknown.json()["detail"]["code"] == "unregistered_agent_action"
        assert effect.status_code == 200
        stored = await manager.queue.get_job(effect.json()["job_id"])
        assert stored.status is JobStatus.BLOCKED
        assert stored.tags["blocked_reason"] == "awaiting_owner_approval"
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_public_agent_action_cannot_select_or_weaken_server_routes(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    app = _app(None, legacy_key="legacy-key")
    base = {
        "job_type": "agent_action",
        "payload": {"action_hint": "commitment_list_open"},
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            matching = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    **base,
                    "capabilities": [{"name": "agent_sync:v1"}],
                },
            )
            mismatching = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    **base,
                    "capabilities": [{
                        "name": "action_plane:v1",
                        "preferred": True,
                    }],
                },
            )
            derived = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={**base, "capabilities": []},
            )
            effect_on_read = await client.post(
                "/v1/host/queue/jobs",
                headers=_headers("legacy-key"),
                json={
                    **base,
                    "capabilities": [{"name": "filesystem:write"}],
                },
            )
        for rejected in (matching, mismatching):
            assert rejected.status_code == 400
            assert rejected.json()["detail"]["code"] == (
                "agent_action_route_authority_reserved"
            )
        assert derived.status_code == 200
        stored = await manager.queue.get_job(derived.json()["job_id"])
        assert [item.name for item in stored.capabilities] == [
            "agent_sync:v1",
        ]
        assert effect_on_read.status_code == 400
        assert effect_on_read.json()["detail"]["code"] == (
            "agent_action_routing_invalid"
        )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_http_claim_fails_closed_without_scheduler_readiness(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    await _research_job(manager)
    manager.queue.set_execution_ready(False, "scheduler_test_failure")
    app = _app(None, legacy_key="legacy-key")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("legacy-key"),
                json={"node_id": "worker-a", "job_types": ["research"]},
            )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == (
            "queue_execution_unavailable"
        )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_enforce_claim_is_principal_node_bound_and_body_can_only_narrow(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_worker_principal("worker-a-principal", "a-key", "worker-a")])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            forged_node = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "node_id": "worker-b",
                    "capabilities": ["research"],
                    "job_types": ["research"],
                },
            )
            expanded_capability = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "node_id": "worker-a",
                    "capabilities": ["research", "shell"],
                    "job_types": ["research"],
                },
            )
            expanded_capacity = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "node_id": "worker-a",
                    "capabilities": ["research"],
                    "capacity": {"ram_gb": 32},
                    "job_types": ["research"],
                },
            )
            claimed = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "node_id": "worker-a",
                    "capabilities": ["research"],
                    "capacity": {"ram_gb": 8},
                    "max_concurrent": 1,
                    "job_types": ["research"],
                },
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert forged_node.status_code == 403
    assert forged_node.json()["detail"]["code"] == "worker_node_not_granted"
    assert expanded_capability.status_code == 403
    assert expanded_capability.json()["detail"]["code"] == "worker_grant_exceeded"
    assert expanded_capacity.status_code == 403
    assert expanded_capacity.json()["detail"]["code"] == "worker_grant_exceeded"
    assert claimed.status_code == 200
    payload = claimed.json()
    assert payload["claimed_by"] == "worker-a"
    assert payload["tags"]["worker_authority_mode"] == "enforce"
    assert payload["tags"]["worker_authority_principal"] == "worker-a-principal"
    assert payload["tags"]["worker_authority_credential"] == "current"


@pytest.mark.asyncio
async def test_restricted_worker_uses_exact_routes_but_not_api_fallback(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_RELEASE_COMMIT", "1" * 40)
    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", "2" * 64,
    )
    keyring = tmp_path / "restricted-worker.json"
    _write_keyring(keyring, [_worker_principal(
        "restricted-worker", "restricted-key", "worker-a",
        allow_unscoped_api=False,
    )])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    headers = _headers("restricted-key", "restricted-worker")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            contract = await client.get(
                "/v1/host/queue/contract", headers=headers,
            )
            registered = await client.post(
                "/v1/host/queue/workers/register",
                headers=headers,
                json={"node_id": "worker-a"},
            )
            claimed = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=headers,
                json={"node_id": "worker-a"},
            )
            job_id = claimed.json()["job_id"]
            attempt_id = claimed.json()["claim_attempt_id"]
            started = await client.post(
                f"/v1/host/queue/jobs/{job_id}/start",
                headers=headers,
                json={"claim_attempt_id": attempt_id},
            )
            goals = await client.get("/v1/host/goals", headers=headers)
            unknown = await client.get(
                "/v1/host/not-an-exact-route", headers=headers,
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert contract.status_code == 200
    assert registered.status_code == 200
    assert claimed.status_code == 200
    assert started.status_code == 200
    for denied in (goals, unknown):
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "unscoped_api_denied"


@pytest.mark.asyncio
async def test_shadow_keeps_legacy_worker_usable_but_records_denied_future_posture(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(None, legacy_key="legacy-key")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("legacy-key"),
                json={
                    "node_id": "legacy-worker",
                    "capabilities": ["research"],
                    "job_types": ["research"],
                },
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert response.status_code == 200
    payload = response.json()
    assert payload["claimed_by"] == "legacy-worker"
    assert payload["tags"]["worker_authority_mode"] == "shadow"
    assert payload["tags"]["worker_authority_principal"] == "legacy"
    assert payload["tags"]["worker_authority_would_deny"] == "true"


@pytest.mark.asyncio
async def test_shadow_scoped_worker_records_body_expansion_without_breaking_it(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_worker_principal("worker-a-principal", "a-key", "worker-a")])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "node_id": "worker-a",
                    "capabilities": ["research", "shell"],
                    "job_types": ["research"],
                },
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert response.status_code == 200
    assert response.json()["tags"]["worker_authority_would_deny"] == "true"


@pytest.mark.asyncio
async def test_shadow_scoped_worker_uses_grant_defaults_for_realistic_canary(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_worker_principal("worker-a-principal", "a-key", "worker-a")])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={"node_id": "worker-a"},
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert response.status_code == 200
    assert response.json()["claimed_by"] == "worker-a"
    assert response.json()["tags"]["worker_authority_would_deny"] == "false"


@pytest.mark.asyncio
async def test_enforce_lifecycle_requires_exact_authenticated_claimant(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [
        _worker_principal("worker-a-principal", "a-key", "worker-a"),
        _worker_principal("worker-b-principal", "b-key", "worker-b"),
    ])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            claimed = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={"node_id": "worker-a"},
            )
            job_id = claimed.json()["job_id"]
            claim_attempt_id = claimed.json()["claim_attempt_id"]
            forged_start = await client.post(
                f"/v1/host/queue/jobs/{job_id}/start",
                headers=_headers("b-key", "worker-b-principal"),
                json={"claim_attempt_id": claim_attempt_id},
            )
            started = await client.post(
                f"/v1/host/queue/jobs/{job_id}/start",
                headers=_headers("a-key", "worker-a-principal"),
                json={"claim_attempt_id": claim_attempt_id},
            )
            forged_complete = await client.post(
                f"/v1/host/queue/jobs/{job_id}/complete",
                headers=_headers("b-key", "worker-b-principal"),
                json={
                    "output": {"summary": "forged"},
                    "claim_attempt_id": claim_attempt_id,
                },
            )
            completed = await client.post(
                f"/v1/host/queue/jobs/{job_id}/complete",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "output": {"summary": "done"},
                    "claim_attempt_id": claim_attempt_id,
                },
            )
            stored = await manager.queue.get_job(job_id)
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert claimed.status_code == 200
    assert forged_start.status_code == 403
    assert forged_start.json()["detail"]["code"] == "worker_claimant_mismatch"
    assert started.status_code == 200
    assert forged_complete.status_code == 403
    assert completed.status_code == 200
    assert completed.json()["success"] is True
    assert stored is not None and stored.status == JobStatus.COMPLETED
    assert stored.result is not None
    assert stored.result.output == {"summary": "done"}


@pytest.mark.asyncio
async def test_enforce_claim_applies_server_concurrency_ceiling_atomically(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    principal = _worker_principal("worker-a-principal", "a-key", "worker-a")
    principal["worker_grants"][0]["max_concurrent"] = 1
    _write_keyring(keyring, [principal])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={"node_id": "worker-a"},
            )
            second = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={"node_id": "worker-a"},
            )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert first.status_code == 200 and first.json() is not None
    assert second.status_code == 200 and second.json() is None


@pytest.mark.asyncio
async def test_completion_ignores_untrusted_future_started_at(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_worker_principal("worker-a-principal", "a-key", "worker-a")])
    manager = await _manager(tmp_path)
    await _research_job(manager)
    app = _app(keyring)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            claimed = await client.post(
                "/v1/host/queue/jobs/claim",
                headers=_headers("a-key", "worker-a-principal"),
                json={"node_id": "worker-a"},
            )
            job_id = claimed.json()["job_id"]
            claim_attempt_id = claimed.json()["claim_attempt_id"]
            assert (await client.post(
                f"/v1/host/queue/jobs/{job_id}/start",
                headers=_headers("a-key", "worker-a-principal"),
                json={"claim_attempt_id": claim_attempt_id},
            )).status_code == 200
            completed = await client.post(
                f"/v1/host/queue/jobs/{job_id}/complete",
                headers=_headers("a-key", "worker-a-principal"),
                json={
                    "output": {"summary": "done"},
                    "started_at": "2099-01-01T00:00:00+00:00",
                    "claim_attempt_id": claim_attempt_id,
                },
            )
            stored = await manager.queue.get_job(job_id)
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert completed.status_code == 200
    assert stored is not None and stored.result is not None
    assert stored.result.duration_seconds is not None
    assert 0 <= stored.result.duration_seconds < 60


@pytest.mark.asyncio
async def test_completed_durations_excludes_legacy_poisoned_evidence(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    jobs = [await _research_job(manager) for _ in range(3)]
    assert manager.queue._db is not None
    results = [
        {"completed_at": "2026-07-12T12:00:00+00:00", "duration_seconds": 4.25},
        {"completed_at": "2026-07-12T12:00:00+00:00", "duration_seconds": -900.0},
        {"completed_at": "2026-07-12T12:00:00+00:00", "duration_seconds": "NaN"},
    ]
    try:
        for job, result in zip(jobs, results):
            await manager.queue._db.execute(
                "UPDATE jobs SET status = ?, result = ? WHERE job_id = ?",
                (JobStatus.COMPLETED.value, json.dumps(result), job.job_id),
            )
        await manager.queue._db.commit()
        durations = await manager.queue.completed_durations(
            "2026-07-12T00:00:00+00:00",
            "2026-07-13T00:00:00+00:00",
        )
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert durations == [4.25]


@pytest.mark.asyncio
async def test_failed_transition_cannot_rollback_another_workers_success(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    job = await _research_job(manager)
    claimed = await manager.queue.claim_job(
        "worker-a",
        queue_router.WorkerCapabilities(
            node_id="worker-a",
            capabilities={"research"},
            job_types={JobType.RESEARCH},
        ),
    )
    assert claimed is not None
    entered_audit = asyncio.Event()
    release_audit = asyncio.Event()
    original_audit = manager.queue._audit

    async def blocked_audit(job_id, from_status, to_status, **kwargs):
        if job_id == job.job_id and to_status == JobStatus.RUNNING.value:
            entered_audit.set()
            await release_audit.wait()
        return await original_audit(job_id, from_status, to_status, **kwargs)

    monkeypatch.setattr(manager.queue, "_audit", blocked_audit)
    first = asyncio.create_task(
        manager.queue.start_job(
            job.job_id, "worker-a", claimed.claim_attempt_id,
        )
    )
    await entered_audit.wait()
    failed = asyncio.create_task(
        manager.queue.start_job("missing-job", "worker-b")
    )
    await asyncio.sleep(0)
    try:
        # A serialized connection leaves the failed mutation waiting; it can
        # never roll back the successful UPDATE before its audit commits.
        assert failed.done() is False
        release_audit.set()
        assert await first is True
        assert await failed is False
        stored = await manager.queue.get_job(job.job_id)
        audit = await manager.queue.get_audit_log(job.job_id, limit=20)
    finally:
        release_audit.set()
        await manager.stop()
        TaskQueueManager._instance = None

    assert stored is not None and stored.status == JobStatus.RUNNING
    transitions = [
        (entry.from_status, entry.to_status, entry.node_id)
        for entry in audit
        if entry.to_status == JobStatus.RUNNING.value
    ]
    assert transitions == [("claimed", "running", "worker-a")]


@pytest.mark.asyncio
async def test_queue_transitions_are_claimant_and_state_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    job = await _research_job(manager)
    try:
        claimed = await manager.queue.claim_job(
            "worker-a",
            queue_router.WorkerCapabilities(
                node_id="worker-a",
                capabilities={"research"},
                job_types={JobType.RESEARCH},
            ),
        )
        assert claimed is not None
        assert await manager.queue.start_job(
            job.job_id, "worker-b", claimed.claim_attempt_id,
        ) is False
        assert await manager.queue.start_job(
            job.job_id, "worker-a", claimed.claim_attempt_id,
        ) is True
        assert await manager.queue.fail_job(
            job.job_id, "worker-b", "forged failure",
            claim_attempt_id=claimed.claim_attempt_id,
        ) is False
        assert await manager.queue.release_job(
            job.job_id, "worker-b", claimed.claim_attempt_id,
        ) is False
        still_running = await manager.queue.get_job(job.job_id)
        assert still_running is not None
        assert still_running.status == JobStatus.RUNNING
        assert still_running.claimed_by == "worker-a"
        assert await manager.queue.release_job(
            job.job_id, "worker-a", claimed.claim_attempt_id,
        ) is True
        released = await manager.queue.get_job(job.job_id)
        assert released is not None
        assert released.status == JobStatus.QUEUED
        assert released.claimed_by is None
        audit = await manager.queue.get_audit_log(job.job_id, limit=20)
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    # Failed claimant attempts must not create truthful-looking transitions.
    assert [entry.node_id for entry in audit if entry.to_status == "running"] == [
        "worker-a"
    ]
    assert not any(entry.node_id == "worker-b" for entry in audit)


def test_worker_route_scopes_switch_only_at_explicit_enforcement(monkeypatch):
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    assert required_scope("POST", "/v1/host/queue/jobs/claim") == "api:access"
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "enforce")
    assert required_scope("POST", "/v1/host/queue/jobs/claim") == "workers:claim"
    assert required_scope(
        "POST", "/v1/host/queue/jobs/id/start"
    ) == "workers:lifecycle"
    assert required_scope(
        "POST", "/v1/host/queue/workers/id/deregister"
    ) == "workers:register"


def test_worker_keyring_rejects_ambiguous_or_unbounded_node_grants(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    _write_keyring(duplicate, [
        _worker_principal("worker-a", "a-key", "shared-node"),
        _worker_principal("worker-b", "b-key", "shared-node"),
    ])
    with pytest.raises(KeyringError, match="multiple principals"):
        load_keyring(duplicate)

    wildcard = tmp_path / "wildcard.json"
    value = _worker_principal("worker-a", "a-key", "worker-a")
    value["worker_grants"][0]["node_id"] = "worker-*"
    _write_keyring(wildcard, [value])
    with pytest.raises(KeyringError, match="node_id is invalid"):
        load_keyring(wildcard)

    no_job_types = tmp_path / "no-job-types.json"
    value = _worker_principal("worker-a", "a-key", "worker-a")
    value["worker_grants"][0]["job_types"] = []
    _write_keyring(no_job_types, [value])
    with pytest.raises(KeyringError, match="job_types must not be empty"):
        load_keyring(no_job_types)

    unknown_job_type = tmp_path / "unknown-job-type.json"
    value = _worker_principal("worker-a", "a-key", "worker-a")
    value["worker_grants"][0]["job_types"] = ["invented"]
    _write_keyring(unknown_job_type, [value])
    with pytest.raises(KeyringError, match="unsupported values"):
        load_keyring(unknown_job_type)

    incomplete_lifecycle = tmp_path / "incomplete-lifecycle.json"
    value = _worker_principal("worker-a", "a-key", "worker-a")
    value["scopes"].remove("workers:lifecycle")
    _write_keyring(incomplete_lifecycle, [value])
    with pytest.raises(KeyringError, match="also requires workers:lifecycle"):
        load_keyring(incomplete_lifecycle)


@pytest.mark.parametrize(
    "credential_id",
    [
        "x" * 193,
        "external credential",
        "external\ncredential",
        " external-credential",
        "external-credential ",
    ],
)
def test_keyring_rejects_noncanonical_credential_ids(tmp_path, credential_id):
    keyring = tmp_path / "invalid-credential-id.json"
    principal = _worker_principal("worker-a", "a-key", "worker-a")
    principal["credentials"][0]["id"] = credential_id
    _write_keyring(keyring, [principal])

    with pytest.raises(KeyringError, match="canonical credential ID"):
        load_keyring(keyring)


@pytest.mark.parametrize(
    "credential_id",
    ["host-approval:bridge/current-v1", "c" + "x" * 191],
)
def test_keyring_accepts_bounded_external_credential_ids(tmp_path, credential_id):
    keyring = tmp_path / "valid-credential-id.json"
    principal = _worker_principal("worker-a", "a-key", "worker-a")
    principal["credentials"][0]["id"] = credential_id
    _write_keyring(keyring, [principal])

    loaded = load_keyring(keyring)
    assert loaded[0].credentials[0].credential_id == credential_id


@pytest.mark.asyncio
async def test_heartbeat_does_not_create_evidence_for_another_claimant(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    job = await _research_job(manager)
    try:
        claimed = await manager.queue.claim_job(
            "worker-a",
            queue_router.WorkerCapabilities(
                node_id="worker-a",
                capabilities={"research"},
                job_types={JobType.RESEARCH},
            ),
        )
        assert claimed is not None
        assert await manager.queue.send_heartbeat(
            "worker-b",
            [job.job_id],
            progress={job.job_id: 0.9},
            claim_attempt_ids={job.job_id: claimed.claim_attempt_id},
        ) == 0
        cursor = await manager.queue._db.execute(
            "SELECT COUNT(*) AS count FROM heartbeats WHERE node_id = ? AND job_id = ?",
            ("worker-b", job.job_id),
        )
        row = await cursor.fetchone()
        stored = await manager.queue.get_job(job.job_id)
    finally:
        await manager.stop()
        TaskQueueManager._instance = None

    assert row["count"] == 0
    assert stored is not None and stored.claimed_by == "worker-a"
