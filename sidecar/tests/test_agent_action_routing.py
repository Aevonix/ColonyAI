"""Fail-closed agent_action routing, migration, and approval authority."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.api.routers import task_queue as queue_router
from colony_sidecar.cognition.goal_spine import ThoughtJobV1
from colony_sidecar.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.action_receipts import (
    ActionReceiptAttestationV1,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.task_queue.routing import (
    ACTION_PLANE_ROUTE,
    AGENT_SYNC_ROUTE,
    HERMES_RUN_ROUTE,
    WORK_ORDER_ROUTE,
    THOUGHT_ROUTE,
)
from colony_sidecar.work_orders import WorkOrderV1


@pytest.fixture(autouse=True)
def _isolated_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "strict")
    monkeypatch.delenv("COLONY_AGENT_SYNC_WORKER_NODE_ID", raising=False)
    monkeypatch.delenv("COLONY_HERMES_RUN_WORKER_NODE_ID", raising=False)
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)


async def _manager(tmp_path, name="queue.db") -> TaskQueueManager:
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / name)


def _caps(node_id: str, *capabilities: str) -> WorkerCapabilities:
    return WorkerCapabilities(
        node_id=node_id,
        capabilities=set(capabilities),
        job_types={JobType.AGENT_ACTION},
        max_concurrent=1,
    )


def _route_requirements(job: Job) -> list[JobCapabilityRequirement]:
    return [
        item for item in job.capabilities
        if item.name in {
            AGENT_SYNC_ROUTE,
            ACTION_PLANE_ROUTE,
            WORK_ORDER_ROUTE,
            HERMES_RUN_ROUTE,
        }
    ]


def _thought_job(concern_id: str = "concern-thought-route") -> Job:
    concern = SimpleNamespace(
        concern_id=concern_id,
        kind="question",
        viewer_scope="owner",
        shareability="owner_private",
        subject_person_id="owner",
        summary="Evaluate one durable concern",
        last_note="",
        sources=(f"journal:1:event-{concern_id}",),
        last_material_digest=f"material-{concern_id}",
    )
    thought = ThoughtJobV1.for_concern(
        concern,
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "reasoning"),
        now=datetime.now(timezone.utc),
    )
    return Job(
        job_id=thought.thought_job_id,
        job_type=JobType.THOUGHT,
        payload=thought.payload(),
        capabilities=[JobCapabilityRequirement(name="cognition_scoped")],
    )


def _work_order_job(
    *,
    project_id: str = "project-route",
    action_kind: str = "research",
    capabilities: list[JobCapabilityRequirement] | None = None,
) -> Job:
    project = SimpleNamespace(
        id=project_id,
        title="Route authority",
        objective="Exercise one exact routed WorkOrder",
        source="owner",
        subject_person_id="owner",
        entity_ids=(),
        created_at=1_783_871_200.0,
    )
    step = SimpleNamespace(
        id=f"step-{project_id}",
        ordinal=0,
        description="Perform the bounded step",
        action_kind=action_kind,
        boundary_subject="owner",
        work_order_issued_at=1_783_871_200.0,
        created_at=1_783_871_200.0,
    )
    order = WorkOrderV1.for_project_step(
        project,
        step,
        now=datetime(2026, 7, 12, 12, tzinfo=timezone.utc),
    )
    return Job(
        job_id=order.work_order_id,
        job_type=JobType.AGENT_ACTION,
        payload=order.payload(),
        capabilities=capabilities or [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "owner_env", "owner", "eligible"),
    [
        ("sync", "COLONY_AGENT_SYNC_WORKER_NODE_ID", "sync-node",
         {AGENT_SYNC_ROUTE}),
        ("effect", "COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node",
         {ACTION_PLANE_ROUTE}),
        ("work_order", "COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node",
         {WORK_ORDER_ROUTE, ACTION_PLANE_ROUTE}),
        ("hermes", "COLONY_HERMES_RUN_WORKER_NODE_ID", "text-node",
         {HERMES_RUN_ROUTE}),
    ],
)
async def test_only_exact_route_and_owner_wins_simultaneous_claim_race(
    tmp_path, monkeypatch, kind, owner_env, owner, eligible,
):
    monkeypatch.setenv(owner_env, owner)
    manager = await _manager(tmp_path)
    try:
        if kind == "sync":
            job = Job(
                job_type=JobType.AGENT_ACTION,
                payload={"action_hint": "commitment_list_open"},
            )
        elif kind == "effect":
            job = Job(
                job_type=JobType.AGENT_ACTION,
                payload={"action_hint": "agent_deliver_message"},
            )
        elif kind == "work_order":
            job = _work_order_job()
        else:
            job = Job(
                job_type=JobType.AGENT_ACTION,
                payload={"schema": "HermesRunV1", "description": "compose"},
            )
        await manager.queue.post(job)
        if job.status is JobStatus.BLOCKED:
            decision = await queue_router.approve_job(
                job.job_id, queue_router.JobApproveRequest(),
            )
            assert decision["status"] == "queued"

        all_routes = {
            AGENT_SYNC_ROUTE, ACTION_PLANE_ROUTE,
            WORK_ORDER_ROUTE, HERMES_RUN_ROUTE,
        }
        required = set(job.required_capabilities())
        wrong_node = _caps("wrong-node", *(all_routes | required))
        under_capable = _caps(owner, *(required - {next(iter(eligible))}))
        exact = _caps(owner, *required)
        results = await asyncio.gather(
            manager.queue.claim_job("wrong-node", wrong_node),
            manager.queue.claim_job(owner, under_capable),
            manager.queue.claim_job(owner, exact),
        )
        winners = [item for item in results if item is not None]
        assert len(winners) == 1
        assert winners[0].job_id == job.job_id
        assert winners[0].claimed_by == owner
        assert winners[0].claim_attempt_id
        assert winners[0].claim_expires_at is not None
        assert {item.name for item in _route_requirements(winners[0])} == eligible
        assert all(
            item.preferred is False and item.minimum is None
            for item in _route_requirements(winners[0])
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("missing_field", "invalid WorkOrder authority field"),
        ("digest_drift", "authority digest mismatch"),
        ("wrong_job_id", "queue job ID does not match"),
    ],
)
async def test_work_order_post_rejects_malformed_authority(
    tmp_path, variant, message,
):
    manager = await _manager(tmp_path)
    try:
        job = _work_order_job(project_id=f"post-{variant}")
        if variant == "missing_field":
            job.payload.pop("capability_allowlist")
        elif variant == "digest_drift":
            job.payload["objective"] = "caller changed authority"
        else:
            job.job_id = job.job_id + "-spoofed"
        with pytest.raises(ValueError, match=message):
            await manager.queue.post(job)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_work_order_post_rebuilds_payload_tags_and_exact_capabilities(
    tmp_path,
):
    manager = await _manager(tmp_path)
    try:
        job = _work_order_job(
            project_id="canonical-post",
            capabilities=[
                JobCapabilityRequirement(
                    name=WORK_ORDER_ROUTE, minimum=9, preferred=True,
                ),
                JobCapabilityRequirement(name=ACTION_PLANE_ROUTE),
                JobCapabilityRequirement(name="messaging:send"),
                JobCapabilityRequirement(name="caller:extra"),
            ],
        )
        canonical_payload = dict(job.payload)
        job.payload.update({
            "description": "execute caller supplied command",
            "context": {"COMMAND": "rm -rf /"},
            "command": "arbitrary executable extra",
            "target": "owner-private-target",
        })
        await manager.queue.post(job)
        stored = await manager.queue.get_job(job.job_id)
        assert stored.payload == canonical_payload
        assert stored.required_capabilities() == [
            WORK_ORDER_ROUTE,
            ACTION_PLANE_ROUTE,
            "memory:read",
            "web:read",
            "reasoning",
        ]
        assert all(
            not capability.preferred and capability.minimum is None
            for capability in stored.capabilities
        )
        assert stored.tags["work_order_digest"] == (
            stored.payload["work_order_digest"]
        )
        assert "caller:extra" not in stored.required_capabilities()
        assert "messaging:send" not in stored.required_capabilities()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_work_order_post_rejects_partial_caller_selected_routes(tmp_path):
    manager = await _manager(tmp_path)
    try:
        partial = _work_order_job(
            project_id="partial-post",
            capabilities=[JobCapabilityRequirement(name=WORK_ORDER_ROUTE)],
        )
        with pytest.raises(ValueError, match="route capabilities do not match"):
            await manager.queue.post(partial)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_work_order_restart_normalizes_inactive_and_quarantines_malformed(
    tmp_path,
):
    db_path = tmp_path / "work-order-inactive.db"
    manager = await _manager(tmp_path, db_path.name)
    try:
        canonical = _work_order_job(project_id="inactive-canonical")
        malformed = _work_order_job(project_id="inactive-malformed")
        await manager.queue.post(canonical)
        await manager.queue.post(malformed)
        altered_payload = dict(canonical.payload)
        altered_payload.update({
            "description": "unsigned compatibility drift",
            "context": {"COMMAND": "unsigned"},
            "command": "unsigned extra",
        })
        await manager.queue._db.execute(
            "UPDATE jobs SET payload = ?, capabilities = ?, tags = '{}' "
            "WHERE job_id = ?",
            (
                json.dumps(altered_payload),
                json.dumps([
                    {"name": WORK_ORDER_ROUTE, "minimum": 7,
                     "preferred": True},
                    {"name": "caller:extra", "minimum": None,
                     "preferred": False},
                ]),
                canonical.job_id,
            ),
        )
        malformed_payload = dict(malformed.payload)
        malformed_payload.pop("work_order_digest")
        await manager.queue._db.execute(
            "UPDATE jobs SET payload = ? WHERE job_id = ?",
            (json.dumps(malformed_payload), malformed.job_id),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        normalized = await manager.queue.get_job(canonical.job_id)
        assert normalized.payload == canonical.payload
        assert normalized.required_capabilities() == [
            WORK_ORDER_ROUTE, ACTION_PLANE_ROUTE,
            "memory:read", "web:read", "reasoning",
        ]
        quarantined = await manager.queue.get_job(malformed.job_id)
        assert quarantined.status is JobStatus.BLOCKED
        assert quarantined.tags["hold_kind"] == "route_migration"
        assert "invalid WorkOrder authority field" in quarantined.tags[
            "agent_action_route_error"
        ]
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    ["missing_cap", "extra_cap", "payload_extra", "tag_drift"],
)
async def test_work_order_active_restart_holds_without_rewrite(
    tmp_path, monkeypatch, variant,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    db_path = tmp_path / f"work-order-active-{variant}.db"
    manager = await _manager(tmp_path, db_path.name)
    try:
        job = _work_order_job(project_id=f"active-{variant}")
        await manager.queue.post(job)
        claimed = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", *job.required_capabilities()),
        )
        assert claimed is not None
        if variant in {"missing_cap", "extra_cap"}:
            capabilities = [
                {
                    "name": item.name,
                    "minimum": item.minimum,
                    "preferred": item.preferred,
                }
                for item in claimed.capabilities
            ]
            if variant == "missing_cap":
                capabilities = capabilities[:-1]
            else:
                capabilities.append({
                    "name": "caller:extra",
                    "minimum": None,
                    "preferred": False,
                })
            await manager.queue._db.execute(
                "UPDATE jobs SET capabilities = ? WHERE job_id = ?",
                (json.dumps(capabilities), job.job_id),
            )
        elif variant == "payload_extra":
            payload = dict(claimed.payload)
            payload["description"] = "unsigned active executor drift"
            payload["command"] = "unsigned active command"
            await manager.queue._db.execute(
                "UPDATE jobs SET payload = ? WHERE job_id = ?",
                (json.dumps(payload), job.job_id),
            )
        else:
            tags = dict(claimed.tags)
            tags["work_order_digest"] = "f" * 64
            await manager.queue._db.execute(
                "UPDATE jobs SET tags = ? WHERE job_id = ?",
                (json.dumps(tags), job.job_id),
            )
        await manager.queue._db.commit()
        cursor = await manager.queue._db.execute(
            "SELECT payload, capabilities, tags FROM jobs WHERE job_id = ?",
            (job.job_id,),
        )
        before = tuple(await cursor.fetchone())
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        readiness = manager.queue.execution_readiness()
        assert readiness["ready"] is False
        assert "incompatible_active_attempts" in readiness["reason"]
        cursor = await manager.queue._db.execute(
            "SELECT payload, capabilities, tags FROM jobs WHERE job_id = ?",
            (job.job_id,),
        )
        assert tuple(await cursor.fetchone()) == before
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_central_claim_kill_switch_skips_generic_but_keeps_action_plane(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    manager = await _manager(tmp_path)
    try:
        generic = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_list_open"},
        )
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
        )
        await manager.queue.post(generic)
        await manager.queue.post(effect)
        await queue_router.approve_job(
            effect.job_id, queue_router.JobApproveRequest(),
        )

        assert await manager.queue.claim_job(
            "generic-node",
            _caps("generic-node", AGENT_SYNC_ROUTE),
        ) is None
        action = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", ACTION_PLANE_ROUTE),
        )
        assert action is not None and action.job_id == effect.job_id
        assert await manager.queue.release_job(
            action.job_id,
            "action-node",
            claim_attempt_id=action.claim_attempt_id,
        )
        assert await manager.queue.cancel_job(
            effect.job_id, reason="containment_test_complete",
        )

        work_order = _work_order_job(project_id="containment-work-order")
        await manager.queue.post(work_order)
        receipt_lane = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", *work_order.required_capabilities()),
        )
        assert receipt_lane is not None
        assert receipt_lane.job_id == work_order.job_id
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_inactive_restart_canonicalizes_or_quarantines_legacy_rows(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "routes.db"
    manager = await _manager(tmp_path, "routes.db")
    try:
        read = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_list_open"},
        )
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
        )
        wrong = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"schema": "HermesRunV1"},
        )
        preferred = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_list_open"},
        )
        for job in (read, effect, wrong, preferred):
            await manager.queue.post(job)

        for job in (read, effect):
            payload = dict(job.payload)
            payload.pop("risk", None)
            await manager.queue._db.execute(
                """UPDATE jobs SET status = ?, payload = ?,
                          capabilities = '[]', tags = '{}'
                   WHERE job_id = ?""",
                (JobStatus.QUEUED.value, json.dumps(payload), job.job_id),
            )
        await manager.queue._db.execute(
            "UPDATE jobs SET capabilities = ?, tags = '{}' WHERE job_id = ?",
            (
                json.dumps([{
                    "name": ACTION_PLANE_ROUTE,
                    "minimum": None,
                    "preferred": False,
                }]),
                wrong.job_id,
            ),
        )
        await manager.queue._db.execute(
            "UPDATE jobs SET capabilities = ? WHERE job_id = ?",
            (
                json.dumps([
                    {"name": AGENT_SYNC_ROUTE, "minimum": 99,
                     "preferred": True},
                    {"name": AGENT_SYNC_ROUTE, "minimum": None,
                     "preferred": False},
                ]),
                preferred.job_id,
            ),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        migrated_read = await manager.queue.get_job(read.job_id)
        assert migrated_read.status is JobStatus.QUEUED
        assert migrated_read.payload["risk"] == "read_only"
        assert [item.name for item in _route_requirements(migrated_read)] == [
            AGENT_SYNC_ROUTE,
        ]

        migrated_effect = await manager.queue.get_job(effect.job_id)
        assert migrated_effect.payload["risk"] == "mutating"
        assert migrated_effect.status is JobStatus.BLOCKED
        assert migrated_effect.tags["blocked_reason"] == (
            "awaiting_owner_approval"
        )
        assert len(migrated_effect.tags["action_digest"]) == 64
        assert migrated_effect.tags["action_result_contract"] == (
            "ActionReceiptAttestationV1"
        )
        assert [item.name for item in _route_requirements(migrated_effect)] == [
            ACTION_PLANE_ROUTE,
        ]

        quarantined = await manager.queue.get_job(wrong.job_id)
        assert quarantined.status is JobStatus.BLOCKED
        assert quarantined.tags["hold_kind"] == "route_migration"

        canonical = await manager.queue.get_job(preferred.job_id)
        route_caps = _route_requirements(canonical)
        assert len(route_caps) == 1
        assert route_caps[0] == JobCapabilityRequirement(
            name=AGENT_SYNC_ROUTE,
            minimum=None,
            preferred=False,
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_inactive_graduated_effect_migration_restores_gate_and_receipt_contract(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    db_path = tmp_path / "graduated-effect-migration.db"
    manager = await _manager(tmp_path, db_path.name)
    try:
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={
                "action_hint": "commitment_mark_complete",
                "ID": "legacy-commitment",
            },
        )
        await manager.queue.post(effect)
        legacy_payload = dict(effect.payload)
        legacy_payload.pop("risk", None)
        await manager.queue._db.execute(
            "UPDATE jobs SET payload = ?, capabilities = '[]', tags = '{}' "
            "WHERE job_id = ?",
            (json.dumps(legacy_payload), effect.job_id),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        migrated = await manager.queue.get_job(effect.job_id)
        assert migrated.status is JobStatus.BLOCKED
        assert migrated.tags["blocked_reason"] == "awaiting_owner_approval"
        assert "auto_approved_by_policy" not in migrated.tags
        assert len(migrated.tags["action_digest"]) == 64
        assert migrated.tags["action_result_contract"] == (
            "ActionReceiptAttestationV1"
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "ready"),
    [
        ("valid", True),
        ("missing_owner", False),
        ("changed_owner", False),
        ("missing_risk", False),
        ("preferred_route", False),
    ],
)
async def test_active_restart_never_rewrites_route_or_owner_drift(
    tmp_path, monkeypatch, variant, ready,
):
    db_path = tmp_path / f"active-{variant}.db"
    monkeypatch.setenv("COLONY_AGENT_SYNC_WORKER_NODE_ID", "sync-node")
    manager = await _manager(tmp_path, db_path.name)
    try:
        job = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_list_open"},
        )
        await manager.queue.post(job)
        claimed = await manager.queue.claim_job(
            "sync-node", _caps("sync-node", AGENT_SYNC_ROUTE),
        )
        assert claimed is not None
        if variant == "missing_owner":
            tags = dict(claimed.tags)
            tags.pop("agent_action_route_node", None)
            await manager.queue._db.execute(
                "UPDATE jobs SET tags = ? WHERE job_id = ?",
                (json.dumps(tags), job.job_id),
            )
        elif variant == "changed_owner":
            monkeypatch.setenv(
                "COLONY_AGENT_SYNC_WORKER_NODE_ID", "replacement-node"
            )
        elif variant == "missing_risk":
            payload = dict(claimed.payload)
            payload.pop("risk", None)
            await manager.queue._db.execute(
                "UPDATE jobs SET payload = ? WHERE job_id = ?",
                (json.dumps(payload), job.job_id),
            )
        elif variant == "preferred_route":
            await manager.queue._db.execute(
                "UPDATE jobs SET capabilities = ? WHERE job_id = ?",
                (
                    json.dumps([{
                        "name": AGENT_SYNC_ROUTE,
                        "minimum": None,
                        "preferred": True,
                    }]),
                    job.job_id,
                ),
            )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        posture = manager.queue.execution_readiness()
        assert posture["ready"] is ready
        assert posture["routing_ready"] is ready
        if not ready:
            assert "incompatible_active_attempts" in posture["reason"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_active_unapproved_effect_holds_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "active-effect.db"
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    manager = await _manager(tmp_path, db_path.name)
    try:
        job = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
        )
        await manager.queue.post(job)
        await queue_router.approve_job(job.job_id, queue_router.JobApproveRequest())
        claimed = await manager.queue.claim_job(
            "action-node", _caps("action-node", ACTION_PLANE_ROUTE),
        )
        assert claimed is not None
        tags = dict(claimed.tags)
        for key in (
            "approved_by", "approved_at", "approval_request_id",
            "approval_decision_id", "action_digest",
        ):
            tags.pop(key, None)
        await manager.queue._db.execute(
            "UPDATE jobs SET tags = ? WHERE job_id = ?",
            (json.dumps(tags), job.job_id),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=db_path)
    try:
        assert manager.queue.execution_readiness()["ready"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_post_rejects_caller_active_state_and_spoofed_risk_or_approval(
    tmp_path,
):
    manager = await _manager(tmp_path)
    try:
        with pytest.raises(ValueError, match="active claim state is server-owned"):
            await manager.queue.post(Job(
                status=JobStatus.CLAIMED,
                claimed_by="caller",
            ))

        with pytest.raises(ValueError, match="risk does not match"):
            await manager.queue.post(Job(
                job_type=JobType.AGENT_ACTION,
                payload={
                    "action_hint": "agent_git_push",
                    "risk": "read_only",
                },
            ))

        spoofed = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "agent_git_push"},
            tags={"approved_by": "fabricated-owner"},
        )
        await manager.queue.post(spoofed)
        stored = await manager.queue.get_job(spoofed.job_id)
        assert stored.payload["risk"] == "mutating"
        assert stored.status is JobStatus.BLOCKED
        assert "approved_by" not in stored.tags
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_forged_outbound_policy_tags_never_authorize_direct_post_or_claim(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    manager = await _manager(tmp_path)
    try:
        outbound = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "agent_deliver_message"},
            tags={
                "auto_approved_by_policy": "graduated",
                "outbound_target": "contact:forged",
            },
        )
        await manager.queue.post(outbound)
        stored = await manager.queue.get_job(outbound.job_id)
        assert stored.status is JobStatus.BLOCKED
        assert stored.tags["blocked_reason"] == "awaiting_owner_approval"
        assert "auto_approved_by_policy" not in stored.tags
        assert "outbound_target" not in stored.tags

        # Same-process status/tag mutation is not a second authority path.
        assert await manager.queue.update_job_status(
            outbound.job_id,
            JobStatus.QUEUED,
            reason="forged_internal_requeue",
            tags={
                "approved_by": "forged-owner",
                "auto_approved_by_policy": "graduated",
                "outbound_target": "contact:forged",
            },
            remove_tags=[
                "hold_kind", "blocked_reason", "awaiting_owner_approval",
            ],
        )
        claimed = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", ACTION_PLANE_ROUTE),
        )
        assert claimed is None
        reheld = await manager.queue.get_job(outbound.job_id)
        assert reheld.status is JobStatus.BLOCKED
        assert reheld.tags["blocked_reason"] == "awaiting_owner_approval"
        assert "approved_by" not in reheld.tags
        assert "auto_approved_by_policy" not in reheld.tags
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_graduated_mutating_policy_cannot_replace_canonical_authority(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "graduated")
    manager = await _manager(tmp_path)
    try:
        mutation = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
        )
        await manager.queue.post(mutation)
        stored = await manager.queue.get_job(mutation.job_id)
        assert stored.status is JobStatus.BLOCKED
        assert stored.tags["blocked_reason"] == "awaiting_owner_approval"
        assert stored.tags["approval_request_id"].startswith("apr_")
        assert "auto_approved_by_policy" not in stored.tags
        assert "approval_provenance" not in stored.tags
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_action_effect_attestation_releases_dependency_and_writeback(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    manager = await _manager(tmp_path)
    try:
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={
                "action_hint": "commitment_mark_complete",
                "ID": "commitment-1",
                "initiative_id": "initiative-1",
                "description": "Mark the verified commitment complete",
            },
        )
        await manager.queue.post(effect)
        await queue_router.approve_job(
            effect.job_id, queue_router.JobApproveRequest(),
        )
        claimed = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", ACTION_PLANE_ROUTE),
        )
        assert claimed is not None and claimed.job_id == effect.job_id
        assert await manager.queue.start_job(
            claimed.job_id, "action-node", claimed.claim_attempt_id,
        )
        completion = await manager.queue.complete_job(
            claimed.job_id,
            "action-node",
            {
                "status": "completed",
                "summary": "worker reports the commitment was updated",
                "action_plane": {"state": "completed"},
                "goal_id": "goal-1",
                "subtask_id": "subtask-1",
            },
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert completion["job_status"] == "neutral"
        pending = await manager.queue.get_job(effect.job_id)
        assert pending.status is JobStatus.NEUTRAL
        assert pending.tags["verification_pending"] == "true"
        assert pending.tags["action_result_contract"] == (
            "ActionReceiptAttestationV1"
        )

        dependent = Job(depends_on=[effect.job_id])
        await manager.queue.post(dependent)
        assert await manager.queue.unblock_ready_jobs() == 0
        assert (await manager.queue.get_job(
            dependent.job_id
        )).status is JobStatus.BLOCKED

        digest = pending.tags["action_digest"]
        observed_at = datetime.now(timezone.utc).isoformat()

        def receipt(*, attempt=claimed.claim_attempt_id, refs=None):
            return ActionReceiptAttestationV1.from_payload({
                "schema": "ActionReceiptAttestationV1",
                "version": 1,
                "job_id": effect.job_id,
                "action_digest": digest,
                "claim_attempt_id": attempt,
                "effect_class": "mutation",
                "terminal_outcome": "succeeded",
                "receipt_refs": refs or [
                    "commitment-ledger:commitment-1:completed"
                ],
                "observed_at": observed_at,
                "summary": "commitment ledger confirms completion",
            })

        assert not await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt(),
            verifier_identity="action-node",
        )
        assert not await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt(attempt="wrong-attempt"),
            verifier_identity="receipt-verifier",
        )
        attested = await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt(),
            verifier_identity="receipt-verifier",
        )
        assert attested is not None and attested["replayed"] is False
        replayed = await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt(),
            verifier_identity="receipt-verifier",
        )
        assert replayed is not None and replayed["replayed"] is True
        assert not await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt(refs=["commitment-ledger:other"]),
            verifier_identity="receipt-verifier",
        )
        completed = await manager.queue.get_job(effect.job_id)
        assert completed.status is JobStatus.COMPLETED
        assert completed.tags["success_attested"] == "true"
        assert completed.tags["success_verifier_identity"] == (
            "receipt-verifier"
        )
        assert "verification_pending" not in completed.tags
        assert (await manager.queue.get_job(
            dependent.job_id
        )).status is JobStatus.QUEUED

        registry = MagicMock()
        registry.task_queue = manager
        registry.graph.store_memory = AsyncMock(return_value="memory-1")
        registry.goals = MagicMock()
        registry.initiative_store = MagicMock()
        loop = AutonomyLoop(registry=registry)
        await loop._phase_job_writeback()
        registry.initiative_store.complete.assert_called_once()
        registry.goals.on_job_completed.assert_called_once()
        memory = registry.graph.store_memory.await_args.kwargs
        assert memory["metadata"]["verification_pending"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_action_receipt_rejects_stale_or_future_chronology(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    monkeypatch.setenv("COLONY_ACTION_RECEIPT_CLOCK_SKEW_SECS", "1")
    manager = await _manager(tmp_path)
    try:
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={
                "action_hint": "commitment_mark_complete",
                "ID": "chronology",
            },
        )
        await manager.queue.post(effect)
        await queue_router.approve_job(
            effect.job_id, queue_router.JobApproveRequest(),
        )
        claimed = await manager.queue.claim_job(
            "action-node", _caps("action-node", ACTION_PLANE_ROUTE),
        )
        assert claimed is not None
        assert await manager.queue.start_job(
            effect.job_id, "action-node", claimed.claim_attempt_id,
        )
        await manager.queue.complete_job(
            effect.job_id,
            "action-node",
            {"status": "completed", "action_plane": {"state": "completed"}},
            claim_attempt_id=claimed.claim_attempt_id,
        )
        pending = await manager.queue.get_job(effect.job_id)

        def receipt(observed_at):
            return ActionReceiptAttestationV1.from_payload({
                "schema": "ActionReceiptAttestationV1",
                "version": 1,
                "job_id": effect.job_id,
                "action_digest": pending.tags["action_digest"],
                "claim_attempt_id": claimed.claim_attempt_id,
                "effect_class": "mutation",
                "terminal_outcome": "succeeded",
                "receipt_refs": ["commitment-ledger:chronology:completed"],
                "observed_at": observed_at,
                "summary": "chronology check",
            })

        assert not await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt("2020-01-01T00:00:00+00:00"),
            verifier_identity="receipt-verifier",
        )
        assert not await manager.queue.attest_action_success(
            effect.job_id,
            attestation=receipt("2099-01-01T00:00:00+00:00"),
            verifier_identity="receipt-verifier",
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["generic", "work_order"])
async def test_skipped_action_dependency_propagates_without_attestation(
    tmp_path, monkeypatch, kind,
):
    monkeypatch.setenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", "action-node")
    manager = await _manager(tmp_path)
    try:
        if kind == "work_order":
            job = _work_order_job(project_id="skipped-dependency")
        else:
            job = Job(
                job_type=JobType.AGENT_ACTION,
                payload={
                    "action_hint": "commitment_mark_complete",
                    "ID": "skipped-dependency",
                },
            )
        await manager.queue.post(job)
        if job.status is JobStatus.BLOCKED:
            await queue_router.approve_job(
                job.job_id, queue_router.JobApproveRequest(),
            )
        claimed = await manager.queue.claim_job(
            "action-node",
            _caps("action-node", *job.required_capabilities()),
        )
        assert claimed is not None
        assert await manager.queue.start_job(
            job.job_id, "action-node", claimed.claim_attempt_id,
        )
        completion = await manager.queue.complete_job(
            job.job_id,
            "action-node",
            {
                "status": "skipped",
                "reason": "owner policy skipped the action",
                "action_plane": {"state": "skipped"},
            },
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert completion["job_status"] == "neutral"
        skipped = await manager.queue.get_job(job.job_id)
        if kind == "generic":
            # A started mutation that reports "skipped" may still have landed
            # before the worker observed its terminal policy decision. Hold it
            # for exact-attempt reconciliation instead of releasing children.
            assert skipped.tags["verification_pending"] == "true"
            assert skipped.tags["ambiguous_prior_effects"] == "true"
        else:
            assert "verification_pending" not in skipped.tags

        dependent = Job(depends_on=[job.job_id])
        await manager.queue.post(dependent)
        assert dependent.status is JobStatus.BLOCKED
        expected_released = 0 if kind == "generic" else 1
        assert await manager.queue.unblock_ready_jobs() == expected_released
        propagated = await manager.queue.get_job(dependent.job_id)
        assert propagated.status is (
            JobStatus.BLOCKED if kind == "generic" else JobStatus.NEUTRAL
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_approval_does_not_erase_dependency_gate(tmp_path):
    manager = await _manager(tmp_path)
    try:
        dependency = Job()
        await manager.queue.post(dependency)
        effect = Job(
            job_type=JobType.AGENT_ACTION,
            payload={"action_hint": "commitment_mark_complete"},
            depends_on=[dependency.job_id],
        )
        await manager.queue.post(effect)
        decision = await queue_router.approve_job(
            effect.job_id, queue_router.JobApproveRequest(),
        )
        assert decision["status"] == "blocked"
        waiting = await manager.queue.get_job(effect.job_id)
        assert waiting.tags["hold_kind"] == "dependency"
        assert waiting.tags["approved_by"] == "trusted-internal"

        claimed = await manager.queue.claim_job(
            "dependency-worker", WorkerCapabilities(
                node_id="dependency-worker",
            ),
        )
        assert claimed is not None and claimed.job_id == dependency.job_id
        assert await manager.queue.start_job(
            claimed.job_id, "dependency-worker", claimed.claim_attempt_id,
        )
        completed = await manager.queue.complete_job(
            claimed.job_id,
            "dependency-worker",
            {"status": "verified"},
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert completed["transitioned"] is True
        released = await manager.queue.get_job(effect.job_id)
        assert released.status is JobStatus.QUEUED
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_thought_route_race_requires_live_exact_cognition_owner(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", "thought-node")
    manager = await _manager(tmp_path)
    try:
        job = _thought_job()
        await manager.queue.post(job)
        assert manager.queue.execution_readiness()["typed_routes"][
            "thought"
        ]["ready"] is False
        assert manager.queue.set_thought_runtime_ready(
            True, node_id="wrong-node",
        ) is False
        assert manager.queue.set_thought_runtime_ready(
            True, node_id="thought-node",
        ) is True

        aexec = WorkerCapabilities(
            node_id="action-node",
            capabilities={
                ACTION_PLANE_ROUTE, WORK_ORDER_ROUTE, "cognition_scoped",
                THOUGHT_ROUTE,
            },
            job_types={JobType.AGENT_ACTION},
        )
        spoof = WorkerCapabilities(
            node_id="spoof-node",
            capabilities={"cognition_scoped", THOUGHT_ROUTE},
            job_types={JobType.THOUGHT},
        )
        under = WorkerCapabilities(
            node_id="thought-node",
            capabilities={"cognition_scoped"},
            job_types={JobType.THOUGHT},
        )
        exact = WorkerCapabilities(
            node_id="thought-node",
            capabilities={"cognition_scoped", THOUGHT_ROUTE},
            job_types={JobType.THOUGHT},
        )
        results = await asyncio.gather(
            manager.queue.claim_job("action-node", aexec),
            manager.queue.claim_job("spoof-node", spoof),
            manager.queue.claim_job("thought-node", under),
            manager.queue.claim_job("thought-node", exact),
        )
        winners = [item for item in results if item is not None]
        assert len(winners) == 1
        assert winners[0].claimed_by == "thought-node"
        assert winners[0].required_capabilities() == [
            "cognition_scoped", THOUGHT_ROUTE,
        ]
        assert winners[0].tags["thought_route"] == THOUGHT_ROUTE
        assert winners[0].tags["thought_route_node"] == "thought-node"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_thought_readiness_compare_and_set_preserves_valid_owner(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", "thought-node")
    manager = await _manager(tmp_path)
    try:
        queue = manager.queue
        assert queue.set_thought_runtime_ready(
            True, node_id="thought-node",
        ) is True
        healthy = queue.execution_readiness()["typed_routes"]["thought"]
        assert healthy == {
            "ready": True,
            "node_id": "thought-node",
            "reason": "thought_handler_ready",
        }

        assert queue.set_thought_runtime_ready(
            True, node_id="wrong-node",
        ) is False
        assert queue.set_thought_runtime_ready(
            False, node_id="wrong-node", reason="wrong_worker_stopped",
        ) is False
        assert queue.execution_readiness()["typed_routes"]["thought"] == (
            healthy
        )

        assert queue.set_thought_runtime_ready(
            False, node_id="thought-node", reason="owner_stopped",
        ) is True
        cleared = queue.execution_readiness()["typed_routes"]["thought"]
        assert cleared == {
            "ready": False,
            "node_id": None,
            "reason": "owner_stopped",
        }
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_thought_restart_normalizes_inactive_and_holds_active_drift(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", "thought-node")
    inactive_path = tmp_path / "thought-inactive.db"
    manager = await _manager(tmp_path, inactive_path.name)
    try:
        inactive = _thought_job()
        malformed = _thought_job("concern-thought-malformed")
        await manager.queue.post(inactive)
        await manager.queue.post(malformed)
        await manager.queue._db.execute(
            """UPDATE jobs SET capabilities = ?, tags = '{}'
               WHERE job_id = ?""",
            (
                json.dumps([
                    {"name": "cognition_scoped", "minimum": 7,
                     "preferred": True},
                    {"name": "filesystem:write", "minimum": None,
                     "preferred": False},
                    {"name": ACTION_PLANE_ROUTE, "minimum": None,
                     "preferred": False},
                ]),
                inactive.job_id,
            ),
        )
        malformed_payload = dict(malformed.payload)
        malformed_payload["schema"] = "ThoughtJobV0"
        await manager.queue._db.execute(
            "UPDATE jobs SET payload = ? WHERE job_id = ?",
            (json.dumps(malformed_payload), malformed.job_id),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=inactive_path)
    try:
        canonical = await manager.queue.get_job(inactive.job_id)
        assert canonical.required_capabilities() == [
            "cognition_scoped", THOUGHT_ROUTE,
        ]
        assert canonical.tags["thought_route_node"] == "thought-node"
        quarantined = await manager.queue.get_job(malformed.job_id)
        assert quarantined.status is JobStatus.BLOCKED
        assert quarantined.tags["hold_kind"] == "route_migration"
        assert quarantined.tags["blocked_reason"] == "thought_route_unresolved"
    finally:
        await manager.stop()

    active_path = tmp_path / "thought-active.db"
    manager = await _manager(tmp_path, active_path.name)
    try:
        active = _thought_job()
        await manager.queue.post(active)
        assert manager.queue.set_thought_runtime_ready(
            True, node_id="thought-node",
        )
        claimed = await manager.queue.claim_job(
            "thought-node",
            WorkerCapabilities(
                node_id="thought-node",
                capabilities={"cognition_scoped", THOUGHT_ROUTE},
                job_types={JobType.THOUGHT},
            ),
        )
        assert claimed is not None
        tags = dict(claimed.tags)
        tags.pop("thought_route_node")
        await manager.queue._db.execute(
            "UPDATE jobs SET tags = ? WHERE job_id = ?",
            (json.dumps(tags), active.job_id),
        )
        await manager.queue._db.commit()
    finally:
        await manager.stop()

    manager = await TaskQueueManager.initialize(db_path=active_path)
    try:
        readiness = manager.queue.execution_readiness()
        assert readiness["routing_ready"] is False
        assert "incompatible_active_thought_attempts" in readiness["reason"]
        assert readiness["typed_routes"]["thought"]["ready"] is False
    finally:
        await manager.stop()
