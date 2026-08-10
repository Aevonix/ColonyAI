"""Incident-shaped tests for the permanently inert approval relay canary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from colony_sidecar.api.authority import RequestAuthority, required_scope
from colony_sidecar.api.routers import task_queue as tq_router
from colony_sidecar.initiatives.approval_authority import (
    ApprovalAuthorityStore,
    DEFAULT_REQUEST_TTL_SECONDS,
    build_action_binding,
    build_approval_presentation,
)
from colony_sidecar.task_queue.approval_relay_canary import (
    ACTION_HINT,
    APPROVAL_TTL_SECONDS,
    SCHEMA,
    TERMINAL_POLICY,
    build_job,
    idempotency_digest,
    is_exact_job,
)
from colony_sidecar.task_queue.models import JobStatus, WorkerCapabilities
from colony_sidecar.task_queue.queue_manager import TaskQueueManager


def _authority(*scopes: str, legacy: bool = False) -> RequestAuthority:
    return RequestAuthority(
        principal_id="host-approval-bridge" if not legacy else "legacy",
        credential_id="approval-bridge-key-1" if not legacy else "legacy-key",
        scopes=frozenset(scopes),
        viewer_person_id="owner",
        person_ids=frozenset({"owner"}),
        audiences=frozenset({"owner"}),
        authenticated=True,
        legacy=legacy,
    )


def _request(authority: RequestAuthority) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/host/queue/approvals/canary",
        "headers": [],
        "query_string": b"",
        "server": ("127.0.0.1", 7777),
        "client": ("127.0.0.1", 50000),
        "scheme": "http",
    })
    request.state.colony_authority = authority
    return request


def _body(key: str = "relay-canary-idempotency-0001"):
    return tq_router.ApprovalRelayCanaryRequest.model_validate({
        "schema": SCHEMA,
        "version": 1,
        "idempotency_key": key,
    })


async def _manager(tmp_path, monkeypatch) -> TaskQueueManager:
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")


def test_canary_route_has_exact_decision_scope():
    assert required_scope(
        "POST", "/v1/host/queue/approvals/canary"
    ) == "approvals:decide"


@pytest.mark.asyncio
async def test_canary_requires_nonlegacy_scoped_bridge_even_in_shadow(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "shadow")
    try:
        rejected = (
            _authority("api:access"),
            _authority("approvals:decide"),
            _authority("api:access", "approvals:decide", legacy=True),
        )
        for authority in rejected:
            with pytest.raises(HTTPException) as exc_info:
                await tq_router.create_approval_relay_canary(
                    _body(), _request(authority),
                )
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail["code"] == (
                "approval_relay_canary_scope_required"
            )
        assert sum((await manager.queue.get_queue_stats()).by_status.values()) == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_canary_create_is_atomic_and_idempotent_under_concurrency(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        results = await asyncio.gather(*(
            tq_router.create_approval_relay_canary(_body(), _request(authority))
            for _ in range(24)
        ))

        assert sum(result["created"] is True for result in results) == 1
        assert len({result["job_id"] for result in results}) == 1
        assert len({result["request_id"] for result in results}) == 1
        assert len({result["action_digest"] for result in results}) == 1
        assert all(result["job_status"] == "blocked" for result in results)
        assert all(result["external_effect"] is False for result in results)
        assert all(result["terminal_policy"] == TERMINAL_POLICY for result in results)
        jobs = await manager.queue.get_jobs_by_status(JobStatus.BLOCKED)
        assert len(jobs) == 1
        assert is_exact_job(jobs[0])
        requests = ApprovalAuthorityStore().list_requests(limit=100)
        assert len(requests) == 1
        assert requests[0]["request_id"] == results[0]["request_id"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_canary_request_has_one_hour_ttl_and_replay_preserves_it(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        first = await tq_router.create_approval_relay_canary(
            _body("relay-canary-one-hour-window"), _request(authority),
        )
        store = ApprovalAuthorityStore()
        request = store.get_request(first["request_id"])
        assert request is not None
        created_at = datetime.fromisoformat(request["created_at"])
        expires_at = datetime.fromisoformat(request["expires_at"])
        assert APPROVAL_TTL_SECONDS == 60 * 60
        assert (expires_at - created_at).total_seconds() == APPROVAL_TTL_SECONDS

        job = await manager.queue.get_job(first["job_id"])
        assert job is not None
        assert job.tags["approval_expires_at"] == request["expires_at"]

        replay = await tq_router.create_approval_relay_canary(
            _body("relay-canary-one-hour-window"), _request(authority),
        )
        replay_request = store.get_request(replay["request_id"])
        assert replay["created"] is False
        assert replay["request_id"] == first["request_id"]
        assert replay["request_digest"] == first["request_digest"]
        assert replay_request is not None
        assert replay_request["expires_at"] == request["expires_at"]
    finally:
        await manager.stop()


def test_ordinary_approval_request_keeps_generic_default_ttl(tmp_path):
    store = ApprovalAuthorityStore(tmp_path / "ordinary-approval.db")
    observed = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    binding = build_action_binding(
        job_id="ordinary-approval-job",
        job_type="agent_action",
        payload={"action_hint": "ordinary_mutation", "risk": "mutating"},
    )

    request = store.ensure_request(
        job_id="ordinary-approval-job",
        binding=binding,
        now=observed,
    )

    assert DEFAULT_REQUEST_TTL_SECONDS == 24 * 60 * 60
    assert datetime.fromisoformat(request["expires_at"]) == (
        observed + timedelta(seconds=DEFAULT_REQUEST_TTL_SECONDS)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_either_decision_is_terminal_cancelled_and_never_claimed(
    tmp_path, monkeypatch, decision,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        created = await tq_router.create_approval_relay_canary(
            _body("relay-canary-decision-%s" % decision),
            _request(authority),
        )
        decided = await tq_router.decide_approval_request(
            created["request_id"],
            tq_router.ApprovalDecisionRequest(
                decision=decision,
                decision_id="relay_canary_%s_decision" % decision,
                expected_action_digest=created["action_digest"],
            ),
            _request(authority),
        )

        assert decided["status"] == JobStatus.CANCELLED.value
        assert decided["bounded_grant"] is None
        job = await manager.queue.get_job(created["job_id"])
        assert job is not None and is_exact_job(job)
        assert job.status is JobStatus.CANCELLED
        assert job.claimed_by is None
        assert job.claimed_at is None
        assert job.claim_attempt_id is None
        assert job.claim_expires_at is None
        assert job.last_heartbeat is None
        assert job.tags["approval_relay_canary_terminalized"] == "true"
        assert job.tags["approval_relay_canary_decision"] == decision
        projection = await tq_router.get_job_approval_projection(job.job_id)
        assert projection["job_status"] == "cancelled"
        assert projection["request"]["decision"] == decision
        assert projection["presentation"]["effect"] == "none"
        assert projection["presentation"]["risk"] == "calibration"
        audit = await manager.queue.get_audit_log(job.job_id, limit=100)
        assert not any(
            entry.to_status in {"claimed", "running"} for entry in audit
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_canary_first_winner_replay_and_grant_guards(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        created = await tq_router.create_approval_relay_canary(
            _body("relay-canary-first-winner-0001"), _request(authority),
        )
        approve = tq_router.ApprovalDecisionRequest(
            decision="approve",
            decision_id="relay_canary_first_winner",
            expected_action_digest=created["action_digest"],
        )
        first = await tq_router.decide_approval_request(
            created["request_id"], approve, _request(authority),
        )
        replay = await tq_router.decide_approval_request(
            created["request_id"], approve, _request(authority),
        )
        assert first["status"] == replay["status"] == "cancelled"
        assert replay["replayed"] is True

        with pytest.raises(HTTPException) as conflict:
            await tq_router.decide_approval_request(
                created["request_id"],
                tq_router.ApprovalDecisionRequest(
                    decision="reject",
                    decision_id="relay_canary_losing_decision",
                    expected_action_digest=created["action_digest"],
                ),
                _request(authority),
            )
        assert conflict.value.status_code == 409

        second = await tq_router.create_approval_relay_canary(
            _body("relay-canary-grant-forbidden-0001"), _request(authority),
        )
        with pytest.raises(HTTPException) as forbidden:
            await tq_router.decide_approval_request(
                second["request_id"],
                tq_router.ApprovalDecisionRequest(
                    decision="approve",
                    decision_id="relay_canary_grant_attempt",
                    expected_action_digest=second["action_digest"],
                    grant=tq_router.BoundedGrantRequest(),
                ),
                _request(authority),
            )
        assert forbidden.value.detail["code"] == (
            "approval_relay_canary_grant_forbidden"
        )
        assert (await manager.queue.get_job(second["job_id"])).status is JobStatus.BLOCKED
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_scheduler_restart_repairs_winner_crash_only_to_cancelled(
    tmp_path, monkeypatch, decision,
):
    """A winner may commit before the separate queue transition transaction."""

    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        created = await tq_router.create_approval_relay_canary(
            _body("relay-canary-crash-window-%s" % decision),
            _request(authority),
        )
        store = ApprovalAuthorityStore()
        store.decide(
            created["request_id"],
            decision=decision,
            decision_id="relay_canary_crash_winner_%s" % decision,
            expected_action_digest=created["action_digest"],
            decided_by="host-approval-bridge",
            authority_evidence=(
                "scoped_principal:host-approval-bridge:approval-bridge-key-1"
            ),
        )
        stranded = await manager.queue.get_job(created["job_id"])
        assert stranded.status is JobStatus.BLOCKED
        assert store.list_grants() == []
        assert store.get_grant_use(created["action_digest"]) is None
        assert await manager.queue.claim_job(
            "effect-worker",
            WorkerCapabilities(
                node_id="effect-worker",
                capabilities={"action_plane:v1"},
                max_concurrent=1,
            ),
        ) is None

        losing_decision = "reject" if decision == "approve" else "approve"
        with pytest.raises(HTTPException) as loser:
            await tq_router.decide_approval_request(
                created["request_id"],
                tq_router.ApprovalDecisionRequest(
                    decision=losing_decision,
                    decision_id="relay_canary_crash_loser",
                    expected_action_digest=created["action_digest"],
                ),
                _request(authority),
            )
        assert loser.value.status_code == 409
        assert (await manager.queue.get_job(created["job_id"])).status is JobStatus.BLOCKED

        # Reproduce the real process boundary: the approval winner is durable,
        # while only the queue projection is restarted and reconciled.
        await manager.stop()
        manager = await _manager(tmp_path, monkeypatch)
        assert await manager.queue.reconcile_blocked_approval_authority() == 1
        terminal = await manager.queue.get_job(created["job_id"])
        assert terminal.status is JobStatus.CANCELLED
        assert terminal.claimed_by is None
        assert terminal.claimed_at is None
        assert terminal.claim_attempt_id is None
        assert terminal.claim_expires_at is None
        assert terminal.last_heartbeat is None
        assert terminal.tags["approval_relay_canary_decision"] == decision
        assert terminal.tags["approval_relay_canary_terminalized"] == "true"

        recovered = await tq_router.decide_approval_request(
            created["request_id"],
            tq_router.ApprovalDecisionRequest(
                decision=decision,
                decision_id="relay_canary_crash_winner_%s" % decision,
                expected_action_digest=created["action_digest"],
            ),
            _request(authority),
        )
        assert recovered["replayed"] is True
        assert recovered["status"] == "cancelled"
        assert recovered["bounded_grant"] is None
        terminal = await manager.queue.get_job(created["job_id"])
        assert terminal.claim_attempt_id is None
        assert await manager.queue.claim_job(
            "effect-worker",
            WorkerCapabilities(
                node_id="effect-worker",
                capabilities={"action_plane:v1"},
                max_concurrent=1,
            ),
        ) is None
        audit = await manager.queue.get_audit_log(created["job_id"], limit=100)
        assert not any(
            entry.to_status in {"claimed", "running"} for entry in audit
        )
        assert store.list_grants() == []
        assert store.get_grant_use(created["action_digest"]) is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_exact_replay_repairs_historical_queued_canary(
    tmp_path, monkeypatch, decision,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    try:
        created = await tq_router.create_approval_relay_canary(
            _body("relay-canary-queued-replay-%s" % decision),
            _request(authority),
        )
        store = ApprovalAuthorityStore()
        winner_id = "relay_canary_queued_winner_%s" % decision
        store.decide(
            created["request_id"],
            decision=decision,
            decision_id=winner_id,
            expected_action_digest=created["action_digest"],
            decided_by="host-approval-bridge",
            authority_evidence=(
                "scoped_principal:host-approval-bridge:approval-bridge-key-1"
            ),
        )
        assert await manager.queue.update_job_status(
            created["job_id"],
            JobStatus.QUEUED,
            reason="simulate_pre_fix_scheduler_projection",
            remove_tags=[
                "hold_kind", "blocked_reason", "awaiting_owner_approval",
            ],
        )
        assert (await manager.queue.get_job(created["job_id"])).status is (
            JobStatus.QUEUED
        )

        replay = await tq_router.decide_approval_request(
            created["request_id"],
            tq_router.ApprovalDecisionRequest(
                decision=decision,
                decision_id=winner_id,
                expected_action_digest=created["action_digest"],
            ),
            _request(authority),
        )

        assert replay["replayed"] is True
        assert replay["status"] == "cancelled"
        assert replay["bounded_grant"] is None
        terminal = await manager.queue.get_job(created["job_id"])
        assert terminal.status is JobStatus.CANCELLED
        assert terminal.claimed_by is None
        assert terminal.claim_attempt_id is None
        assert await manager.queue.claim_job(
            "effect-worker",
            WorkerCapabilities(
                node_id="effect-worker",
                capabilities={"action_plane:v1"},
                max_concurrent=1,
            ),
        ) is None
        audit = await manager.queue.get_audit_log(created["job_id"], limit=100)
        assert not any(
            entry.to_status in {"claimed", "running"} for entry in audit
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_canary_request_wins_without_spending_matching_bounded_grant(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    authority = _authority("api:access", "approvals:decide")
    key = "relay-canary-bounded-grant-fail-closed"
    try:
        digest = idempotency_digest(key)
        target = build_job(digest)
        target_binding = build_action_binding(
            job_id=target.job_id,
            job_type=target.job_type.value,
            payload=target.payload,
        )
        source_binding = build_action_binding(
            job_id="relay-canary-grant-source",
            job_type=target.job_type.value,
            payload=target.payload,
        )
        assert source_binding.scope_digest == target_binding.scope_digest
        store = ApprovalAuthorityStore()
        source = store.ensure_request(
            job_id="relay-canary-grant-source",
            binding=source_binding,
            presentation=build_approval_presentation(
                job_id=target.job_id,
                job_type=target.job_type.value,
                payload=target.payload,
            ),
        )
        minted = store.decide(
            source["request_id"],
            decision="approve",
            decision_id="relay_canary_source_grant",
            expected_action_digest=source_binding.action_digest,
            decided_by="trusted-test-owner",
            authority_evidence="test:preexisting-exact-scope-grant",
            grant_scope=source_binding.scope,
        )["grant"]
        assert minted is not None and minted["uses"] == 0

        created = await tq_router.create_approval_relay_canary(
            _body(key), _request(authority),
        )

        assert created["job_status"] == "blocked"
        assert created["request_status"] == "pending"
        assert created["request_id"] != source["request_id"]
        assert store.get_grant_use(target_binding.action_digest) is None
        current_grant = next(
            item for item in store.list_grants()
            if item["grant_id"] == minted["grant_id"]
        )
        assert current_grant["uses"] == 0
        job = await manager.queue.get_job(created["job_id"])
        assert job.status is JobStatus.BLOCKED
        assert "bounded_grant_id" not in job.tags
        assert await manager.queue.claim_job(
            "effect-worker",
            WorkerCapabilities(
                node_id="effect-worker",
                capabilities={"action_plane:v1"},
                max_concurrent=1,
            ),
        ) is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_generic_job_route_cannot_forge_reserved_canary(
    tmp_path, monkeypatch,
):
    manager = await _manager(tmp_path, monkeypatch)
    try:
        for payload in (
            {"schema": SCHEMA, "version": 1, "action_hint": "coding_merge_pr"},
            {"action_hint": ACTION_HINT},
            {"action_hint": " approval_relay_canary "},
            {"action_hint": "\tapproval_relay_canary\n"},
        ):
            with pytest.raises(HTTPException) as exc_info:
                await tq_router.create_job(tq_router.JobPostRequest(
                    job_type="agent_action", payload=payload,
                ))
            assert exc_info.value.detail["code"] == (
                "approval_relay_canary_authority_reserved"
            )
        assert sum((await manager.queue.get_queue_stats()).by_status.values()) == 0
    finally:
        await manager.stop()


def test_idempotency_key_is_hashed_and_raw_value_never_enters_payload():
    raw = "relay-canary-private-retry-material"
    digest = idempotency_digest(raw)
    assert len(digest) == 64
    assert raw not in digest
