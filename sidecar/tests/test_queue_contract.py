"""Authenticated deploy identity and exact-job canary inspection."""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import required_scope
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.task_queue.contract import queue_contract_identity
from colony_sidecar.task_queue.models import (
    Job,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.task_queue.routing import AGENT_SYNC_ROUTE


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
MANIFEST_A = "c" * 64
MANIFEST_B = "d" * 64


@pytest.fixture(autouse=True)
def _contract_env(monkeypatch):
    monkeypatch.setenv("COLONY_RELEASE_COMMIT", COMMIT_A)
    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_A,
    )
    monkeypatch.setenv("COLONY_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("COLONY_AGENT_WORKER_ROUTES", "agent_sync")
    monkeypatch.setenv("COLONY_AGENT_SYNC_WORKER_NODE_ID", "sync-node")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    monkeypatch.delenv("COLONY_HERMES_RUN_WORKER_NODE_ID", raising=False)


def _write_keyring(path, scopes):
    path.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "contract-reader",
            "status": "active",
            "scopes": list(scopes),
            "audiences": [],
            "credentials": [{
                "id": "current",
                "secret": "scoped-secret",
                "status": "active",
            }],
        }],
    }))
    path.chmod(0o600)


def _app(*, legacy_key=None, keyring=None):
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=legacy_key,
        keyring_path=str(keyring) if keyring else None,
    )
    app.include_router(queue_router.router)
    return app


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


@pytest.mark.asyncio
async def test_contract_endpoint_auth_matrix(tmp_path):
    no_auth_app = _app()
    async with AsyncClient(
        transport=ASGITransport(app=no_auth_app), base_url="http://test",
    ) as client:
        no_auth = await client.get("/v1/host/queue/contract")
    assert no_auth.status_code == 503

    legacy_app = _app(legacy_key="legacy-secret")
    async with AsyncClient(
        transport=ASGITransport(app=legacy_app), base_url="http://test",
    ) as client:
        missing = await client.get("/v1/host/queue/contract")
        wrong = await client.get(
            "/v1/host/queue/contract", headers=_headers("wrong"),
        )
        legacy = await client.get(
            "/v1/host/queue/contract", headers=_headers("legacy-secret"),
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert legacy.status_code == 200

    denied_ring = tmp_path / "denied.json"
    _write_keyring(denied_ring, ["api:access"])
    denied_app = _app(keyring=denied_ring)
    async with AsyncClient(
        transport=ASGITransport(app=denied_app), base_url="http://test",
    ) as client:
        denied = await client.get(
            "/v1/host/queue/contract", headers=_headers("scoped-secret"),
        )
    assert denied.status_code == 403

    allowed_ring = tmp_path / "allowed.json"
    _write_keyring(allowed_ring, ["workers:contract"])
    allowed_app = _app(keyring=allowed_ring)
    async with AsyncClient(
        transport=ASGITransport(app=allowed_app), base_url="http://test",
    ) as client:
        allowed = await client.get(
            "/v1/host/queue/contract", headers=_headers("scoped-secret"),
        )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["schema"] == "ColonyQueueContractV1"
    assert body["version"] == 1
    assert body["release"] == {
        "commit": COMMIT_A,
        "artifact_manifest_sha256": MANIFEST_A,
    }
    assert body["claim_attempt"]["missing_attempt_accepted"] is False
    assert body["action_plane_result"] == {
        "request_schema": "ActionReceiptAttestationV1",
        "request_version": 1,
        "response_schema": "ActionReceiptAttestationResultV1",
        "response_version": 1,
        "effect_completion_status_before_attestation": "neutral",
        "terminal_status_after_attestation": "completed",
        "attestation_endpoint": (
            "/v1/host/queue/attestations/jobs/{job_id}"
        ),
        "required_scope": "workers:attest",
        "inspection_scope": "workers:inspect",
        "inspection_authority_fields": [
            "action_digest", "action_result_contract",
            "claim_attempt_id", "verification_pending",
            "success_attestation_schema",
        ],
        "request_fields": [
            "schema", "version", "job_id", "action_digest",
            "claim_attempt_id", "effect_class", "terminal_outcome",
            "receipt_refs", "observed_at", "summary",
        ],
        "response_fields": [
            "schema", "version", "success", "job_id",
            "claim_attempt_id", "action_digest", "verifier_identity",
            "replayed", "job_status", "evidence_sha256",
            "receipt_refs_sha256",
        ],
        "action_digest_bound": True,
        "claim_attempt_bound": True,
        "effect_class_server_verified": True,
        "evidence_sha256_server_computed": True,
        "receipt_refs_bounded_and_scheme_qualified": True,
        "observed_at_bounded_to_completion": True,
        "executor_self_attestation_allowed": False,
        "legacy_bearer_attestation_allowed": False,
        "verifier_principal_may_have_worker_grants": False,
        "exact_replay_status": 200,
        "conflicting_replay_status": 409,
        "raw_receipts_persisted": False,
    }
    assert body["agent_action_routing"]["deployment_owners"][
        "agent_sync"
    ] == "sync-node"
    assert body["deployment_posture"] == {
        "worker_authority_mode": "shadow",
        "generic_agent_job_claims_enabled": False,
        "generic_agent_worker_routes": ["agent_sync"],
        "embedded_worker_enabled": True,
    }
    assert body["provider_context_privacy"] == {
        "schema": "ContextProjectionAttestationV1",
        "version": 1,
        "readiness_endpoint": "/v1/host/context/projection-readiness",
        "required_scope": "context:read",
        "guest_assemble_projection_policy": "scoped_viewer_required",
        "viewer_person_id_must_match_turn_contact": True,
        "guest_scoped_projection_modes": ["shadow", "live"],
        "guest_projection_backends": ["p8", "canonical_sources"],
        "guest_legacy_global_allowed": False,
        "p8_absent_scoped_guest_status": 200,
        "exact_owner_legacy_context_carveout": True,
        "temporary_legacy_bearer_carveout": True,
        "guest_temporal_endpoint_allowed": False,
        "guest_reply_thread_projection_enabled": False,
        "general_plugin_governance_ready": False,
        "general_plugin_follow_on_slice": "hermes-session-governance-v1",
    }
    assert required_scope(
        "GET", "/v1/host/queue/contract",
    ) == "workers:contract"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commit", "manifest"),
    [
        ("", MANIFEST_A),
        ("not-a-commit", MANIFEST_A),
        ("0" * 40, MANIFEST_A),
        (COMMIT_A, ""),
        (COMMIT_A, "not-a-digest"),
        (COMMIT_A, "0" * 64),
    ],
)
async def test_contract_rejects_missing_malformed_or_null_release_identity(
    monkeypatch, commit, manifest,
):
    monkeypatch.setenv("COLONY_RELEASE_COMMIT", commit)
    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", manifest,
    )
    app = _app(legacy_key="legacy-secret")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/host/queue/contract", headers=_headers("legacy-secret"),
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "queue_contract_identity_unavailable"
    )


