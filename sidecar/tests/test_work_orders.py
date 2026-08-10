import copy
import importlib
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.initiatives.approval_authority import (
    ApprovalAuthorityStore,
    build_action_binding,
    build_approval_presentation,
)
from colony_sidecar.projects import Project, ProjectEngine, ProjectStore, Step
from colony_sidecar.self_model import (
    ActionJournal,
    CompetenceStore,
    SelfModel,
    TrustEngine,
)
from colony_sidecar.task_queue.governor import WorkerGovernor
from colony_sidecar.task_queue.models import (
    Job,
    JobResult,
    JobStatus,
    JobType,
    WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.task_queue.queue_manager import QueueManager
from colony_sidecar.workers.queue_worker import AGENT_ACTION_CAPABILITIES
from colony_sidecar.work_orders import (
    QueueWorkOrderAdapter,
    ReceiptVerifierConfigurationError,
    WorkOrderV1,
    load_receipt_verifier_from_env,
)


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.posts = 0

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def post(self, job):
        if job.job_id in self.jobs:
            raise AssertionError("duplicate work order post")
        self.jobs[job.job_id] = job
        self.posts += 1
        return job.job_id

    async def merge_job_tags(self, job_id, tags):
        self.jobs[job_id].tags.update(tags)
        return True

    async def update_job_status(
        self,
        job_id,
        status,
        *,
        reason="",
        tags=None,
        remove_tags=None,
    ):
        del reason
        job = self.jobs[job_id]
        job.status = status
        for key in remove_tags or ():
            job.tags.pop(key, None)
        job.tags.update(tags or {})
        return True

    async def attest_job_success(
        self,
        job_id,
        *,
        report,
        verifier_identity,
        verifier_type="independent_receipt",
    ):
        job = self.jobs[job_id]
        result = report.get("execution_result", {})
        if (
            job.status not in {JobStatus.NEUTRAL, JobStatus.COMPLETED}
            or result.get("terminal_outcome") != "succeeded"
            or result.get("verification_result") != "verified"
            or result.get("verifier_identity") != verifier_identity
        ):
            return False
        job.status = JobStatus.COMPLETED
        job.tags["success_attested"] = "true"
        job.tags["success_verifier_identity"] = verifier_identity
        job.tags["success_verifier_type"] = verifier_type
        if job.result is not None:
            job.result.status = JobStatus.COMPLETED
        return True


class FakeManager:
    def __init__(self):
        self.queue = FakeQueue()


class IndependentArtifactVerifier:
    """Test-only stand-in for a server-owned artifact/evidence resolver."""

    identity = "test-artifact-resolver:v1"
    verifier_type = "artifact_resolver"

    def verify(self, *, result, **_kwargs):
        return {
            "verified": bool(result.receipt_refs),
            "receipt_refs": list(result.receipt_refs),
            "verifier_identity": self.identity,
            "detail": "bounded refs resolved by test artifact store",
        }


class AllowDirectives:
    def check(self, _action):
        return SimpleNamespace(allowed=True, reason="ok")


def test_receipt_verifier_loader_is_optional_and_host_agnostic():
    assert load_receipt_verifier_from_env({}) is None


def test_receipt_verifier_loader_builds_exact_configured_factory(monkeypatch):
    observed = {}

    class Verifier:
        identity = "test-verifier:v1"

        def __init__(self, **config):
            observed.update(config)

        def verify(self, **_kwargs):
            return {"verified": False}

    module = SimpleNamespace(factory=SimpleNamespace(Verifier=Verifier))
    monkeypatch.setattr(importlib, "import_module", lambda name: module)
    verifier = load_receipt_verifier_from_env({
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER": "test_plugin:factory.Verifier",
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER_CONFIG": (
            '{"action_db":"/state/actions.db","expected_executor_identity":"node-a"}'
        ),
    })
    assert isinstance(verifier, Verifier)
    assert observed == {
        "action_db": "/state/actions.db",
        "expected_executor_identity": "node-a",
    }


@pytest.mark.parametrize("environment", [
    {"COLONY_WORK_ORDER_RECEIPT_VERIFIER": "not-an-import"},
    {
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER": "plugin:Verifier",
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER_CONFIG": "[]",
    },
    {
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER": "plugin:Verifier",
        "COLONY_WORK_ORDER_RECEIPT_VERIFIER_CONFIG": "{broken",
    },
])
def test_receipt_verifier_loader_rejects_invalid_configuration(environment):
    with pytest.raises(ReceiptVerifierConfigurationError):
        load_receipt_verifier_from_env(environment)


def test_receipt_verifier_loader_rejects_invalid_interface(monkeypatch):
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: SimpleNamespace(Verifier=lambda: object()),
    )
    with pytest.raises(ReceiptVerifierConfigurationError, match="verify interface"):
        load_receipt_verifier_from_env({
            "COLONY_WORK_ORDER_RECEIPT_VERIFIER": "plugin:Verifier",
        })


def project_step(kind="research"):
    project = Project(
        id="proj-work-order-1",
        title="Build a durable brief",
        objective="Research and verify the topic",
        source="thinker",
        status="active",
    )
    step = Step(
        id="step-work-order-1",
        project_id=project.id,
        ordinal=1,
        description="Research the current primary sources",
        action_kind=kind,
    )
    return project, step


