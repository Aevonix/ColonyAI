"""Adversarial tests for immutable ApprovalRequests and bounded grants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Response
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from colony_sidecar.api.authority import RequestAuthority, required_scope
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import task_queue as tq_router
from colony_sidecar.initiatives.approval_authority import (
    ApprovalAuthorityError,
    ApprovalAuthorityStore,
    build_action_binding,
    build_approval_presentation,
    prepare_action_approval,
)
from colony_sidecar.task_queue.models import (
    Job,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager


def _binding(job_id: str, *, pr: str = "17", message: str = "merge"):
    return build_action_binding(
        job_id=job_id,
        job_type="agent_action",
        payload={
            "action_hint": "coding_merge_pr",
            "risk": "destructive",
            "description": message,
            "context": {"PR": pr},
        },
    )


def _merge_payload(*, pr: str = "17", extra=None) -> dict:
    payload = {
        "action_hint": "coding_merge_pr",
        "risk": "destructive",
        "description": "merge",
        "context": {"PR": pr},
    }
    if extra is not None:
        payload["extra"] = extra
    return payload


def _mint_exact_grant(
    store: ApprovalAuthorityStore,
    *,
    source_job_id: str,
    pr: str = "17",
    now=None,
    max_uses: int = 5,
) -> dict:
    source = _binding(source_job_id, pr=pr)
    request = store.ensure_request(
        job_id=source_job_id,
        binding=source,
        presentation=build_approval_presentation(
            job_id=source_job_id,
            job_type="agent_action",
            payload=_merge_payload(pr=pr),
        ),
        now=now,
    )
    return _decide(
        store,
        request,
        source,
        decision_id=f"decision_{source_job_id}",
        grant_scope=source.scope,
        grant_max_uses=max_uses,
        now=now,
    )["grant"]


async def _post_approval_held_job(
    manager: TaskQueueManager,
    job_id: str,
    *,
    job_type: JobType = JobType.AGENT_ACTION,
) -> None:
    if job_type is JobType.AGENT_ACTION:
        job = Job(
            job_id=job_id,
            job_type=job_type,
            payload={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": f"Approve {job_id}",
                "context": {"COMMITMENT_ID": job_id},
            },
        )
    else:
        job = Job(
            job_id=job_id,
            job_type=job_type,
            payload={"description": f"Compatibility hold {job_id}"},
            status=JobStatus.BLOCKED,
            tags={
                "hold_kind": "approval",
                "blocked_reason": "awaiting_owner_approval",
                "awaiting_owner_approval": "true",
            },
        )
    await manager.queue.post(job)


async def _walk_blocked_ids(
    *,
    limit: int,
    task_type: str | None = None,
) -> list[str]:
    after = None
    seen: list[str] = []
    while True:
        page = await tq_router.list_blocked_jobs(
            task_type=task_type,
            limit=limit,
            after=after,
        )
        ids = [item["id"] for item in page]
        assert ids == sorted(ids)
        assert all(item not in seen for item in ids)
        seen.extend(ids)
        if len(page) < limit:
            return seen
        next_after = ids[-1]
        assert after is None or next_after > after
        after = next_after


async def _inject_legacy_approval_hold(
    manager: TaskQueueManager,
    *,
    seed: str,
    legacy_job_id: str,
    job_type: JobType = JobType.AGENT_ACTION,
) -> None:
    await _post_approval_held_job(
        manager, seed, job_type=JobType.CUSTOM,
    )
    assert manager.queue._db is not None
    await manager.queue._db.execute(
        "UPDATE jobs SET job_id=?, job_type=? WHERE job_id=?",
        (legacy_job_id, job_type.value, seed),
    )
    await manager.queue._db.commit()


def _decide(
    store: ApprovalAuthorityStore,
    request: dict,
    binding,
    *,
    decision: str = "approve",
    decision_id: str = "decision_00000001",
    grant_scope=None,
    grant_max_uses: int = 2,
    grant_ttl_seconds: int = 7 * 24 * 60 * 60,
    now=None,
):
    return store.decide(
        request["request_id"],
        decision=decision,
        decision_id=decision_id,
        expected_action_digest=binding.action_digest,
        decided_by="owner-approval-service",
        authority_evidence="scoped_principal:owner-approval-service:key-1",
        grant_scope=grant_scope,
        grant_max_uses=grant_max_uses,
        grant_ttl_seconds=grant_ttl_seconds,
        now=now,
    )


def _request(authority: RequestAuthority) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/host/queue/jobs/job/approve",
        "headers": [],
        "query_string": b"",
        "server": ("127.0.0.1", 7777),
        "client": ("127.0.0.1", 50000),
        "scheme": "http",
    })
    request.state.colony_authority = authority
    return request


def _authority(*scopes: str) -> RequestAuthority:
    return RequestAuthority(
        principal_id="owner-approval-service",
        credential_id="approval-key-1",
        scopes=frozenset({"api:access", *scopes}),
        viewer_person_id="owner",
        person_ids=frozenset({"owner"}),
        audiences=frozenset({"owner"}),
        authenticated=True,
    )


def test_store_closes_every_transient_sqlite_connection(tmp_path, monkeypatch):
    """A context-managed sqlite connection still needs an explicit close.

    Keep strong references to the connections so CPython's refcounting cannot
    hide a lifecycle leak that exhausts macOS's default descriptor limit.
    """

    import colony_sidecar.initiatives.approval_authority as authority_module

    original_connect = authority_module.sqlite3.connect
    opened = []

    class TrackedConnection(sqlite3.Connection):
        explicitly_closed = False

        def close(self):
            self.explicitly_closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(authority_module.sqlite3, "connect", tracked_connect)
    store = ApprovalAuthorityStore(tmp_path / "connection-lifecycle.db")
    assert store.get_request("missing") is None
    assert len(opened) == 2
    assert all(connection.explicitly_closed for connection in opened)


def test_typed_subject_columns_migrate_an_existing_authority_database(tmp_path):
    path = tmp_path / "pre-typed-authority.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE approval_requests ("
            "request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "action_digest TEXT NOT NULL, scope_json TEXT NOT NULL, "
            "scope_digest TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
            "superseded_by TEXT, decision TEXT, decision_id TEXT UNIQUE, "
            "decided_at TEXT, decided_by TEXT, authority_evidence TEXT, "
            "grant_id TEXT, presentation_json TEXT, presentation_digest TEXT)"
        )

    store = ApprovalAuthorityStore(path)
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(approval_requests)"
            ).fetchall()
        }
    assert {
        "subject_kind", "subject_id", "subject_revision", "subject_action",
        "subject_digest",
    }.issubset(columns)
    request = store.ensure_request(
        job_id="queue-compat-after-migration",
        binding=_binding("queue-compat-after-migration"),
    )
    assert request["subject"] is None
    assert request["request_digest_version"] == 1


def test_first_valid_decision_wins_and_exact_replay_is_idempotent(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    binding = _binding("job-1")
    request = store.ensure_request(job_id="job-1", binding=binding)

    first = _decide(store, request, binding)
    assert first["request"]["status"] == "approved"
    assert first["replayed"] is False

    replay = _decide(store, request, binding)
    assert replay["replayed"] is True
    assert replay["request"]["decision_id"] == "decision_00000001"

    with pytest.raises(ApprovalAuthorityError) as exc_info:
        _decide(
            store,
            request,
            binding,
            decision="reject",
            decision_id="decision_00000002",
        )
    assert exc_info.value.code == "decision_already_final"
    assert store.get_request(request["request_id"])["status"] == "approved"


def test_authority_evidence_is_bounded_without_burning_request(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority-evidence.db")
    binding = _binding("job-evidence-bound")
    request = store.ensure_request(job_id="job-evidence-bound", binding=binding)

    with pytest.raises(ApprovalAuthorityError) as exc_info:
        store.decide(
            request["request_id"],
            decision="approve",
            decision_id="decision_evidence_bound",
            expected_action_digest=binding.action_digest,
            decided_by="owner",
            authority_evidence="x" * 513,
        )
    assert exc_info.value.code == "invalid_authority_evidence"
    assert store.get_request(request["request_id"])["status"] == "pending"


def test_stale_digest_and_superseded_request_cannot_authorize(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    old_binding = _binding("job-1", pr="17")
    old = store.ensure_request(job_id="job-1", binding=old_binding)

    with pytest.raises(ApprovalAuthorityError) as exc_info:
        store.decide(
            old["request_id"],
            decision="approve",
            decision_id="decision_stale01",
            expected_action_digest="0" * 64,
            decided_by="owner",
            authority_evidence="scoped_principal:owner:key",
        )
    assert exc_info.value.code == "stale_action_digest"
    assert store.get_request(old["request_id"])["status"] == "pending"

    new_binding = _binding("job-1", pr="99")
    new = store.ensure_request(job_id="job-1", binding=new_binding)
    assert new["request_id"] != old["request_id"]
    assert store.get_request(old["request_id"])["status"] == "superseded"

    with pytest.raises(ApprovalAuthorityError) as exc_info:
        _decide(store, old, old_binding, decision_id="decision_stale02")
    assert exc_info.value.code == "request_superseded"


def test_expired_request_and_cross_request_decision_replay_fail_closed(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    start = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    first_binding = _binding("job-1")
    first = store.ensure_request(
        job_id="job-1", binding=first_binding, ttl_seconds=60, now=start
    )
    with pytest.raises(ApprovalAuthorityError) as exc_info:
        _decide(
            store,
            first,
            first_binding,
            decision_id="decision_expired1",
            now=start + timedelta(seconds=61),
        )
    assert exc_info.value.code == "request_expired"

    second_binding = _binding("job-2")
    second = store.ensure_request(job_id="job-2", binding=second_binding)
    _decide(store, second, second_binding, decision_id="decision_shared01")
    third_binding = _binding("job-3")
    third = store.ensure_request(job_id="job-3", binding=third_binding)
    with pytest.raises(ApprovalAuthorityError) as exc_info:
        _decide(store, third, third_binding, decision_id="decision_shared01")
    assert exc_info.value.code == "decision_replay"


def test_scope_broadening_rejected_without_burning_request(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    binding = _binding("job-1", pr="17")
    request = store.ensure_request(job_id="job-1", binding=binding)
    broadened = dict(binding.scope)
    broadened["constraints"] = {}

    with pytest.raises(ApprovalAuthorityError) as exc_info:
        _decide(
            store,
            request,
            binding,
            decision_id="decision_scope001",
            grant_scope=broadened,
        )
    assert exc_info.value.code == "scope_broadening"
    assert store.get_request(request["request_id"])["status"] == "pending"

    accepted = _decide(
        store,
        request,
        binding,
        decision_id="decision_scope002",
        grant_scope=binding.scope,
    )
    assert accepted["grant"]["scope"] == binding.scope


def test_bounded_grant_exact_scope_exhaustion_and_retry_idempotency(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    source_binding = _binding("job-source", pr="17")
    request = store.ensure_request(job_id="job-source", binding=source_binding)
    approved = _decide(
        store,
        request,
        source_binding,
        decision_id="decision_grant001",
        grant_scope=source_binding.scope,
        grant_max_uses=2,
    )
    grant_id = approved["grant"]["grant_id"]

    first = _binding("job-next-1", pr="17")
    second = _binding("job-next-2", pr="17")
    wrong_scope = _binding("job-wrong", pr="99")
    third = _binding("job-next-3", pr="17")

    assert store.consume_grant(binding=wrong_scope, operation_id="job-wrong") is None
    use_one = store.consume_grant(binding=first, operation_id="job-next-1")
    assert use_one["grant_id"] == grant_id and use_one["uses"] == 1
    retry = store.consume_grant(binding=first, operation_id="job-next-1")
    assert retry["idempotent_reuse"] is True and retry["uses"] == 1
    use_two = store.consume_grant(binding=second, operation_id="job-next-2")
    assert use_two["uses"] == 2 and use_two["status"] == "exhausted"
    assert store.consume_grant(binding=third, operation_id="job-next-3") is None


def test_bounded_grant_expires_without_manual_revocation(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    start = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    source = _binding("job-source")
    request = store.ensure_request(job_id="job-source", binding=source, now=start)
    _decide(
        store,
        request,
        source,
        decision_id="decision_expgrant1",
        grant_scope=source.scope,
        grant_ttl_seconds=60,
        now=start,
    )
    future = _binding("job-future")
    assert store.consume_grant(
        binding=future,
        operation_id="job-future",
        now=start + timedelta(seconds=61),
    ) is None
    assert store.list_grants(now=start + timedelta(seconds=61))[0]["status"] == "expired"


@pytest.mark.parametrize(
    ("request_state", "expected_state"),
    [
        ("pending", "pending"),
        ("approved", "authorized_direct"),
        ("rejected", "rejected"),
        ("expired", "expired"),
        ("superseded", "superseded"),
    ],
)
def test_existing_direct_request_always_wins_before_exact_grant(
    tmp_path, request_state, expected_state,
):
    store = ApprovalAuthorityStore(tmp_path / f"{request_state}.db")
    started = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    target_id = f"job-target-{request_state}"
    payload = _merge_payload()
    first = prepare_action_approval(
        store,
        job_id=target_id,
        job_type="agent_action",
        payload=payload,
        now=started,
    )
    request = first["request"]
    binding = first["binding"]
    observed = started
    if request_state in {"approved", "rejected"}:
        _decide(
            store,
            request,
            binding,
            decision="approve" if request_state == "approved" else "reject",
            decision_id=f"decision_target_{request_state}",
            now=started,
        )
    elif request_state == "expired":
        observed = datetime.fromisoformat(request["expires_at"]) + timedelta(seconds=1)
    elif request_state == "superseded":
        store.supersede(request["request_id"], replaced_by="operator-cancelled")

    grant = _mint_exact_grant(
        store,
        source_job_id=f"source-{request_state}",
        now=started,
    )
    resolved = prepare_action_approval(
        store,
        job_id=target_id,
        job_type="agent_action",
        payload=payload,
        now=observed,
    )

    assert resolved["state"] == expected_state
    assert resolved["request"]["request_id"] == request["request_id"]
    assert store.get_grant_use(binding.action_digest) is None
    assert next(
        item for item in store.list_grants(now=observed)
        if item["grant_id"] == grant["grant_id"]
    )["uses"] == 0


def test_concurrent_grant_resolution_is_one_use_and_idempotent(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority-race.db")
    _mint_exact_grant(
        store, source_job_id="source-race", max_uses=5,
    )
    target_id = "target-race"

    def resolve() -> dict:
        return prepare_action_approval(
            store,
            job_id=target_id,
            job_type="agent_action",
            payload=_merge_payload(),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: resolve(), range(12)))

    assert {item["state"] for item in results} == {"authorized_grant"}
    binding = build_action_binding(
        job_id=target_id, job_type="agent_action", payload=_merge_payload(),
    )
    use = store.get_grant_use(binding.action_digest)
    assert use is not None
    assert use["operation_id"] == target_id
    assert use["uses"] == 1


@pytest.mark.asyncio
async def test_http_approved_by_spoof_is_ignored_and_scope_is_attested(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "complete it",
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        job = await manager.queue.get_job(submitted["id"])
        binding = build_action_binding(
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
        )
        approval_request = ApprovalAuthorityStore().ensure_request(
            job_id=job.job_id, binding=binding
        )
        body = tq_router.JobApproveRequest(
            approved_by="model-claims-to-be-owner",
            approval_request_id=approval_request["request_id"],
            expected_action_digest=binding.action_digest,
            decision_id="decision_http0001",
        )
        response = await tq_router.approve_job(
            job.job_id, body, _request(_authority("approvals:decide")),
        )
        assert response["approved_by"] == "owner-approval-service"
        assert response["approved_by"] != "model-claims-to-be-owner"
        updated = await manager.queue.get_job(job.job_id)
        assert updated.tags["approval_authority"] == "owner-approval-service"
        assert updated.status == JobStatus.QUEUED
        replay = await tq_router.approve_job(
            job.job_id, body, _request(_authority("approvals:decide")),
        )
        assert replay["replayed"] is True
        assert replay["approval_request"]["decision_id"] == "decision_http0001"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_exact_approval_replay_is_idempotent_while_dependency_blocked(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        dependency = Job(job_type=JobType.RESEARCH, payload={"query": "wait"})
        await manager.queue.post(dependency)
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload=_merge_payload(),
            depends_on=[dependency.job_id],
        )
        await manager.queue.post(effect)
        request = ApprovalAuthorityStore().get_request_for_job(effect.job_id)
        assert request is not None
        body = tq_router.JobApproveRequest(
            approval_request_id=request["request_id"],
            expected_action_digest=request["action_digest"],
            decision_id="decision_dependency_replay",
            grant=tq_router.BoundedGrantRequest(max_uses=2),
        )

        first = await tq_router.approve_job(
            effect.job_id, body, _request(_authority("approvals:decide")),
        )
        after_first = await manager.queue.get_job(effect.job_id)
        replay = await tq_router.approve_job(
            effect.job_id, body, _request(_authority("approvals:decide")),
        )
        after_replay = await manager.queue.get_job(effect.job_id)

        assert first["status"] == JobStatus.BLOCKED.value
        assert after_first.tags["blocked_reason"] == "dependencies_pending"
        assert replay["status"] == JobStatus.BLOCKED.value
        assert replay["replayed"] is True
        assert replay["bounded_grant"]["grant_id"] == first["bounded_grant"]["grant_id"]
        assert after_replay.status is JobStatus.BLOCKED
        assert after_replay.tags == after_first.tags
        assert len(ApprovalAuthorityStore().list_grants()) == 1
        projection = await tq_router.get_job_approval_projection(effect.job_id)
        assert projection["authority_mode"] == "enforce"
        assert projection["authorization"]["status"] == "authorized"
        assert projection["queue_authority_state"] == {
            "job_status": "blocked",
            "hold_kind": "dependency",
            "blocked_reason": "dependencies_pending",
        }
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_enforce_mode_rejects_model_or_consumer_without_decision_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={"action_hint": "commitment_mark_complete", "risk": "mutating"},
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        with pytest.raises(HTTPException) as exc_info:
            await tq_router.approve_job(
                submitted["id"],
                tq_router.JobApproveRequest(approved_by="owner"),
                _request(_authority("api:access")),
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "approval_scope_required"
        assert (await manager.queue.get_job(submitted["id"])).status == JobStatus.BLOCKED
    finally:
        await manager.stop()


def test_enforce_middleware_scopes_are_exact(monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    assert required_scope(
        "POST", "/v1/host/queue/jobs/job-1/approve"
    ) == "approvals:decide"
    assert required_scope(
        "GET", "/v1/host/queue/approvals/requests"
    ) == "approvals:read"
    assert required_scope(
        "GET", "/v1/host/queue/approvals/jobs/job-1"
    ) == "approvals:read"
    assert required_scope(
        "DELETE", "/v1/host/queue/approvals/grants/grt-1"
    ) == "approvals:manage"
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enfroce")
    assert required_scope(
        "GET", "/v1/host/queue/jobs/blocked"
    ) == "approvals:read"
    assert required_scope(
        "POST", "/v1/host/queue/approvals/requests/apr-1/decision"
    ) == "approvals:decide"


@pytest.mark.asyncio
async def test_enforce_approval_surfaces_never_allow_anonymous_dev_mode(
    monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=None)
    app.include_router(tq_router.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = [
            await client.get("/v1/host/queue/approvals/requests"),
            await client.get("/v1/host/queue/jobs/blocked"),
            await client.post(
                "/v1/host/queue/approvals/requests/apr-missing/decision",
                json={
                    "decision": "approve",
                    "decision_id": "decision_missing1",
                    "expected_action_digest": "0" * 64,
                },
            ),
            await client.delete("/v1/host/queue/approvals/grants/grt-missing"),
        ]

    for response in responses:
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == (
            "exact_scoped_principal_required"
        )


@pytest.mark.asyncio
async def test_invalid_approval_mode_returns_503_on_read_and_decision(
    monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enfroce")
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=None)
    app.include_router(tq_router.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        read = await client.get("/v1/host/queue/approvals/requests")
        decision = await client.post(
            "/v1/host/queue/approvals/requests/apr-missing/decision",
            json={
                "decision": "approve",
                "decision_id": "decision_missing2",
                "expected_action_digest": "0" * 64,
            },
        )
    for response in (read, decision):
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == (
            "approval_authority_mode_invalid"
        )


@pytest.mark.asyncio
async def test_enforce_approval_reads_require_api_access_and_exact_scope(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    keyring = tmp_path / "approval-read-keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [
            {
                "principal": "approval-reader",
                "allow_unscoped_api": False,
                "scopes": ["api:access", "approvals:read"],
                "audiences": [],
                "credentials": [{
                    "id": "current", "secret": "reader-secret", "status": "active",
                }],
            },
            {
                "principal": "missing-api-access",
                "allow_unscoped_api": False,
                "scopes": ["approvals:read"],
                "audiences": [],
                "credentials": [{
                    "id": "current", "secret": "missing-api-secret", "status": "active",
                }],
            },
        ],
    }))
    keyring.chmod(0o600)
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key="legacy-secret",
        keyring_path=str(keyring),
    )
    app.include_router(tq_router.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            legacy = await client.get(
                "/v1/host/queue/approvals/requests",
                headers={"Authorization": "Bearer legacy-secret"},
            )
            missing_api = await client.get(
                "/v1/host/queue/approvals/requests",
                headers={"Authorization": "Bearer missing-api-secret"},
            )
            exact = await client.get(
                "/v1/host/queue/approvals/requests",
                headers={"Authorization": "Bearer reader-secret"},
            )
    finally:
        await manager.stop()

    assert legacy.status_code == 403
    assert missing_api.status_code == 403
    assert exact.status_code == 200


@pytest.mark.asyncio
async def test_shadow_restricted_approval_principal_uses_exact_route_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    keyring = tmp_path / "shadow-approval-keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "shadow-approval-bridge",
            "allow_unscoped_api": False,
            "scopes": [
                "api:access", "approvals:read", "approvals:decide",
            ],
            "audiences": [],
            "credentials": [{
                "id": "current", "secret": "shadow-bridge-secret",
                "status": "active",
            }],
        }],
    }))
    keyring.chmod(0o600)
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key="legacy-shadow-secret",
        keyring_path=str(keyring),
    )
    app.include_router(tq_router.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            exact = await client.get(
                "/v1/host/queue/jobs/blocked",
                headers={"Authorization": "Bearer shadow-bridge-secret"},
            )
            fallback = await client.get(
                "/v1/host/goals",
                headers={"Authorization": "Bearer shadow-bridge-secret"},
            )
            legacy = await client.get(
                "/v1/host/queue/jobs/blocked",
                headers={"Authorization": "Bearer legacy-shadow-secret"},
            )
    finally:
        await manager.stop()

    assert exact.status_code == 200
    assert legacy.status_code == 200
    assert fallback.status_code == 403
    assert fallback.json()["detail"]["code"] == "unscoped_api_denied"


def test_presentation_is_redacted_bounded_and_digest_bound(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority.db")
    payload = {
        "schema": "WorkOrderV1",
        "version": 1,
        "source": "project_engine",
        "project_id": "project-safe",
        "step_id": "step-safe",
        "action_hint": "agent_project_deliver",
        "objective": "Send the brief; api_key=do-not-leak\nwithout raw context",
        "description": "ignored duplicate",
        "risk_class": "disclosure",
        "recipient_scope": "owner",
        "capability_allowlist": ["messaging:send"],
        "deadline": "2026-07-13T00:00:00+00:00",
        "context_refs": ["secret-memory:never-project"],
    }
    binding = build_action_binding(
        job_id="job-presentation-1",
        job_type="agent_action",
        payload=payload,
    )
    presentation = build_approval_presentation(
        job_id="job-presentation-1",
        job_type="agent_action",
        payload=payload,
        deadline=payload["deadline"],
    )
    request = store.ensure_request(
        job_id="job-presentation-1",
        binding=binding,
        presentation=presentation,
    )

    encoded = json.dumps(request, sort_keys=True)
    assert "do-not-leak" not in encoded
    assert "secret-memory" not in encoded
    assert request["presentation"]["summary"].startswith("Send the brief")
    assert request["presentation"]["effect"] == "disclosure"
    assert request["presentation"]["target"] == "owner"
    assert request["presentation"]["capabilities"] == ["messaging:send"]
    assert len(request["presentation_digest"]) == 64


@pytest.mark.asyncio
async def test_pem_private_key_never_enters_approval_surfaces(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    pem_body = "HOST_PRIVATE_KEY_BYTES_MUST_NEVER_PROJECT_7xQ9"
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        f"{pem_body}\n"
        "-----END PRIVATE KEY-----"
    )
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": f"Complete the commitment with credential {pem}",
                "context": {"COMMITMENT_ID": "pem-redaction"},
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        request = store.get_request_for_job(submitted["id"])
        blocked = await tq_router.list_blocked_jobs(limit=50)
        projection = await tq_router.get_job_approval_projection(submitted["id"])

        for surface in (request, blocked, projection):
            encoded = json.dumps(surface, sort_keys=True)
            assert pem_body not in encoded
            assert "-----BEGIN PRIVATE KEY-----" not in encoded
            assert "-----END PRIVATE KEY-----" not in encoded
            assert "[REDACTED PRIVATE KEY]" in encoded
    finally:
        await manager.stop()


@pytest.mark.parametrize("tamper", ["json", "digest"])
def test_stored_presentation_integrity_fails_closed(tmp_path, tamper):
    path = tmp_path / ("authority-%s.db" % tamper)
    store = ApprovalAuthorityStore(path)
    binding = _binding("job-integrity-%s" % tamper)
    presentation = build_approval_presentation(
        job_id="job-integrity-%s" % tamper,
        job_type="agent_action",
        payload={
            "action_hint": "coding_merge_pr",
            "risk": "destructive",
            "description": "Merge reviewed change",
            "context": {"PR": "17"},
        },
    )
    request = store.ensure_request(
        job_id="job-integrity-%s" % tamper,
        binding=binding,
        presentation=presentation,
    )
    with sqlite3.connect(path) as conn:
        if tamper == "digest":
            conn.execute(
                "UPDATE approval_requests SET presentation_digest=? "
                "WHERE request_id=?",
                ("0" * 64, request["request_id"]),
            )
        else:
            raw = conn.execute(
                "SELECT presentation_json FROM approval_requests WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()[0]
            changed = json.loads(raw)
            changed["summary"] = "Tampered display"
            conn.execute(
                "UPDATE approval_requests SET presentation_json=? WHERE request_id=?",
                (json.dumps(changed, sort_keys=True), request["request_id"]),
            )
    with pytest.raises(ApprovalAuthorityError) as exc_info:
        store.get_request(request["request_id"])
    assert exc_info.value.code == "presentation_integrity_failed"


@pytest.mark.asyncio
async def test_direct_effect_submission_has_authority_before_blocked_read(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "Complete the owner commitment",
                "context": {"COMMITMENT_ID": "commitment-7"},
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        before = store.list_requests(status="pending")
        job = await manager.queue.get_job(submitted["id"])

        assert len(before) == 1
        assert before[0]["job_id"] == job.job_id
        assert job.tags["approval_request_id"] == before[0]["request_id"]
        assert job.tags["approval_request_digest"] == before[0]["request_digest"]

        blocked = await tq_router.list_blocked_jobs(limit=50)
        after = store.list_requests(status="pending")
        assert len(after) == 1
        assert after[0]["request_id"] == before[0]["request_id"]
        assert blocked[0]["approval_request_id"] == before[0]["request_id"]
        assert blocked[0]["request_digest"] == before[0]["request_digest"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reconciler_repairs_partial_legacy_approval_hold(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "Repair this interrupted approval birth",
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        original = store.get_request_for_job(submitted["id"])
        assert original is not None
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "DELETE FROM approval_requests WHERE request_id=?",
                (original["request_id"],),
            )

        repaired_count = await manager.queue.reconcile_blocked_approval_authority()
        repaired = store.get_request_for_job(submitted["id"])
        job = await manager.queue.get_job(submitted["id"])

        assert repaired_count == 1
        assert repaired is not None
        assert repaired["request_id"] != original["request_id"]
        assert job.status is JobStatus.BLOCKED
        assert job.tags["approval_request_id"] == repaired["request_id"]
        assert job.tags["approval_request_digest"] == repaired["request_digest"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reconciler_preserves_rejection_before_available_exact_grant(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params=_merge_payload(),
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        target = store.get_request_for_job(submitted["id"])
        assert target is not None
        binding = build_action_binding(
            job_id=submitted["id"],
            job_type="agent_action",
            payload=_merge_payload(),
        )
        grant = _mint_exact_grant(store, source_job_id="source-crash-repair")
        _decide(
            store,
            target,
            binding,
            decision="reject",
            decision_id="decision_crash_reject",
        )

        # Reproduce a process death after the approval DB commit but before
        # the queue transition: the row is still approval-blocked.
        assert (await manager.queue.get_job(submitted["id"])).status is JobStatus.BLOCKED
        repaired = await manager.queue.reconcile_blocked_approval_authority()
        terminal = await manager.queue.get_job(submitted["id"])

        assert repaired == 1
        assert terminal.status is JobStatus.CANCELLED
        assert terminal.tags["approval_request_id"] == target["request_id"]
        assert terminal.tags["approval_decision_id"] == "decision_crash_reject"
        assert store.get_grant_use(binding.action_digest) is None
        assert next(
            item for item in store.list_grants()
            if item["grant_id"] == grant["grant_id"]
        )["uses"] == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_historical_grant_use_cannot_override_target_rejection(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params=_merge_payload(),
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        request = store.get_request_for_job(submitted["id"])
        binding = build_action_binding(
            job_id=submitted["id"],
            job_type="agent_action",
            payload=_merge_payload(),
        )
        _mint_exact_grant(store, source_job_id="source-historical-race")
        consumed = store.consume_grant(
            binding=binding, operation_id=submitted["id"],
        )
        use = store.get_grant_use(binding.action_digest)
        _decide(
            store,
            request,
            binding,
            decision="reject",
            decision_id="decision_historical_reject",
        )

        projection = await tq_router.get_job_approval_projection(submitted["id"])
        assert projection["authorization"]["kind"] == "request"
        assert projection["authorization"]["status"] == "rejected"
        assert await manager.queue.update_job_status(
            submitted["id"],
            JobStatus.QUEUED,
            reason="simulate_historical_partial_transition",
            tags={
                "action_digest": binding.action_digest,
                "auto_approved_by_policy": "bounded_grant",
                "approval_provenance": "server_bounded_grant",
                "bounded_grant_id": consumed["grant_id"],
                "approved_by": use["granted_by"],
            },
            remove_tags=[
                "hold_kind", "blocked_reason", "awaiting_owner_approval",
            ],
        )
        claimed = await manager.queue.claim_job(
            "action-node",
            WorkerCapabilities(
                node_id="action-node",
                capabilities={"action_plane:v1"},
                job_types={JobType.AGENT_ACTION},
            ),
        )
        reheld = await manager.queue.get_job(submitted["id"])
        assert claimed is None
        assert reheld.status is JobStatus.BLOCKED
        assert reheld.tags["blocked_reason"] == "awaiting_owner_approval"
        assert await manager.queue.reconcile_blocked_approval_authority() == 1
        assert (await manager.queue.get_job(submitted["id"])).status is (
            JobStatus.CANCELLED
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_initial_post_preserves_orphaned_rejection_before_exact_grant(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    target_id = "job-rejected-before-queue-insert"
    payload = _merge_payload()
    try:
        store = ApprovalAuthorityStore()
        pending = prepare_action_approval(
            store,
            job_id=target_id,
            job_type="agent_action",
            payload=payload,
        )
        _decide(
            store,
            pending["request"],
            pending["binding"],
            decision="reject",
            decision_id="decision_preinsert_reject",
        )
        grant = _mint_exact_grant(store, source_job_id="source-preinsert")
        target = Job(
            job_id=target_id,
            job_type=JobType.AGENT_ACTION,
            payload=payload,
        )

        await manager.queue.post(target)
        stored = await manager.queue.get_job(target_id)

        assert stored.status is JobStatus.CANCELLED
        assert stored.tags["approval_request_id"] == pending["request"]["request_id"]
        assert store.get_grant_use(pending["binding"].action_digest) is None
        assert next(
            item for item in store.list_grants()
            if item["grant_id"] == grant["grant_id"]
        )["uses"] == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unserializable_queue_row_neither_prompts_nor_burns_grant(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        store = ApprovalAuthorityStore()
        grant = _mint_exact_grant(store, source_job_id="source-serialization")
        with_grant = Job(
            job_id="bad-json-with-grant",
            job_type=JobType.AGENT_ACTION,
            payload=_merge_payload(extra={"not", "json"}),
        )
        without_grant = Job(
            job_id="bad-json-without-grant",
            job_type=JobType.AGENT_ACTION,
            payload=_merge_payload(pr="99", extra={"still", "not-json"}),
        )

        with pytest.raises(TypeError):
            await manager.queue.post(with_grant)
        with pytest.raises(TypeError):
            await manager.queue.post(without_grant)

        with_binding = build_action_binding(
            job_id=with_grant.job_id,
            job_type=with_grant.job_type.value,
            payload=with_grant.payload,
        )
        assert await manager.queue.get_job(with_grant.job_id) is None
        assert await manager.queue.get_job(without_grant.job_id) is None
        assert store.get_request_for_job(with_grant.job_id) is None
        assert store.get_request_for_job(without_grant.job_id) is None
        assert store.get_grant_use(with_binding.action_digest) is None
        assert next(
            item for item in store.list_grants()
            if item["grant_id"] == grant["grant_id"]
        )["uses"] == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_job_id", ["job with spaces", "j" * 193])
async def test_invalid_queue_job_id_is_rejected_before_approval_or_grant(
    tmp_path, monkeypatch, invalid_job_id,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        store = ApprovalAuthorityStore()
        grant = _mint_exact_grant(store, source_job_id="source-invalid-job-id")
        invalid = Job(
            job_id=invalid_job_id,
            job_type=JobType.AGENT_ACTION,
            payload=_merge_payload(),
        )
        binding = build_action_binding(
            job_id=invalid_job_id,
            job_type=invalid.job_type.value,
            payload=invalid.payload,
        )

        with pytest.raises(ValueError, match="job_id must be a canonical"):
            await manager.queue.post(invalid)

        assert await manager.queue.get_job(invalid_job_id) is None
        assert store.get_request_for_job(invalid_job_id) is None
        assert store.get_grant_use(binding.action_digest) is None
        assert next(
            item for item in store.list_grants()
            if item["grant_id"] == grant["grant_id"]
        )["uses"] == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_job_id",
    [str(uuid.uuid4()), "work-" + "a" * 24],
)
async def test_uuid_and_work_order_queue_ids_remain_valid(
    tmp_path, canonical_job_id,
):
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        job = Job(
            job_id=canonical_job_id,
            job_type=JobType.RESEARCH,
            payload={"query": "canonical id compatibility"},
        )
        assert await manager.queue.post(job) == canonical_job_id
        assert (await manager.queue.get_job(canonical_job_id)).job_id == (
            canonical_job_id
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_queue_first_birth_crash_before_authority_is_safe_and_repairable(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    job = Job(
        job_id="queue-first-before-authority",
        job_type=JobType.AGENT_ACTION,
        payload=_merge_payload(),
    )
    original = manager.queue._materialize_effect_approval

    def crash_before_authority(_job):
        raise RuntimeError("injected crash before approval DB commit")

    monkeypatch.setattr(
        manager.queue, "_materialize_effect_approval", crash_before_authority,
    )
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            await manager.queue.post(job)
        staged = await manager.queue.get_job(job.job_id)
        assert staged.status is JobStatus.BLOCKED
        assert staged.tags["blocked_reason"] == "awaiting_owner_approval"
        assert ApprovalAuthorityStore().get_request_for_job(job.job_id) is None
        assert await manager.queue.get_queued_jobs_sorted(
            datetime.now(timezone.utc)
        ) == []

        monkeypatch.setattr(
            manager.queue, "_materialize_effect_approval", original,
        )
        assert await manager.queue.reconcile_blocked_approval_authority() == 1
        repaired = await manager.queue.get_job(job.job_id)
        requests = ApprovalAuthorityStore().list_requests(status="pending")
        assert repaired.status is JobStatus.BLOCKED
        assert repaired.tags["approval_request_id"] == requests[0]["request_id"]
        assert len(requests) == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_kind", ["pending", "grant", "approve", "reject"])
async def test_queue_first_birth_crash_after_authority_converges_once(
    tmp_path, monkeypatch, authority_kind,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    job = Job(
        job_id=f"queue-first-after-{authority_kind}",
        job_type=JobType.AGENT_ACTION,
        payload=_merge_payload(),
    )
    store = ApprovalAuthorityStore()
    grant = None
    direct = None
    if authority_kind == "grant":
        grant = _mint_exact_grant(
            store, source_job_id="source-after-authority-crash",
        )
    elif authority_kind in {"approve", "reject"}:
        direct = prepare_action_approval(
            store,
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
        )
        _decide(
            store,
            direct["request"],
            direct["binding"],
            decision="approve" if authority_kind == "approve" else "reject",
            decision_id=f"decision_after_{authority_kind}",
        )

    original = manager.queue._materialize_effect_approval

    def crash_after_authority(target):
        original(target)
        raise RuntimeError("injected crash after approval DB commit")

    monkeypatch.setattr(
        manager.queue, "_materialize_effect_approval", crash_after_authority,
    )
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            await manager.queue.post(job)
        staged = await manager.queue.get_job(job.job_id)
        assert staged.status is JobStatus.BLOCKED
        assert staged.tags["blocked_reason"] == "awaiting_owner_approval"
        staged_projection = await tq_router.get_job_approval_projection(job.job_id)
        expected_staged_hold = {
            "pending": ("approval", "awaiting_owner_approval"),
            "grant": (
                "authority_transition", "canonical_authority_release_pending",
            ),
            "approve": (
                "authority_transition", "canonical_authority_release_pending",
            ),
            "reject": (
                "authority_transition", "canonical_authority_terminal_pending",
            ),
        }[authority_kind]
        assert staged_projection["queue_authority_state"] == {
            "job_status": "blocked",
            "hold_kind": expected_staged_hold[0],
            "blocked_reason": expected_staged_hold[1],
        }

        before_requests = store.list_requests()
        before_use = store.get_grant_use(
            build_action_binding(
                job_id=job.job_id,
                job_type=job.job_type.value,
                payload=job.payload,
            ).action_digest
        )
        monkeypatch.setattr(
            manager.queue, "_materialize_effect_approval", original,
        )
        assert await manager.queue.reconcile_blocked_approval_authority() == 1
        converged = await manager.queue.get_job(job.job_id)

        expected = {
            "pending": JobStatus.BLOCKED,
            "grant": JobStatus.QUEUED,
            "approve": JobStatus.QUEUED,
            "reject": JobStatus.CANCELLED,
        }[authority_kind]
        assert converged.status is expected
        if authority_kind == "pending":
            assert len(store.list_requests()) == len(before_requests) == 1
        elif authority_kind == "grant":
            after_use = store.get_grant_use(before_use["action_digest"])
            assert after_use == before_use
            assert next(
                item for item in store.list_grants()
                if item["grant_id"] == grant["grant_id"]
            )["uses"] == 1
        else:
            final_direct = store.get_request(direct["request"]["request_id"])
            assert final_direct["decision"] == authority_kind
            assert len(store.list_requests()) == len(before_requests) == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_exact_request_expiry_terminalizes_blocked_job_without_limbo(
    tmp_path,
):
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "complete the exact commitment",
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        job = await manager.queue.get_job(submitted["id"])
        start = datetime(2026, 7, 12, tzinfo=timezone.utc)
        binding = build_action_binding(
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
        )
        request = ApprovalAuthorityStore(tmp_path / "authority.db").ensure_request(
            job_id=job.job_id,
            binding=binding,
            ttl_seconds=60,
            now=start,
        )
        await manager.queue.merge_job_tags(job.job_id, {
            "approval_request_id": request["request_id"],
            "action_digest": binding.action_digest,
            "approval_expires_at": request["expires_at"],
        })

        expired = await manager.queue.expire_blocked_approvals(
            start + timedelta(seconds=61),
            timeout_hours=72,
        )
        terminal = await manager.queue.get_job(job.job_id)

        assert expired == 1
        assert terminal.status is JobStatus.FAILED
        audit = await manager.queue.get_audit_log(job.job_id)
        assert any(
            item.reason == "canonical_approval_expired" for item in audit
        )
    finally:
        await manager.stop()


def test_delayed_materialization_request_never_outlives_job_deadline(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority-deadline.db")
    issued = datetime(2026, 7, 12, tzinfo=timezone.utc)
    deadline = issued + timedelta(minutes=10)
    delayed = issued + timedelta(minutes=8)
    authority = prepare_action_approval(
        store,
        job_id="job-delayed-approval",
        job_type="agent_action",
        payload={
            "action_hint": "commitment_mark_complete",
            "risk": "mutating",
            "description": "Complete before the stable deadline",
            "context": {"COMMITMENT_ID": "commitment-delayed"},
        },
        deadline=deadline,
        now=delayed,
    )
    request = authority["request"]

    assert authority["state"] == "pending"
    assert datetime.fromisoformat(request["expires_at"]) <= deadline
    assert store.get_request(
        request["request_id"], now=deadline,
    )["status"] == "expired"


def test_reconciliation_does_not_expire_existing_request_early(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "authority-existing-expiry.db")
    started = datetime(2026, 7, 12, tzinfo=timezone.utc)
    payload = {
        "action_hint": "commitment_mark_complete",
        "risk": "mutating",
        "description": "Preserve the canonical request lifetime",
        "context": {"COMMITMENT_ID": "commitment-expiry"},
    }
    original = prepare_action_approval(
        store,
        job_id="job-existing-expiry",
        job_type="agent_action",
        payload=payload,
        approval_started_at=started,
        now=started,
    )
    expires_at = datetime.fromisoformat(original["request"]["expires_at"])

    reconciled = prepare_action_approval(
        store,
        job_id="job-existing-expiry",
        job_type="agent_action",
        payload=payload,
        approval_started_at=started,
        now=expires_at - timedelta(seconds=30),
    )

    assert reconciled["state"] == "pending"
    assert reconciled["request"]["request_id"] == original["request"]["request_id"]
    assert reconciled["request"]["expires_at"] == original["request"]["expires_at"]


@pytest.mark.asyncio
async def test_blocked_projection_never_falls_back_to_secret_job_payload(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    secret = "never-render-this-owner-secret"
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": secret,
                "context": {"OWNER_SECRET": secret},
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        store = ApprovalAuthorityStore()
        request = store.get_request_for_job(submitted["id"])
        assert request is not None
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "DELETE FROM approval_requests WHERE request_id=?",
                (request["request_id"],),
            )

        blocked = await tq_router.list_blocked_jobs(limit=50)

        assert len(blocked) == 1
        assert blocked[0]["projection_status"] == "projection_unavailable"
        assert blocked[0]["action_hint"] is None
        assert blocked[0]["description"] == "Approval presentation unavailable"
        assert secret not in json.dumps(blocked[0])
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_raw_approval_reads_hide_nonqueue_and_orphan_requests(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        store = ApprovalAuthorityStore()
        orphan_binding = _binding("nonqueue-charter-request")
        orphan = store.ensure_request(
            job_id="nonqueue-charter-request",
            binding=orphan_binding,
            presentation=build_approval_presentation(
                job_id="nonqueue-charter-request",
                job_type="agent_action",
                payload=_merge_payload(),
            ),
        )
        queued = await manager.submit(
            task_type="agent_action",
            params=_merge_payload(pr="22"),
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        queue_request = store.get_request_for_job(queued["id"])

        visible = await tq_router.list_approval_requests(status="pending", limit=50)
        assert [item["request_id"] for item in visible] == [
            queue_request["request_id"]
        ]
        with pytest.raises(HTTPException) as exc_info:
            await tq_router.get_approval_request(orphan["request_id"])
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "queue_approval_request_not_found"
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 199, 200, 201, 450])
async def test_blocked_discovery_cursor_pages_exactly_once_without_mutation(
    tmp_path, monkeypatch, count,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        expected = [f"page-{index:04d}" for index in range(count)]
        for job_id in expected:
            await _post_approval_held_job(manager, job_id)
        store = ApprovalAuthorityStore()
        before_requests = {
            item["request_id"]: (
                item["job_id"], item["status"], item["request_digest"],
            )
            for item in store.list_requests(limit=500)
        }
        before_jobs = {
            job.job_id: (job.status.value, dict(job.tags))
            for job in await manager.queue.get_jobs_by_status(JobStatus.BLOCKED)
        }

        seen = await _walk_blocked_ids(limit=200)

        after_requests = {
            item["request_id"]: (
                item["job_id"], item["status"], item["request_digest"],
            )
            for item in store.list_requests(limit=500)
        }
        after_jobs = {
            job.job_id: (job.status.value, dict(job.tags))
            for job in await manager.queue.get_jobs_by_status(JobStatus.BLOCKED)
        }
        assert seen == expected
        assert before_requests == after_requests
        assert before_jobs == after_jobs
        assert len(before_requests) == count
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blocked_discovery_excludes_and_counts_legacy_ids_read_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    canonical = [f"legacy-boundary-{index:03d}" for index in range(199)]
    legacy_ids = [
        "z" * 193,
        "legacy-unicode-☃",
        "legacy\x00nul",
        "legacy\ncontrol",
        "-leading-punctuation",
        "legacy with spaces",
        "H" * 10_000,
    ]
    try:
        for job_id in canonical:
            await _post_approval_held_job(manager, job_id)
        for index, legacy_job_id in enumerate(legacy_ids):
            await _inject_legacy_approval_hold(
                manager,
                seed=f"legacy-seed-{index}",
                legacy_job_id=legacy_job_id,
            )
        before = {
            job.job_id: (job.status.value, dict(job.tags))
            for job in await manager.queue.get_jobs_by_status(JobStatus.BLOCKED)
        }

        all_response = Response()
        all_items = await tq_router.list_blocked_jobs(
            limit=200, response=all_response,
        )
        agent_response = Response()
        agent_items = await tq_router.list_blocked_jobs(
            task_type=JobType.AGENT_ACTION.value,
            limit=200,
            response=agent_response,
        )
        custom_response = Response()
        custom_items = await tq_router.list_blocked_jobs(
            task_type=JobType.CUSTOM.value,
            limit=200,
            response=custom_response,
        )
        after = {
            job.job_id: (job.status.value, dict(job.tags))
            for job in await manager.queue.get_jobs_by_status(JobStatus.BLOCKED)
        }

        assert [item["id"] for item in all_items] == canonical
        assert [item["id"] for item in agent_items] == canonical
        assert custom_items == []
        assert all_response.headers["X-Colony-Blocked-Legacy-Count"] == "7"
        assert agent_response.headers["X-Colony-Blocked-Legacy-Count"] == "7"
        assert custom_response.headers["X-Colony-Blocked-Legacy-Count"] == "0"
        encoded = json.dumps(all_items, ensure_ascii=False)
        assert all(legacy_job_id not in encoded for legacy_job_id in legacy_ids)
        assert before == after
        assert set(legacy_ids).issubset(after)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blocked_discovery_accepts_canonical_boundary_cursors(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    ids = ["a", "a/path", "z" * 192]
    try:
        for job_id in ids:
            await _post_approval_held_job(manager, job_id)
        assert await _walk_blocked_ids(limit=1) == ids
        assert [
            item["id"]
            for item in await tq_router.list_blocked_jobs(
                limit=200, after="a/path",
            )
        ] == ["z" * 192]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blocked_discovery_cursor_validation_and_unknown_position(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        await _post_approval_held_job(manager, "cursor-a")
        await _post_approval_held_job(manager, "cursor-b")
        for invalid in ("", "cursor with spaces", "c" * 193):
            with pytest.raises(HTTPException) as exc_info:
                await tq_router.list_blocked_jobs(limit=200, after=invalid)
            assert exc_info.value.status_code == 422
            assert exc_info.value.detail["code"] == (
                "invalid_blocked_jobs_cursor"
            )

        between = await tq_router.list_blocked_jobs(
            limit=200, after="cursor-aa",
        )
        beyond = await tq_router.list_blocked_jobs(
            limit=200, after="cursor-z",
        )
        assert [item["id"] for item in between] == ["cursor-b"]
        assert beyond == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blocked_discovery_task_type_filter_is_cursor_stable(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        agent_ids = ["filter-agent-1", "filter-agent-2", "filter-agent-3"]
        custom_ids = ["filter-custom-1", "filter-custom-2"]
        for job_id in agent_ids:
            await _post_approval_held_job(manager, job_id)
        for job_id in custom_ids:
            await _post_approval_held_job(
                manager, job_id, job_type=JobType.CUSTOM,
            )

        assert await _walk_blocked_ids(
            limit=1, task_type=JobType.AGENT_ACTION.value,
        ) == agent_ids
        assert await _walk_blocked_ids(
            limit=1, task_type=JobType.CUSTOM.value,
        ) == custom_ids
        assert await _walk_blocked_ids(limit=2) == sorted(
            [*agent_ids, *custom_ids]
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blocked_cursor_walk_handles_removal_and_lower_id_insert(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "shadow")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    initial = ["walk-100", "walk-200", "walk-300", "walk-400"]
    try:
        for job_id in initial:
            await _post_approval_held_job(manager, job_id)
        first = await tq_router.list_blocked_jobs(limit=2)
        first_ids = [item["id"] for item in first]
        assert first_ids == ["walk-100", "walk-200"]

        request = ApprovalAuthorityStore().get_request_for_job("walk-200")
        await tq_router.reject_job(
            "walk-200",
            tq_router.JobRejectRequest(
                approval_request_id=request["request_id"],
                expected_action_digest=request["action_digest"],
                decision_id="decision_cursor_removal",
            ),
        )
        await _post_approval_held_job(manager, "walk-150")

        second = await tq_router.list_blocked_jobs(
            limit=2, after=first_ids[-1],
        )
        walked = [*first_ids, *[item["id"] for item in second]]
        assert walked == initial
        assert len(walked) == len(set(walked))
        assert "walk-150" not in walked

        # A new no-cursor poll eventually observes the lower lexical insert;
        # the rejected row remains absent and no current walk duplicates.
        assert await _walk_blocked_ids(limit=2) == [
            "walk-100", "walk-150", "walk-300", "walk-400",
        ]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_approval_projection_ignores_forged_authorization_tags(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "Complete one owner commitment",
                "context": {"COMMITMENT_ID": "commitment-forged-tags"},
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        await manager.queue.merge_job_tags(submitted["id"], {
            "approved_by": "forged-worker",
            "approval_decision_id": "forged-decision",
            "bounded_grant_id": "forged-grant",
        })

        projection = await tq_router.get_job_approval_projection(submitted["id"])

        assert projection["authorization"]["kind"] == "request"
        assert projection["authorization"]["status"] == "pending"
        assert projection["authorization"]["decision_id"] is None
        assert projection["queue_authority_state"] == {
            "job_status": "blocked",
            "hold_kind": "approval",
            "blocked_reason": "awaiting_owner_approval",
        }
        assert "forged" not in json.dumps(projection)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_approval_job_projection_exposes_exact_direct_authorization(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                "action_hint": "commitment_mark_complete",
                "risk": "mutating",
                "description": "Complete the owner commitment",
                "context": {"COMMITMENT_ID": "commitment-7"},
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        blocked = await tq_router.list_blocked_jobs(limit=50)
        request = blocked[0]
        job = await manager.queue.get_job(submitted["id"])
        await tq_router.approve_job(
            job.job_id,
            tq_router.JobApproveRequest(
                approval_request_id=request["approval_request_id"],
                expected_action_digest=request["action_digest"],
                decision_id="decision_projection01",
            ),
            _request(_authority("approvals:decide")),
        )

        projection = await tq_router.get_job_approval_projection(job.job_id)

        assert projection["schema"] == "ColonyApprovalAuthorizationProjectionV1"
        assert projection["version"] == 1
        assert projection["authority_mode"] == "shadow"
        assert projection["job_id"] == job.job_id
        assert projection["action_digest"] == request["action_digest"]
        assert projection["presentation_digest"] == request["presentation_digest"]
        assert projection["request"]["request_id"] == request["approval_request_id"]
        assert projection["authorization"]["kind"] == "direct_decision"
        assert projection["authorization"]["status"] == "authorized"
        assert projection["authorization"]["decision_id"] == "decision_projection01"
        assert projection["authorization"]["decided_by"] == "owner-approval-service"
        assert projection["authorization"]["authority_evidence"].startswith(
            "scoped_principal:owner-approval-service:"
        )
        assert projection["queue_authority_state"] == {
            "job_status": "queued",
            "hold_kind": None,
            "blocked_reason": None,
        }
        assert len(projection["projection_digest"]) == 64
        digest_input = dict(projection)
        projection_digest = digest_input.pop("projection_digest")
        assert projection_digest == hashlib.sha256(json.dumps(
            digest_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        assert "payload" not in projection
        assert "context" not in json.dumps(projection)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_approval_projection_reports_rejected_cancelled_queue_state(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params=_merge_payload(),
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        request = ApprovalAuthorityStore().get_request_for_job(submitted["id"])
        await tq_router.reject_job(
            submitted["id"],
            tq_router.JobRejectRequest(
                approval_request_id=request["request_id"],
                expected_action_digest=request["action_digest"],
                decision_id="decision_projection_reject",
            ),
            _request(_authority("approvals:decide")),
        )

        projection = await tq_router.get_job_approval_projection(submitted["id"])
        assert projection["authorization"] == {
            "kind": "request",
            "status": "rejected",
            "request_id": request["request_id"],
            "decision_id": "decision_projection_reject",
            "decision": "reject",
            "binding_matches": True,
        }
        assert projection["queue_authority_state"] == {
            "job_status": "cancelled",
            "hold_kind": None,
            "blocked_reason": None,
        }
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_approval_job_projection_exposes_consumed_grant_after_queueing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    store = ApprovalAuthorityStore()
    source_payload = {
        "action_hint": "coding_merge_pr",
        "risk": "destructive",
        "description": "Merge an approved reviewed change",
        "context": {"PR": "17"},
    }
    source_binding = build_action_binding(
        job_id="grant-source-job",
        job_type="agent_action",
        payload=source_payload,
    )
    source_request = store.ensure_request(
        job_id="grant-source-job",
        binding=source_binding,
        presentation=build_approval_presentation(
            job_id="grant-source-job",
            job_type="agent_action",
            payload=source_payload,
        ),
    )
    grant = _decide(
        store,
        source_request,
        source_binding,
        decision_id="decision_grant_projection",
        grant_scope=source_binding.scope,
        grant_max_uses=2,
    )["grant"]

    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                **source_payload,
                "description": "Merge the next change in the exact same scope",
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        projection = await tq_router.get_job_approval_projection(submitted["id"])
        job = await manager.queue.get_job(submitted["id"])

        assert job.status is JobStatus.QUEUED
        assert projection["job_status"] == "queued"
        assert projection["authorization"]["kind"] == "bounded_grant"
        assert projection["authorization"]["status"] == "authorized"
        assert projection["authorization"]["grant_id"] == grant["grant_id"]
        assert projection["authorization"]["source_request_id"] == source_request[
            "request_id"
        ]
        assert projection["authorization"]["decision_id"] == (
            "decision_grant_projection"
        )
        assert projection["authorization"]["operation_id"] == job.job_id
        assert projection["authorization"]["scope_digest"] == projection[
            "scope_digest"
        ]
        assert projection["request"]["request_id"] == source_request[
            "request_id"
        ]
        assert len(projection["binding_digest"]) == 64
        assert len(projection["request_digest"]) == 64
        assert len(projection["presentation_digest"]) == 64
    finally:
        await manager.stop()


@pytest.mark.parametrize(("mutation", "expected_status"), [
    ("missing", "missing_source_request"),
    ("tampered", "invalid_provenance"),
])
@pytest.mark.asyncio
async def test_grant_projection_fails_closed_without_exact_source_request(
    tmp_path,
    monkeypatch,
    mutation,
    expected_status,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    store = ApprovalAuthorityStore()
    source_payload = {
        "action_hint": "coding_merge_pr",
        "risk": "destructive",
        "description": "Merge an approved reviewed change",
        "context": {"PR": "42"},
    }
    source_binding = build_action_binding(
        job_id="grant-provenance-source",
        job_type="agent_action",
        payload=source_payload,
    )
    source_request = store.ensure_request(
        job_id="grant-provenance-source",
        binding=source_binding,
        presentation=build_approval_presentation(
            job_id="grant-provenance-source",
            job_type="agent_action",
            payload=source_payload,
        ),
    )
    grant = _decide(
        store,
        source_request,
        source_binding,
        decision_id="decision_grant_provenance",
        grant_scope=source_binding.scope,
        grant_max_uses=2,
    )["grant"]

    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={
                **source_payload,
                "description": "Merge the next change in the exact same scope",
            },
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        with sqlite3.connect(store.path) as conn:
            if mutation == "missing":
                conn.execute(
                    "DELETE FROM approval_requests WHERE request_id=?",
                    (source_request["request_id"],),
                )
            else:
                conn.execute(
                    "UPDATE approval_requests SET decided_by=? WHERE request_id=?",
                    ("tampered-principal", source_request["request_id"]),
                )

        projection = await tq_router.get_job_approval_projection(submitted["id"])

        assert projection["authorization"]["kind"] == "bounded_grant"
        assert projection["authorization"]["status"] == expected_status
        assert projection["authorization"]["status"] != "authorized"
        assert projection["authorization"]["grant_id"] == grant["grant_id"]
        assert projection["authorization"]["source_request_matches"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_enforce_http_boundary_derives_principal_from_scoped_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    keyring = tmp_path / "keys.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "operator-approval-adapter",
            "scopes": ["api:access", "approvals:decide"],
            "viewer_person_id": "owner",
            "audiences": ["viewer"],
            "credentials": [{
                "id": "c" + "x" * 191,
                "secret": "transport-attested-secret",
                "status": "active",
            }],
        }],
    }))
    keyring.chmod(0o600)

    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        submitted = await manager.submit(
            task_type="agent_action",
            params={"action_hint": "commitment_mark_complete", "risk": "mutating"},
            initial_status=JobStatus.BLOCKED,
            tags={"blocked_reason": "awaiting_owner_approval"},
        )
        job = await manager.queue.get_job(submitted["id"])
        binding = build_action_binding(
            job_id=job.job_id,
            job_type=job.job_type.value,
            payload=job.payload,
        )
        approval_request = ApprovalAuthorityStore().ensure_request(
            job_id=job.job_id, binding=binding
        )

        app = FastAPI()
        app.add_middleware(
            ApiKeyMiddleware,
            api_key=None,
            keyring_path=str(keyring),
        )
        app.include_router(tq_router.router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/v1/host/queue/jobs/{job.job_id}/approve",
                headers={"Authorization": "Bearer transport-attested-secret"},
                json={
                    "approved_by": "body-spoof",
                    "approval_request_id": approval_request["request_id"],
                    "expected_action_digest": binding.action_digest,
                    "decision_id": "decision_asgi0001",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["approved_by"] == "operator-approval-adapter"
        assert response.json()["approved_by"] != "body-spoof"
        evidence = response.json()["approval_request"]["authority_evidence"]
        assert evidence.startswith("scoped_principal:operator-approval-adapter:")
        assert len(evidence) <= 512
    finally:
        await manager.stop()