def test_contract_digest_is_deterministic_and_binds_release_and_owners(
    monkeypatch,
):
    first = queue_contract_identity()
    replay = queue_contract_identity()
    assert first == replay
    assert len(first["contract_sha256"]) == 64

    monkeypatch.setenv("COLONY_RELEASE_COMMIT", COMMIT_B)
    changed_commit = queue_contract_identity()
    assert changed_commit["contract_sha256"] != first["contract_sha256"]

    monkeypatch.setenv("COLONY_RELEASE_COMMIT", COMMIT_A)
    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_B,
    )
    changed_manifest = queue_contract_identity()
    assert changed_manifest["contract_sha256"] != first["contract_sha256"]

    monkeypatch.setenv(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_A,
    )
    monkeypatch.setenv("COLONY_AGENT_SYNC_WORKER_NODE_ID", "new-sync-node")
    changed_owner = queue_contract_identity()
    assert changed_owner["contract_sha256"] != first["contract_sha256"]


@pytest.mark.asyncio
async def test_contract_runtime_thought_readiness_is_dynamic_not_in_digest(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", "thought-node")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "thought.db")
    try:
        before = await queue_router.queue_contract()
        assert before["runtime_readiness"]["thought"]["ready"] is False
        static_digest = before["contract_sha256"]

        assert manager.queue.set_thought_runtime_ready(
            True, node_id="thought-node",
        ) is True
        after = await queue_router.queue_contract()
        assert after["runtime_readiness"]["thought"] == {
            "ready": True,
            "node_id": "thought-node",
            "reason": "thought_handler_ready",
        }
        assert after["contract_sha256"] == static_digest
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_exact_job_inspection_is_authenticated_and_canary_complete(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "true")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        job = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_list_open"},
        )
        await manager.queue.post(job)
        claimed = await manager.queue.claim_job(
            "sync-node",
            WorkerCapabilities(
                node_id="sync-node",
                capabilities={AGENT_SYNC_ROUTE},
                job_types={JobType.AGENT_ACTION},
            ),
        )
        assert claimed is not None

        no_auth_app = _app()
        async with AsyncClient(
            transport=ASGITransport(app=no_auth_app), base_url="http://test",
        ) as client:
            no_auth = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
            )
        assert no_auth.status_code == 503

        legacy_app = _app(legacy_key="legacy-secret")
        async with AsyncClient(
            transport=ASGITransport(app=legacy_app), base_url="http://test",
        ) as client:
            missing = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
            )
            wrong = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
                headers=_headers("wrong"),
            )
            legacy = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
                headers=_headers("legacy-secret"),
            )
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert legacy.status_code == 200

        denied_ring = tmp_path / "inspect-denied.json"
        _write_keyring(denied_ring, ["api:access"])
        denied_app = _app(keyring=denied_ring)
        async with AsyncClient(
            transport=ASGITransport(app=denied_app), base_url="http://test",
        ) as client:
            denied = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
                headers=_headers("scoped-secret"),
            )
        assert denied.status_code == 403

        ring = tmp_path / "inspect.json"
        _write_keyring(ring, ["workers:inspect"])
        app = _app(keyring=ring)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get(
                f"/v1/host/queue/inspection/jobs/{job.job_id}",
                headers=_headers("scoped-secret"),
            )
            missing_job = await client.get(
                "/v1/host/queue/inspection/jobs/not-a-real-job",
                headers=_headers("scoped-secret"),
            )
        assert response.status_code == 200
        assert missing_job.status_code == 404
        inspected = response.json()
        assert inspected["claimed_by"] == "sync-node"
        assert inspected["claim_attempt_id"] == claimed.claim_attempt_id
        assert inspected["claim_expires_at"]
        assert inspected["tags"]["agent_action_route"] == AGENT_SYNC_ROUTE
        assert inspected["tags"]["agent_action_route_node"] == "sync-node"
        assert inspected["capabilities"] == [{
            "name": AGENT_SYNC_ROUTE,
            "minimum": None,
            "preferred": False,
        }]
        assert len(inspected["payload_sha256"]) == 64
        assert inspected["result_sha256"] is None
        assert "payload" not in inspected
        assert "result" not in inspected
        assert required_scope(
            "GET",
            f"/v1/host/queue/inspection/jobs/{job.job_id}",
        ) == "workers:inspect"
    finally:
        await manager.stop()