def runtime_fence_work_order(
    store, *, suffix="turn", source="cognition_spine", concern_id="turn-concern",
):
    project = Project(
        id=f"proj-runtime-{suffix}",
        title=f"Runtime fence {suffix}",
        objective="Research the bounded source state",
        source=source,
        status="active",
        concern_id=concern_id,
        viewer_scope="owner",
    )
    step = Step(
        id=f"step-runtime-{suffix}",
        project_id=project.id,
        ordinal=1,
        description="Research the current primary sources",
        action_kind="research",
    )
    store.save_project(project)
    store.save_step(step)
    order = WorkOrderV1.for_project_step(project, step)
    store.prepare_work_order(project, step, order)
    return Job(
        job_id=order.work_order_id,
        job_type=JobType.AGENT_ACTION,
        payload=order.payload(),
        deadline=datetime.fromisoformat(order.deadline),
        tags={
            "approved_by": "owner:test",
            "approval_request_id": "approval-preserved",
            "evidence_ref": "evidence:preserved",
        },
    )


def runtime_fence_worker(job, *, worker_id="runtime-worker", concurrent=1):
    return WorkerCapabilities(
        node_id=worker_id,
        capabilities=set(job.required_capabilities()),
        max_concurrent=concurrent,
        job_types={JobType.AGENT_ACTION, JobType.RESEARCH},
    )


def execution_claim(job, *, outcome="succeeded", attempt=1, refs=(), **updates):
    risk = job.payload["risk_class"]
    effect = risk if risk in {"read_only", "mutation", "disclosure"} else "none"
    claim = {
        "schema": "ExecutionResultV1",
        "version": 1,
        "work_order_id": job.payload["work_order_id"],
        "work_order_digest": job.payload["work_order_digest"],
        "work_order_version": job.payload["version"],
        "run_id": f"run-{attempt}",
        "attempt_number": attempt,
        "terminal_outcome": outcome,
        "started_at": f"2026-07-12T0{attempt}:00:00+00:00",
        "ended_at": f"2026-07-12T0{attempt}:00:01+00:00",
        "executor_identity": "host-action-executor",
        "effect_class": effect,
        "receipt_refs": list(refs),
        "verification_result": "verified",  # an executor claim, never trusted
        "summary": "primary sources verified",
        "error": "" if outcome == "succeeded" else "attempt failed",
    }
    claim.update(updates)
    return {"execution_result": claim}


@pytest.mark.asyncio
async def test_stale_project_cannot_reopen_terminal_parent_or_post_work():
    manager = FakeManager()
    store = ProjectStore()
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    stale_project = copy.deepcopy(project)
    stale_step = copy.deepcopy(step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "already_terminal"
    store.save_project(project)
    adapter = QueueWorkOrderAdapter(manager, project_store=store)

    result = await adapter.execute(stale_project, stale_step)

    assert result[0] is False
    assert "terminal" in result[1]
    assert store.get_project(project.id).status == "completed"
    assert manager.queue.posts == 0
    assert manager.queue.jobs == {}


@pytest.mark.asyncio
async def test_stale_step_cannot_reopen_terminal_parent_or_post_work():
    manager = FakeManager()
    store = ProjectStore()
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    stale_step = copy.deepcopy(step)
    step.status = "done"
    store.save_step(step)
    adapter = QueueWorkOrderAdapter(manager, project_store=store)

    result = await adapter.execute(project, stale_step)

    assert result[0] is False
    assert "terminal" in result[1]
    assert store.steps_for(project.id)[0].status == "done"
    assert manager.queue.posts == 0
    assert manager.queue.jobs == {}


@pytest.mark.asyncio
async def test_stale_project_provenance_cannot_bind_a_work_order():
    manager = FakeManager()
    store = ProjectStore()
    project, step = project_step()
    project.source = "cognition_spine"
    project.concern_id = "concern:persisted-authority"
    project.goal_proposal_id = "goal-proposal:persisted-authority"
    store.save_project(project)
    store.save_step(step)
    stale_project = copy.deepcopy(project)
    stale_project.concern_id = "concern:stale-divergent-authority"
    adapter = QueueWorkOrderAdapter(manager, project_store=store)

    result = await adapter.execute(stale_project, step)

    assert result[0] is False
    assert "differs from persisted parent" in result[1]
    assert store.get_project(project.id).concern_id == (
        "concern:persisted-authority"
    )
    assert manager.queue.posts == 0
    assert manager.queue.jobs == {}


def test_work_order_is_stable_bounded_and_reference_only():
    project, step = project_step()
    fixed = datetime(2026, 7, 12, tzinfo=timezone.utc)
    first = WorkOrderV1.for_project_step(
        project,
        step,
        context_refs=("memory:abc", "turn:def"),
        now=fixed,
    )
    replay = WorkOrderV1.for_project_step(
        project,
        step,
        context_refs=("turn:def", "memory:abc"),
        now=fixed,
    )
    assert first.work_order_id == replay.work_order_id
    assert first.idempotency_key == replay.idempotency_key
    payload = first.payload()
    assert payload["schema"] == "WorkOrderV1"
    assert payload["version"] == 1
    assert payload["capability_allowlist"] == ["memory:read", "web:read", "reasoning"]
    assert payload["context_refs"] == ["memory:abc", "turn:def"]
    assert "api_key" not in payload["context"]
    assert payload["max_attempts"] == 2
    assert payload["work_order_digest"] == first.work_order_digest


def test_work_order_redacts_inline_credentials():
    project, step = project_step()
    project.objective = "Inspect api_key=do-not-copy and summarize"
    order = WorkOrderV1.for_project_step(project, step)
    assert "do-not-copy" not in order.objective
    assert "api_key=[REDACTED]" in order.objective


def test_directed_work_order_names_exact_effect_capabilities():
    project, step = project_step("directed")
    order = WorkOrderV1.for_project_step(project, step)
    assert "agent:tools" not in order.capability_allowlist
    assert set(order.capability_allowlist) == {
        "code:execute",
        "filesystem:read",
        "filesystem:write",
        "git:write",
        "memory:read",
        "reasoning",
        "terminal:execute",
        "web:read",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["directed", "deliver"])
async def test_effect_work_order_is_approval_blocked_and_capability_bound(kind):
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step(kind)
    waiting = await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))

    assert waiting[0] is None
    assert waiting[1].endswith(":blocked")
    assert job.status is JobStatus.BLOCKED
    assert job.tags["blocked_reason"] == "awaiting_owner_approval"
    assert job.timeout_secs == float(job.payload["max_runtime_seconds"])
    assert set(job.required_capabilities()) == {
        "action_plane:v1",
        "work_order:v1",
        *job.payload["capability_allowlist"],
    }
    assert not WorkerCapabilities(
        node_id="under-capable",
        capabilities={"reasoning"},
        job_types={JobType.AGENT_ACTION},
    ).can_accept(job)
    assert "work_order:v1" not in AGENT_ACTION_CAPABILITIES
    assert not WorkerCapabilities(
        node_id="generic-hermes-bridge",
        capabilities=set(AGENT_ACTION_CAPABILITIES),
        job_types={JobType.AGENT_ACTION},
    ).can_accept(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["directed", "deliver"])
async def test_effect_work_order_materializes_one_canonical_request_at_birth(
    tmp_path,
    kind,
):
    manager = FakeManager()
    approvals = ApprovalAuthorityStore(tmp_path / "approval-authority.db")
    adapter = QueueWorkOrderAdapter(manager, approval_authority=approvals)
    project, step = project_step(kind)

    waiting = await adapter.execute(project, step)
    replay = await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    requests = approvals.list_requests()

    assert waiting[0] is None and waiting[1].endswith(":blocked")
    assert replay == waiting
    assert len(requests) == 1
    request = requests[0]
    assert request["job_id"] == job.job_id
    assert request["status"] == "pending"
    assert request["request_id"] == job.tags["approval_request_id"]
    assert request["action_digest"] == job.tags["action_digest"]
    assert request["scope_digest"] == job.tags["approval_scope_digest"]
    assert request["presentation_digest"] == job.tags["approval_presentation_digest"]
    assert job.tags["approval_expires_at"] == request["expires_at"]
    assert request["presentation"]["schema"] == "ColonyApprovalPresentationV1"
    assert request["presentation"]["summary"]
    assert request["presentation"]["risk"] == job.payload["risk_class"]
    assert request["presentation"]["effect"] == job.payload["risk_class"]
    assert "context_refs" not in request["presentation"]


@pytest.mark.asyncio
async def test_effect_work_order_consumes_exact_bounded_grant_before_queueing(
    tmp_path,
):
    approvals = ApprovalAuthorityStore(tmp_path / "approval-authority.db")
    first_project, first_step = project_step("directed")
    first_order = WorkOrderV1.for_project_step(first_project, first_step)
    first_payload = first_order.payload()
    first_binding = build_action_binding(
        job_id=first_order.work_order_id,
        job_type=JobType.AGENT_ACTION.value,
        payload=first_payload,
    )
    first_presentation = build_approval_presentation(
        job_id=first_order.work_order_id,
        job_type=JobType.AGENT_ACTION.value,
        payload=first_payload,
        deadline=first_order.deadline,
    )
    request = approvals.ensure_request(
        job_id=first_order.work_order_id,
        binding=first_binding,
        presentation=first_presentation,
    )
    decided = approvals.decide(
        request["request_id"],
        decision="approve",
        decision_id="owner-decision-work-order-1",
        expected_action_digest=first_binding.action_digest,
        decided_by="host-approval-bridge",
        authority_evidence="scoped_principal:host-approval-bridge:test",
        grant_scope=first_binding.scope,
        grant_ttl_seconds=3600,
        grant_max_uses=2,
    )

    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager, approval_authority=approvals)
    second_project, second_step = project_step("directed")
    second_step.id = "step-work-order-2"
    second_step.ordinal = 2
    second_step.description = "Apply the same approved bounded tool scope"
    waiting = await adapter.execute(second_project, second_step)
    job = next(iter(manager.queue.jobs.values()))
    binding = build_action_binding(
        job_id=job.job_id,
        job_type=job.job_type.value,
        payload=job.payload,
    )
    use = approvals.get_grant_use(binding.action_digest)

    assert decided["grant"] is not None
    assert waiting[0] is None and waiting[1].endswith(":queued")
    assert job.status is JobStatus.QUEUED
    assert use is not None
    assert use["operation_id"] == job.job_id
    assert use["grant_id"] == decided["grant"]["grant_id"]
    assert job.tags["approval_provenance"] == "server_bounded_grant"
    assert job.tags["bounded_grant_id"] == use["grant_id"]
    assert job.tags["approval_source_request_id"] == use["source_request_id"]
    assert job.tags["approval_decision_id"] == use["decision_id"]


@pytest.mark.asyncio
async def test_queue_adapter_posts_once_and_polls_verified_completion():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(
        manager, receipt_verifier=IndependentArtifactVerifier(),
    )
    project, step = project_step()
    waiting = await adapter.execute(project, step)
    replay = await adapter.execute(project, step)
    assert waiting[0] is None and replay[0] is None
    assert manager.queue.posts == 1
    job = next(iter(manager.queue.jobs.values()))
    assert job.payload["action_hint"] == "agent_project_research"
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, refs=("artifact:primary-source-set",)),
    )
    completed = await adapter.execute(project, step)
    assert completed[0] is True
    assert "primary sources verified" in completed[1]


@pytest.mark.asyncio
async def test_ambiguous_effect_work_order_waits_for_independent_reconciliation():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step("deliver")
    assert (await adapter.execute(project, step))[0] is None
    job = next(iter(manager.queue.jobs.values()))

    # Model a started external effect whose worker was stopped before it could
    # prove whether the effect landed. The adapter must not mint a failed
    # ExecutionResult or invite a duplicate replan/retry.
    job.status = JobStatus.NEUTRAL
    job.tags.update({
        "ambiguous_prior_effects": "true",
        "verification_pending": "true",
        "blocked_reason": "ambiguous_prior_effects",
    })
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.NEUTRAL,
        output={"status": "ambiguous"},
        error="stopped_after_effectful_attempt_started",
    )

    pending = await adapter.execute(project, step)
    assert pending == (
        None,
        f"work_order:{job.job_id}:"
        "awaiting_independent_effect_reconciliation",
    )
    assert job.status is JobStatus.NEUTRAL
    assert manager.queue.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_mode", ["off", "shadow", "live"])
async def test_queue_work_order_is_neutral_until_independent_attestation(
    tmp_path, monkeypatch, worker_mode,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", worker_mode)
    competence = CompetenceStore()
    journal = ActionJournal()
    trust = TrustEngine(competence, journal=journal)
    trust.set_stage("worker:agent_action", "act_first", notify=False)
    trust.confidence = lambda _domain: 1.0
    self_model = SelfModel(competence, trust=trust, journal=journal)

    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(
        db_path=tmp_path / "queue.db",
    )
    manager.queue.configure_governance(WorkerGovernor(
        directive_manager=AllowDirectives(),
        self_model=self_model,
        boundary_required=True,
    ))
    try:
        store = ProjectStore(str(tmp_path / "projects.db"))
        adapter = QueueWorkOrderAdapter(
            manager,
            project_store=store,
            receipt_verifier=IndependentArtifactVerifier(),
        )
        project, step = project_step("research")
        store.save_project(project)
        store.save_step(step)
        assert (await adapter.execute(project, step))[0] is None
        posted = next(iter((
            await manager.queue.get_jobs_by_status(JobStatus.QUEUED)
        )))
        worker_id = "host-action-executor"
        claimed = await manager.queue.claim_job(
            worker_id,
            WorkerCapabilities(
                node_id=worker_id,
                capabilities=set(posted.required_capabilities()),
                job_types={JobType.AGENT_ACTION},
            ),
        )
        assert claimed is not None
        assert await manager.queue.start_job(
            claimed.job_id, worker_id, claimed.claim_attempt_id,
        )
        completion = await manager.queue.complete_job(
            claimed.job_id,
            worker_id,
            execution_claim(
                claimed, refs=("artifact:server-resolved-result",),
            ),
            claim_attempt_id=claimed.claim_attempt_id,
        )
        assert completion["job_status"] == "neutral"
        assert completion["outcome_reason"] == "work_order_transport_only"
        pending = await manager.queue.get_job(claimed.job_id)
        assert pending.tags["verification_pending"] == "true"

        dependent = Job(depends_on=[claimed.job_id])
        await manager.queue.post(dependent)
        assert dependent.status is JobStatus.BLOCKED
        assert await manager.queue.unblock_ready_jobs() == 0
        still_waiting = await manager.queue.get_job(dependent.job_id)
        assert still_waiting.status is JobStatus.BLOCKED

        resolved = await adapter.execute(project, step)
        assert resolved[0] is True
        promoted = await manager.queue.get_job(claimed.job_id)
        assert promoted.status is JobStatus.COMPLETED
        assert promoted.tags["success_attested"] == "true"
        assert promoted.tags["success_verifier_type"] == "artifact_resolver"
        assert len(promoted.tags["success_evidence_digest"]) == 64
        released = await manager.queue.get_job(dependent.job_id)
        assert released.status is JobStatus.QUEUED

        rows = await manager.queue.pending_worker_outcomes(
            include_delivered=True,
        )
        assert [row["outcome"] for row in rows].count("success") == 1
        assert [row["outcome"] for row in rows].count("neutral") == 1
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_posted_turn_work_order_holds_before_claim_and_releases_same_row(
    tmp_path, monkeypatch,
):
    from colony_sidecar.server import _work_order_runtime_hold_reason

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "shadow")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    projects = ProjectStore(str(tmp_path / "runtime-projects.db"))
    concerns = SimpleNamespace(
        get=lambda concern_id: (
            SimpleNamespace(producer_name="turn_concerns")
            if concern_id == "turn-concern" else None
        ),
    )
    job = runtime_fence_work_order(projects)
    queue = QueueManager(tmp_path / "runtime-queue.db")
    await queue.start()
    restarted = None
    try:
        queue.configure_runtime_claim_hold(
            lambda candidate: _work_order_runtime_hold_reason(
                candidate, projects, concerns,
            ),
        )
        await queue.post(job)
        stored = await queue.get_job(job.job_id)
        caps = runtime_fence_worker(stored)

        assert await queue.claim_job("runtime-worker", caps) is None
        held = await queue.get_job(job.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["hold_kind"] == "source_runtime"
        assert held.tags["blocked_reason"] == "source_runtime_hold"
        assert held.tags["source_runtime_hold_reason"] == (
            "turn_concerns_current_mode_not_live"
        )
        assert held.tags["approved_by"] == "owner:test"
        assert held.tags["approval_request_id"] == "approval-preserved"
        assert held.tags["evidence_ref"] == "evidence:preserved"
        assert all(
            value is None
            for value in (
                held.claimed_by, held.claimed_at, held.claim_attempt_id,
                held.claim_expires_at, held.last_heartbeat,
            )
        )

        # A restart before server wiring cannot accidentally release the
        # source hold. Configuring the read-only callback later is sufficient.
        await queue.stop()
        restarted = QueueManager(tmp_path / "runtime-queue.db")
        await restarted.start()
        assert await restarted.claim_job("runtime-worker", caps) is None

        monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
        restarted.configure_runtime_claim_hold(
            lambda candidate: _work_order_runtime_hold_reason(
                candidate, projects, concerns,
            ),
        )
        claimed = await restarted.claim_job("runtime-worker", caps)
        assert claimed is not None
        assert claimed.job_id == job.job_id
        assert claimed.tags["approved_by"] == "owner:test"
        assert claimed.tags["evidence_ref"] == "evidence:preserved"
        assert "source_runtime_hold_reason" not in claimed.tags
        assert "hold_kind" not in claimed.tags
    finally:
        if restarted is not None:
            await restarted.stop()
        elif queue._db is not None:
            await queue.stop()


@pytest.mark.asyncio
async def test_claimed_turn_work_order_holds_before_start_then_reclaims(
    tmp_path, monkeypatch,
):
    from colony_sidecar.server import _work_order_runtime_hold_reason

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    projects = ProjectStore(str(tmp_path / "claimed-projects.db"))
    concerns = SimpleNamespace(
        get=lambda _concern_id: SimpleNamespace(producer_name="turn_concerns"),
    )
    job = runtime_fence_work_order(projects, suffix="claimed")
    queue = QueueManager(tmp_path / "claimed-queue.db")
    await queue.start()
    try:
        queue.configure_runtime_claim_hold(
            lambda candidate: _work_order_runtime_hold_reason(
                candidate, projects, concerns,
            ),
        )
        await queue.post(job)
        stored = await queue.get_job(job.job_id)
        caps = runtime_fence_worker(stored)
        first = await queue.claim_job("runtime-worker", caps)
        assert first is not None
        first_attempt = first.claim_attempt_id

        monkeypatch.setenv("COLONY_TURN_CONCERNS", "shadow")
        assert await queue.start_job(
            first.job_id, "runtime-worker", first_attempt,
        ) is False
        held = await queue.get_job(first.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["approved_by"] == "owner:test"
        assert all(
            value is None
            for value in (
                held.claimed_by, held.claimed_at, held.claim_attempt_id,
                held.claim_expires_at, held.last_heartbeat,
            )
        )

        monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
        second = await queue.claim_job("runtime-worker", caps)
        assert second is not None
        assert second.job_id == first.job_id
        assert second.claim_attempt_id != first_attempt
        assert await queue.start_job(
            second.job_id, "runtime-worker", second.claim_attempt_id,
        ) is True

        # Rollback does not kill effects that have already crossed RUNNING.
        monkeypatch.setenv("COLONY_TURN_CONCERNS", "shadow")
        assert await queue.start_job(
            second.job_id, "runtime-worker", second.claim_attempt_id,
        ) is True
        running = await queue.get_job(second.job_id)
        assert running.status is JobStatus.RUNNING
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_runtime_turn_fence_leaves_other_work_orders_and_jobs_unchanged(
    tmp_path, monkeypatch,
):
    from colony_sidecar.server import _work_order_runtime_hold_reason

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "shadow")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    projects = ProjectStore(str(tmp_path / "ordinary-projects.db"))

    class MustNotReadConcern:
        def get(self, _concern_id):
            raise AssertionError("non-cognition WorkOrder read concern state")

    jobs = [
        runtime_fence_work_order(
            projects, suffix="owner", source="owner", concern_id="turn-concern",
        ),
        runtime_fence_work_order(
            projects, suffix="governed", source="governed_action",
            concern_id="turn-concern",
        ),
        Job(
            job_id="ordinary-research-job",
            job_type=JobType.RESEARCH,
            payload={"query": "bounded"},
        ),
    ]
    queue = QueueManager(tmp_path / "ordinary-queue.db")
    await queue.start()
    try:
        queue.configure_runtime_claim_hold(
            lambda candidate: _work_order_runtime_hold_reason(
                candidate, projects, MustNotReadConcern(),
            ),
        )
        for job in jobs:
            await queue.post(job)
        all_caps = set()
        for job in jobs:
            all_caps.update((await queue.get_job(job.job_id)).required_capabilities())
        caps = WorkerCapabilities(
            node_id="runtime-worker",
            capabilities=all_caps,
            max_concurrent=3,
            job_types={JobType.AGENT_ACTION, JobType.RESEARCH},
        )

        claimed = [
            await queue.claim_job("runtime-worker", caps)
            for _index in range(3)
        ]

        assert {job.job_id for job in claimed if job is not None} == {
            item.job_id for item in jobs
        }
        assert not await queue.get_jobs_by_status(JobStatus.BLOCKED)
    finally:
        await queue.stop()


def test_runtime_turn_fence_ignores_noncanonical_non_cognition_schema_hint(
    tmp_path,
):
    from colony_sidecar.server import _work_order_runtime_hold_reason

    projects = ProjectStore(str(tmp_path / "schema-hint-projects.db"))
    malformed = SimpleNamespace(
        job_type=JobType.AGENT_ACTION,
        job_id="not-a-work-order",
        payload={"schema": "WorkOrderV1", "source": "owner"},
    )

    assert _work_order_runtime_hold_reason(
        malformed, projects, SimpleNamespace(get=lambda _id: None),
    ) == ""


@pytest.mark.asyncio
async def test_cognition_work_order_callback_failure_is_durably_held(
    tmp_path, monkeypatch,
):
    from colony_sidecar.server import _work_order_runtime_hold_reason

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    projects = ProjectStore(str(tmp_path / "callback-source-projects.db"))
    missing_ledger = ProjectStore(str(tmp_path / "callback-missing-projects.db"))
    job = runtime_fence_work_order(projects, suffix="callback-failure")
    queue = QueueManager(tmp_path / "callback-failure-queue.db")
    await queue.start()
    try:
        queue.configure_runtime_claim_hold(
            lambda candidate: _work_order_runtime_hold_reason(
                candidate,
                missing_ledger,
                SimpleNamespace(get=lambda _id: None),
            ),
        )
        await queue.post(job)
        stored = await queue.get_job(job.job_id)

        assert await queue.claim_job(
            "runtime-worker", runtime_fence_worker(stored),
        ) is None
        held = await queue.get_job(job.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["source_runtime_hold_reason"] == (
            "source_runtime_hold_callback_unavailable"
        )
    finally:
        await queue.stop()


def test_startup_fence_is_narrow_to_declared_cognition_work_orders():
    from colony_sidecar.server import _cognition_work_order_startup_hold_reason

    assert _cognition_work_order_startup_hold_reason(SimpleNamespace(
        payload={"schema": "WorkOrderV1", "source": "cognition_spine"},
    )) == "cognition_runtime_initialization_pending"
    for payload in (
        {"schema": "WorkOrderV1", "source": "owner"},
        {"schema": "ExternalEventV1", "source": "cognition_spine"},
        {"source": "cognition_spine"},
        {"query": "ordinary work"},
        None,
    ):
        assert _cognition_work_order_startup_hold_reason(
            SimpleNamespace(payload=payload),
        ) == ""


@pytest.mark.asyncio
async def test_project_setup_failure_keeps_startup_fence_until_full_wiring(
    tmp_path, monkeypatch,
):
    from colony_sidecar.server import (
        _install_cognition_work_order_runtime_fence,
        _install_cognition_work_order_startup_fence,
    )

    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.delenv("COLONY_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    projects = ProjectStore(str(tmp_path / "startup-projects.db"))
    turn_job = runtime_fence_work_order(projects, suffix="startup-failure")
    ordinary_job = Job(
        job_id="startup-ordinary-research",
        job_type=JobType.RESEARCH,
        payload={"query": "ordinary startup work remains available"},
    )
    queue = QueueManager(tmp_path / "startup-queue.db")
    await queue.start()
    try:
        manager = SimpleNamespace(queue=queue)
        _install_cognition_work_order_startup_fence(manager)
        await queue.post(turn_job)
        await queue.post(ordinary_job)

        # Model the verifier/ProjectStore setup exception caught by lifespan:
        # promotion never runs, so the already-installed placeholder remains.
        with pytest.raises(RuntimeError, match="verifier setup failed"):
            raise RuntimeError("verifier setup failed")

        stored_turn = await queue.get_job(turn_job.job_id)
        stored_ordinary = await queue.get_job(ordinary_job.job_id)
        caps = WorkerCapabilities(
            node_id="startup-worker",
            capabilities=(
                set(stored_turn.required_capabilities())
                | set(stored_ordinary.required_capabilities())
            ),
            max_concurrent=2,
            job_types={JobType.AGENT_ACTION, JobType.RESEARCH},
        )
        first = await queue.claim_job("startup-worker", caps)
        second = await queue.claim_job("startup-worker", caps)
        claimed = {job.job_id for job in (first, second) if job is not None}

        assert claimed == {ordinary_job.job_id}
        held = await queue.get_job(turn_job.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["source_runtime_hold_reason"] == (
            "cognition_runtime_initialization_pending"
        )

        # Successful project setup atomically replaces the placeholder with
        # the full ledger/project/concern verifier, then reconciliation
        # releases the exact same durable row when its source is live.
        concerns = SimpleNamespace(
            get=lambda concern_id: (
                SimpleNamespace(producer_name="turn_concerns")
                if concern_id == "turn-concern" else None
            ),
        )
        _install_cognition_work_order_runtime_fence(
            manager, projects, concerns,
        )
        assert await queue.reconcile_runtime_claim_holds() == 1
        released = await queue.get_job(turn_job.job_id)
        assert released.status is JobStatus.QUEUED
        claimed_turn = await queue.claim_job("startup-worker", caps)
        assert claimed_turn is not None
        assert claimed_turn.job_id == turn_job.job_id
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_skipped_action_plane_result_stays_distinct_from_success():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step("deliver")
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output={
            "status": "skipped",
            "reason": "owner denied",
            "action_plane": {"state": "skipped"},
        },
    )
    result = await adapter.execute(project, step)
    assert result[0] is True
    assert result[1].startswith("SKIPPED:")


@pytest.mark.asyncio
async def test_live_project_uses_work_order_not_local_reasoning(monkeypatch):
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    manager = FakeManager()
    store = ProjectStore()
    adapter = QueueWorkOrderAdapter(
        manager,
        project_store=store,
        receipt_verifier=IndependentArtifactVerifier(),
    )
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    reasoning = SimpleNamespace(run_turn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("local reasoning path must not execute")))
    engine = ProjectEngine(
        store,
        reasoning_loop=reasoning,
        work_order_adapter=adapter,
    )
    await engine.tick()
    pending = store.steps_for(project.id)[0]
    assert pending.status == "pending"
    assert pending.result.startswith("work_order:")
    assert manager.queue.posts == 1

    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, refs=("artifact:project-step-output",)),
    )
    project = store.get_project(project.id)
    project.next_review_at = 0
    store.save_project(project)
    await engine.tick()
    finished_step = store.steps_for(project.id)[0]
    assert finished_step.status == "done"
    assert finished_step.work_order_ref.startswith("work-order:")
    assert finished_step.result_ref.startswith("execution-result:")


@pytest.mark.asyncio
async def test_terminal_result_reconciles_even_after_project_is_blocked():
    """Observed queue truth is independent of later pursuit eligibility."""

    manager = FakeManager()
    store = ProjectStore()
    adapter = QueueWorkOrderAdapter(manager, project_store=store)
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    posted = await adapter.execute(project, step)
    assert posted[0] is None

    # Model a boundary becoming stricter after dispatch but before the
    # already-terminal queue result was projected.
    blocked = store.get_project(project.id)
    blocked.status = "blocked"
    blocked.reason = "later_boundary"
    store.save_project(blocked)

    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.FAILED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.FAILED,
        output=execution_claim(job, outcome="failed"),
    )

    first = await adapter.reconcile_terminal_results()
    projected_updated_at = store.steps_for(project.id)[0].updated_at
    second = await adapter.reconcile_terminal_results()

    assert first == {
        "checked": 1,
        "terminal": 1,
        "projected": 1,
        "errors": 0,
    }
    assert second == {
        "checked": 1,
        "terminal": 1,
        "projected": 0,
        "errors": 0,
    }
    assert len(store.execution_attempts_for(job.job_id)) == 1
    result = store.get_execution_result(job.job_id)
    assert result["terminal_outcome"] == "failed"
    persisted_step = store.steps_for(project.id)[0]
    assert persisted_step.result_ref == result["result_ref"]
    assert persisted_step.updated_at == projected_updated_at
    assert store.get_project(project.id).status == "blocked"


@pytest.mark.asyncio
async def test_terminal_reconciliation_never_posts_nonexistent_queue_work():
    manager = FakeManager()
    store = ProjectStore()
    adapter = QueueWorkOrderAdapter(manager, project_store=store)
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    order = WorkOrderV1.for_project_step(project, step)
    store.prepare_work_order(project, step, order)

    report = await adapter.reconcile_terminal_results()

    assert report == {
        "checked": 1,
        "terminal": 0,
        "projected": 0,
        "errors": 0,
    }
    assert manager.queue.posts == 0
    assert manager.queue.jobs == {}


def test_authority_digest_covers_capability_recipient_criteria_risk_and_budget():
    project, step = project_step("directed")
    order = WorkOrderV1.for_project_step(
        project, step, now=datetime(2026, 7, 12, tzinfo=timezone.utc)
    )
    base = order.authority_payload()
    mutations = {
        "source": "owner",
        "capability_allowlist": ["memory:read"],
        "recipient_scope": "owner:someone-else",
        "success_criteria": ["claim success without evidence"],
        "risk_class": "read_only",
        "max_runtime_seconds": 9999,
        "max_attempts": 9,
        "issued_at": "2026-07-13T00:00:00+00:00",
        "deadline": "2026-07-20T00:00:00+00:00",
    }
    for field, value in mutations.items():
        changed = copy.deepcopy(base)
        changed[field] = value
        assert WorkOrderV1.authority_digest_from_payload(changed) != order.work_order_digest


@pytest.mark.asyncio
async def test_stale_execution_result_version_is_refused():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step()
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, work_order_version=0,
                               refs=("artifact:stale",)),
    )
    result = await adapter.execute(project, step)
    assert result[0] is False
    assert "version is stale" in result[1]
    assert adapter.project_store.get_execution_result(job.job_id) is None


@pytest.mark.asyncio
async def test_same_job_id_with_mutated_authority_payload_is_refused():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step()
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.payload["recipient_scope"] = "contact:guest"
    result = await adapter.execute(project, step)
    assert result[0] is False
    assert "authority digest mismatch" in result[1]


@pytest.mark.asyncio
async def test_inflight_legacy_work_order_is_held_instead_of_duplicated():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step()
    order = WorkOrderV1.for_project_step(project, step)
    manager.queue.jobs[order.legacy_work_order_id()] = SimpleNamespace(
        job_id=order.legacy_work_order_id(), status=JobStatus.RUNNING
    )
    result = await adapter.execute(project, step)
    assert result[0] is None
    assert "legacy_migration_hold" in result[1]
    assert manager.queue.posts == 0
    assert order.work_order_id not in manager.queue.jobs


@pytest.mark.asyncio
async def test_external_completion_without_independent_receipt_is_unverified():
    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager)
    project, step = project_step("deliver")
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, refs=("sms:claimed-provider-id",)),
    )
    completed = await adapter.execute(project, step)
    assert completed[0] is None
    logical = adapter.project_store.get_execution_result(job.job_id)
    assert logical["verification_result"] == "unverified"
    assert logical["payload"]["verification_result"] == "unverified"
    assert "no independent evidence verifier" in logical["payload"]["error"]


@pytest.mark.asyncio
async def test_external_completion_accepts_only_independent_verified_receipt():
    class IndependentVerifier:
        def verify(self, **_kwargs):
            return {
                "verified": True,
                "receipt_refs": ["sms:provider-receipt-42"],
                "verifier_identity": "action-plane-receipt-verifier:v1",
            }

    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager, receipt_verifier=IndependentVerifier())
    project, step = project_step("deliver")
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, refs=("sms:executor-claim",)),
    )
    completed = await adapter.execute(project, step)
    assert completed[0] is True
    logical = adapter.project_store.get_execution_result(job.job_id)
    assert logical["payload"]["receipt_refs"] == ["sms:provider-receipt-42"]
    assert logical["payload"]["verifier_identity"] == "action-plane-receipt-verifier:v1"


@pytest.mark.asyncio
async def test_external_verifier_cannot_use_truthy_string_as_attestation():
    class MalformedVerifier:
        def verify(self, **_kwargs):
            return {
                "verified": "false",
                "receipt_refs": ["sms:untrusted-claim"],
                "verifier_identity": "malformed-verifier",
            }

    manager = FakeManager()
    adapter = QueueWorkOrderAdapter(manager, receipt_verifier=MalformedVerifier())
    project, step = project_step("deliver")
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, refs=("sms:executor-claim",)),
    )
    completed = await adapter.execute(project, step)
    assert completed[0] is False
    assert "must use a boolean" in completed[1]
    assert adapter.project_store.get_execution_result(job.job_id) is None


@pytest.mark.asyncio
async def test_retry_has_one_logical_result_and_retains_every_attempt():
    manager = FakeManager()
    store = ProjectStore()
    adapter = QueueWorkOrderAdapter(
        manager,
        project_store=store,
        receipt_verifier=IndependentArtifactVerifier(),
    )
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, outcome="failed", attempt=1),
    )
    first = await adapter.execute(project, step)
    assert first[0] is False
    first_ref = store.get_execution_result(job.job_id)["result_ref"]

    job.retry_count = 1
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=execution_claim(job, attempt=2, refs=("artifact:retry-output",)),
    )
    second = await adapter.execute(project, step)
    duplicate = await adapter.execute(project, step)
    assert second[0] is True and duplicate[0] is True
    logical = store.get_execution_result(job.job_id)
    assert logical["result_ref"] == first_ref
    assert logical["run_id"] == "run-2"
    assert logical["attempt_number"] == 2
    attempts = store.execution_attempts_for(job.job_id)
    assert [(a["run_id"], a["attempt_number"]) for a in attempts] == [
        ("run-1", 1), ("run-2", 2),
    ]


@pytest.mark.asyncio
async def test_worker_clock_cannot_move_authoritative_result_time():
    manager = FakeManager()
    store = ProjectStore()
    adapter = QueueWorkOrderAdapter(
        manager,
        project_store=store,
        receipt_verifier=IndependentArtifactVerifier(),
    )
    project, step = project_step()
    store.save_project(project)
    store.save_step(step)
    await adapter.execute(project, step)
    job = next(iter(manager.queue.jobs.values()))
    actual_started = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    actual_completed = datetime(2026, 7, 13, 12, 0, 2, tzinfo=timezone.utc)
    claim = execution_claim(
        job,
        refs=("artifact:server-time-bound",),
        started_at="2098-01-01T00:00:00+00:00",
        ended_at="2099-01-01T00:00:00+00:00",
    )
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="host-action-executor",
        status=JobStatus.COMPLETED,
        output=claim,
        started_at=actual_started,
        completed_at=actual_completed,
    )

    completed = await adapter.execute(project, step)

    assert completed[0] is True
    payload = store.get_execution_result(job.job_id)["payload"]
    assert payload["started_at"] == actual_started.isoformat()
    assert payload["ended_at"] == actual_completed.isoformat()


def test_project_store_additive_execution_ledger_migrates_legacy_database(tmp_path):
    db = tmp_path / "legacy-projects.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE projects (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT,
            source TEXT, status TEXT, entity_ids TEXT, reason TEXT,
            replans INTEGER DEFAULT 0, next_review_at REAL,
            created_at REAL, updated_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE steps (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, ordinal INTEGER,
            description TEXT, action_kind TEXT, depends_on TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, result TEXT, boundary_subject TEXT,
            created_at REAL, updated_at REAL
        )"""
    )
    conn.execute(
        "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-project", "legacy", "still readable", "owner", "active",
         "[]", "", 0, 0.0, 1.0, 1.0),
    )
    conn.execute(
        "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-step", "legacy-project", 1, "continue safely", "analyze",
         "[]", "pending", 0, "", "", 1.0, 1.0),
    )
    conn.commit()
    conn.close()

    store = ProjectStore(str(db))
    step = store.steps_for("legacy-project")[0]
    assert step.id == "legacy-step"
    assert step.work_order_ref == step.work_order_digest == step.result_ref == ""
    assert step.work_order_issued_at == 0.0
    tables = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "project_work_orders",
        "project_execution_results",
        "project_execution_attempts",
    }.issubset(tables)
